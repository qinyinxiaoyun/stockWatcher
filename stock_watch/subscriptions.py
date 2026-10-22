"""Organization membership and isolated, versioned personal subscriptions.

The legacy configuration remains intact. Personal configuration is stored as
the complete domain rows in one JSON document, and validated by ConfigStore in
an isolated in-memory database. Optimistic writes only briefly lock the shared
database; no per-member files, dynamic tables, or remote work are needed.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .config import (
    Clock, ConfigError, ConfigSnapshot, ConfigStore, ConfigVersionConflict,
    Confirmation, ConfirmationError, Operation, _format_time, _write_transaction,
)
from .db import connect, init_db


class MembershipError(ConfigError):
    """Membership is absent, inactive, or outside the requested scope."""


# Internal UserIDs are single API recipients. Restrict the registry to the
# documented identifier alphabet; never permit broadcast or recipient lists.
_USER_ID = re.compile(r"[A-Za-z0-9_.@-]{1,64}\Z")
_DOMAIN_TABLES = (
    "config_state", "stocks", "stock_aliases", "stock_topics", "themes", "theme_keywords",
)
_AUDIT_COLUMNS = (
    "actor", "source", "source_message_id", "action", "target_id", "before_json",
    "after_json", "from_version", "to_version", "created_at",
)
_CONFIRMATION_COLUMNS = (
    "token", "user_id", "action", "config_version", "payload_json", "created_at",
    "expires_at", "consumed_at",
)


def _now() -> str:
    return _format_time(datetime.now(timezone.utc))


def _user_id(value: str) -> str:
    if not isinstance(value, str) or not _USER_ID.fullmatch(value) or value.casefold() == "@all":
        raise MembershipError("a single valid internal UserID is required")
    return value


def _actor(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise MembershipError("a trusted operator actor is required")
    return value.strip()


def _dump_domain(conn: sqlite3.Connection) -> str:
    return json.dumps(
        {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
         for table in _DOMAIN_TABLES},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _empty_domain(now: str) -> str:
    return json.dumps({
        "config_state": [{"singleton": 1, "config_version": 1,
                          "push_levels_json": '["重大","重要"]', "updated_at": now}],
        **{table: [] for table in _DOMAIN_TABLES if table != "config_state"},
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _open_domain(document: str) -> sqlite3.Connection:
    """Load trusted persistent rows, preserving IDs, times, and hidden topics."""
    memory = connect(":memory:")
    try:
        init_db(memory)
        data = json.loads(document)
        for table in reversed(_DOMAIN_TABLES):
            memory.execute(f"DELETE FROM {table}")
        for table in _DOMAIN_TABLES:
            allowed = {row[1] for row in memory.execute(f"PRAGMA table_info({table})")}
            for row in data[table]:
                columns = list(row)
                if not set(columns) <= allowed:
                    raise ConfigError("stored member configuration has unsupported fields")
                names = ",".join(columns)
                marks = ",".join("?" for _ in columns)
                memory.execute(f"INSERT INTO {table}({names}) VALUES({marks})",
                               tuple(row[column] for column in columns))
        return memory
    except BaseException:
        memory.close()
        raise


@dataclass(frozen=True, slots=True)
class Member:
    id: int
    corp_id: str
    user_id: str
    role: str
    active: bool
    generation: int
    analysis_private: bool
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _member(row: sqlite3.Row) -> Member:
    return Member(**{name: bool(row[name]) if name in {"active", "analysis_private"}
                     else row[name] for name in Member.__dataclass_fields__})


class MemberRegistry:
    """Trusted local-operator membership administration for one organization.

    These methods must not be exposed as model operations or member commands.
    Adding members does not enable personal monitoring. Explicit legacy-owner
    assignment is the one-time activation boundary.
    """

    def __init__(self, conn: sqlite3.Connection, corp_id: str) -> None:
        if not isinstance(corp_id, str) or not corp_id.strip() or len(corp_id) > 200:
            raise MembershipError("organization identity is required")
        self.conn = conn
        self.corp_id = corp_id.strip()

    def _check_org(self) -> None:
        row = self.conn.execute(
            "SELECT value FROM app_meta WHERE key='independent_subscriptions'"
        ).fetchone()
        if row is not None and row[0] != self.corp_id:
            raise MembershipError("personal subscriptions belong to another organization")

    def get(self, user_id: str) -> Member | None:
        user_id = _user_id(user_id)
        self._check_org()
        row = self.conn.execute("SELECT * FROM members WHERE corp_id=? AND user_id=?",
                                (self.corp_id, user_id)).fetchone()
        return _member(row) if row is not None else None

    def active(self) -> tuple[Member, ...]:
        self._check_org()
        return tuple(_member(row) for row in self.conn.execute(
            "SELECT * FROM members WHERE corp_id=? AND active=1 ORDER BY id", (self.corp_id,)
        ))

    def all(self) -> tuple[Member, ...]:
        self._check_org()
        return tuple(_member(row) for row in self.conn.execute(
            "SELECT * FROM members WHERE corp_id=? ORDER BY id", (self.corp_id,)
        ))

    def is_enabled(self) -> bool:
        self._check_org()
        row = self.conn.execute(
            "SELECT value FROM app_meta WHERE key='independent_subscriptions'"
        ).fetchone()
        return row is not None and row[0] == self.corp_id

    def _audit(self, member_id: int, actor: str, action: str, now: str) -> None:
        self.conn.execute(
            "INSERT INTO member_registry_audit(member_id,actor,action,created_at) VALUES(?,?,?,?)",
            (member_id, actor, action, now),
        )

    def _resume_subscriptions(self, member: Member, actor: str, now: str) -> None:
        """Rejoining starts a fresh live window, without replaying absence."""
        row = self.conn.execute(
            "SELECT * FROM member_configs WHERE member_id=?", (member.id,)
        ).fetchone()
        document = json.loads(row["snapshot_json"])
        enabled = []
        for table in ("stocks", "themes"):
            for item in document[table]:
                if item["enabled"]:
                    enabled.append(item["id"])
                    item["effective_at"] = item["updated_at"] = now
        old_version = int(row["config_version"])
        document["config_state"][0].update(config_version=old_version + 1, updated_at=now)
        self.conn.execute(
            "UPDATE member_configs SET config_version=?,snapshot_json=?,updated_at=? WHERE member_id=?",
            (old_version + 1, json.dumps(document, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":")), now, member.id),
        )
        self.conn.execute(
            "INSERT INTO member_config_audit(member_id,actor,source,action,before_json,after_json,"
            "from_version,to_version,created_at) VALUES(?,?,'membership','reactivate_subscription',?,?,?,?,?)",
            (member.id, actor, json.dumps({"active": False}),
             json.dumps({"active": True, "effective_at": now, "target_ids": enabled}, sort_keys=True),
             old_version, old_version + 1, now),
        )

    def add(self, user_id: str, role: str = "subscriber", actor: str = "local-operator") -> Member:
        user_id, actor = _user_id(user_id), _actor(actor)
        if role not in {"admin", "subscriber"}:
            raise MembershipError("member role must be admin or subscriber")
        now = _now()
        with _write_transaction(self.conn):
            self._check_org()
            current = self.get(user_id)
            if current is None:
                cursor = self.conn.execute(
                    "INSERT INTO members(corp_id,user_id,role,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (self.corp_id, user_id, role, now, now),
                )
                self.conn.execute(
                    "INSERT INTO member_configs(member_id,config_version,snapshot_json,updated_at) VALUES(?,1,?,?)",
                    (cursor.lastrowid, _empty_domain(now), now),
                )
                self._audit(int(cursor.lastrowid), actor, "add", now)
            elif not current.active or current.role != role:
                if not current.active:
                    self._resume_subscriptions(current, actor, now)
                self.conn.execute(
                    "UPDATE members SET active=1,role=?,generation=generation+1,updated_at=? WHERE id=?",
                    (role, now, current.id),
                )
                self._audit(current.id, actor, "reactivate" if not current.active else "set_role", now)
            result = self.get(user_id)
            assert result is not None
            return result

    def revoke(self, user_id: str, actor: str = "local-operator") -> Member:
        actor = _actor(actor)
        with _write_transaction(self.conn):
            member = self.get(user_id)
            if member is None:
                raise MembershipError("member is not registered")
            if member.active:
                now = _now()
                self.conn.execute(
                    "UPDATE members SET active=0,generation=generation+1,updated_at=? WHERE id=?",
                    (now, member.id),
                )
                self.conn.execute(
                    "DELETE FROM member_confirmations WHERE member_id=?", (member.id,)
                )
                self._audit(member.id, actor, "revoke", now)
            result = self.get(user_id)
            assert result is not None
            return result

    def set_analysis_private(self, user_id: str, private: bool, actor: str = "local-operator") -> Member:
        actor = _actor(actor)
        if not isinstance(private, bool):
            raise MembershipError("private must be a boolean")
        with _write_transaction(self.conn):
            member = self.get(user_id)
            if member is None or not member.active:
                raise MembershipError("member is not active")
            if private != member.analysis_private:
                now = _now()
                self.conn.execute(
                    "UPDATE members SET analysis_private=?,generation=generation+1,updated_at=? WHERE id=?",
                    (int(private), now, member.id),
                )
                self._audit(member.id, actor, "analysis_private" if private else "analysis_shared", now)
            result = self.get(user_id)
            assert result is not None
            return result

    def assign_legacy_owner(self, user_id: str, actor: str = "local-operator") -> Member:
        """Explicitly preserve the single original owner's complete config/state."""
        user_id, actor = _user_id(user_id), _actor(actor)
        with _write_transaction(self.conn):
            self._check_org()
            if self.conn.execute(
                "SELECT 1 FROM app_meta WHERE key='legacy_owner_member_id'"
            ).fetchone():
                raise MembershipError("legacy owner has already been assigned")
            # Reject even management replies here: draining the small queue is
            # simpler and safer than guessing whether an old row can broadcast.
            if self.conn.execute(
                "SELECT 1 FROM outbox WHERE status IN ('pending','running','sending') LIMIT 1"
            ).fetchone() or self.conn.execute(
                "SELECT 1 FROM async_tasks WHERE task_type='intent' "
                "AND status IN ('pending','running','sending') LIMIT 1"
            ).fetchone():
                raise MembershipError("drain pending legacy deliveries and intent tasks before activation")
            now = _now()
            if self.conn.execute(
                "SELECT 1 FROM monitor_leases WHERE julianday(lease_until)>julianday(?) LIMIT 1", (now,)
            ).fetchone() or self.conn.execute(
                "SELECT 1 FROM monitor_runs WHERE status='running' "
                "AND julianday(lease_until)>julianday(?) LIMIT 1", (now,)
            ).fetchone():
                raise MembershipError("wait for the active legacy monitor run before activation")
            member = self.get(user_id)
            if member is None:
                raise MembershipError("register the legacy owner explicitly before activation")
            if not member.active:
                raise MembershipError("legacy owner must be an active member")
            existing = MemberConfigStore(self.conn, member.id).snapshot()
            if existing.stocks or existing.themes or existing.config_version != 1:
                raise MembershipError("legacy owner already has personal configuration; refusing to overwrite")
            state = self.conn.execute(
                "SELECT config_version,updated_at FROM config_state WHERE singleton=1"
            ).fetchone()
            self.conn.execute(
                "UPDATE member_configs SET snapshot_json=?,config_version=?,updated_at=? WHERE member_id=?",
                (_dump_domain(self.conn), state["config_version"], state["updated_at"], member.id),
            )
            for row in self.conn.execute("SELECT * FROM config_audit ORDER BY id").fetchall():
                self.conn.execute(
                    f"INSERT INTO member_config_audit(member_id,{','.join(_AUDIT_COLUMNS)}) "
                    f"VALUES({','.join('?' for _ in range(len(_AUDIT_COLUMNS) + 1))})",
                    (member.id, *(row[column] for column in _AUDIT_COLUMNS)),
                )
            # All migration writes, including inherited deduplication/baselines,
            # share this transaction. The monitor helper never commits.
            from .subscription_monitor import copy_legacy_state
            copy_legacy_state(self.conn, member.id)
            self.conn.executemany(
                "INSERT INTO app_meta(key,value) VALUES(?,?)",
                [("independent_subscriptions", self.corp_id),
                 ("legacy_owner_member_id", str(member.id))],
            )
            self._audit(member.id, actor, "assign_legacy_owner", _now())
            return member


class MemberConfigStore:
    """ConfigStore-compatible personal configuration with membership fencing."""

    def __init__(self, conn: sqlite3.Connection, member_id: int, *, clock: Clock | None = None) -> None:
        self.conn, self.member_id, self.clock = conn, member_id, clock

    @property
    def member(self) -> Member:
        row = self.conn.execute("SELECT * FROM members WHERE id=?", (self.member_id,)).fetchone()
        if row is None or not row["active"]:
            raise MembershipError("member is not active")
        enabled = self.conn.execute(
            "SELECT value FROM app_meta WHERE key='independent_subscriptions'"
        ).fetchone()
        if enabled is not None and enabled[0] != row["corp_id"]:
            raise MembershipError("member belongs to another organization")
        return _member(row)

    def _load(self) -> tuple[Member, int, sqlite3.Connection]:
        member = self.member
        row = self.conn.execute("SELECT * FROM member_configs WHERE member_id=?",
                                (self.member_id,)).fetchone()
        if row is None:
            raise MembershipError("member configuration is not initialized")
        return member, int(row["config_version"]), _open_domain(row["snapshot_json"])

    def _check_member(self, member: Member, user_id: str | None = None) -> None:
        current = self.member
        if current.generation != member.generation:
            raise MembershipError("membership changed; retry with the current subscription")
        if user_id is not None and user_id != current.user_id:
            raise MembershipError("members may only operate their own subscription")

    def snapshot(self) -> ConfigSnapshot:
        member, _, memory = self._load()
        try:
            result = ConfigStore(memory, clock=self.clock).snapshot()
            self._check_member(member)
            return result
        finally:
            memory.close()

    def _persist(self, member: Member, old_version: int, memory: sqlite3.Connection,
                 *, confirmation_token: str | None = None) -> ConfigSnapshot:
        snapshot = ConfigStore(memory, clock=self.clock).snapshot()
        document = _dump_domain(memory)
        audits = memory.execute("SELECT * FROM config_audit ORDER BY id").fetchall()
        with _write_transaction(self.conn):
            self._check_member(member)
            row = self.conn.execute(
                "SELECT config_version FROM member_configs WHERE member_id=?", (self.member_id,)
            ).fetchone()
            actual = int(row[0])
            if actual != old_version:
                raise ConfigVersionConflict(old_version, actual)
            if confirmation_token is not None:
                consumed = memory.execute("SELECT consumed_at FROM confirmations WHERE token=?",
                                          (confirmation_token,)).fetchone()[0]
                changed = self.conn.execute(
                    "UPDATE member_confirmations SET consumed_at=? WHERE token=? AND member_id=? "
                    "AND member_generation=? AND consumed_at IS NULL",
                    (consumed, confirmation_token, self.member_id, member.generation),
                ).rowcount
                if changed != 1:
                    raise ConfirmationError("confirmation is invalid or has already been used")
            self.conn.execute(
                "UPDATE member_configs SET config_version=?,snapshot_json=?,updated_at=? WHERE member_id=?",
                (snapshot.config_version, document, snapshot.updated_at, self.member_id),
            )
            for row in audits:
                self.conn.execute(
                    f"INSERT INTO member_config_audit(member_id,{','.join(_AUDIT_COLUMNS)}) "
                    f"VALUES({','.join('?' for _ in range(len(_AUDIT_COLUMNS) + 1))})",
                    (self.member_id, *(row[column] for column in _AUDIT_COLUMNS)),
                )
        return snapshot

    def apply_operation(self, operation: Operation, *, actor: str,
                        expected_version: int | None = None, source: str = "api",
                        source_message_id: str | None = None) -> ConfigSnapshot:
        return self.apply_operations([operation], actor=actor, expected_version=expected_version,
                                     source=source, source_message_id=source_message_id)

    def apply_operations(self, operations: Sequence[Operation], *, actor: str,
                         expected_version: int | None = None, source: str = "api",
                         source_message_id: str | None = None) -> ConfigSnapshot:
        member, version, memory = self._load()
        try:
            self._check_member(member, actor)
            ConfigStore(memory, clock=self.clock).apply_operations(
                operations, actor=actor, expected_version=expected_version, source=source,
                source_message_id=source_message_id,
            )
            return self._persist(member, version, memory)
        finally:
            memory.close()

    def request_confirmation(self, *, user_id: str, action: str = "clear_stocks",
                             ttl_seconds: int = 300, payload: Mapping[str, Any] | None = None,
                             expected_version: int | None = None) -> Confirmation:
        member, version, memory = self._load()
        try:
            self._check_member(member, user_id)
            result = ConfigStore(memory, clock=self.clock).request_confirmation(
                user_id=user_id, action=action, ttl_seconds=ttl_seconds, payload=payload,
                expected_version=expected_version,
            )
            row = memory.execute("SELECT * FROM confirmations WHERE token=?", (result.token,)).fetchone()
            with _write_transaction(self.conn):
                self._check_member(member, user_id)
                current = self.conn.execute(
                    "SELECT config_version FROM member_configs WHERE member_id=?", (self.member_id,)
                ).fetchone()[0]
                if current != version:
                    raise ConfigVersionConflict(version, int(current))
                self.conn.execute(
                    f"INSERT INTO member_confirmations(member_id,member_generation,{','.join(_CONFIRMATION_COLUMNS)}) "
                    f"VALUES({','.join('?' for _ in range(len(_CONFIRMATION_COLUMNS) + 2))})",
                    (self.member_id, member.generation, *(row[column] for column in _CONFIRMATION_COLUMNS)),
                )
            return result
        finally:
            memory.close()

    def apply_confirmation(self, token: str, *, user_id: str, actor: str | None = None,
                           source: str = "command", source_message_id: str | None = None) -> ConfigSnapshot:
        member, version, memory = self._load()
        try:
            self._check_member(member, user_id)
            self._check_member(member, actor if actor is not None else user_id)
            row = self.conn.execute(
                "SELECT * FROM member_confirmations WHERE token=? AND member_id=? AND member_generation=?",
                (token, self.member_id, member.generation),
            ).fetchone()
            if row is None:
                raise ConfirmationError("confirmation is invalid for this member")
            memory.execute(
                f"INSERT INTO confirmations({','.join(_CONFIRMATION_COLUMNS)}) "
                f"VALUES({','.join('?' for _ in _CONFIRMATION_COLUMNS)})",
                tuple(row[column] for column in _CONFIRMATION_COLUMNS),
            )
            ConfigStore(memory, clock=self.clock).apply_confirmation(
                token, user_id=user_id, actor=actor, source=source, source_message_id=source_message_id,
            )
            return self._persist(member, version, memory, confirmation_token=token)
        finally:
            memory.close()

    def audit(self, *, limit: int = 100) -> list[dict[str, Any]]:
        self.member
        if limit <= 0:
            return []
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM member_config_audit WHERE member_id=? ORDER BY id DESC LIMIT ?",
            (self.member_id, min(limit, 1000)),
        )]


__all__ = ["Member", "MemberConfigStore", "MemberRegistry", "MembershipError"]
