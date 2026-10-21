"""Versioned stock configuration backed by SQLite."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from .db import SCHEMA_VERSION


Clock = Callable[[], datetime]
Operation = Mapping[str, Any]

_SYMBOL_RE = re.compile(r"^(?P<code>[0-9A-Z][0-9A-Z_-]{0,15})\.(?P<market>[A-Z]{2,8})$")
_DEFAULT_IDS = {
    "603986.SH": "stk_7a754c32f6c25dc4",
    "688981.SH": "stk_263899307d615441",
    "9988.HK": "stk_a4982c601744566e",
}


class ConfigError(ValueError):
    """Base class for invalid configuration requests."""


class ConfigVersionConflict(ConfigError):
    """The caller edited a stale configuration snapshot."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"configuration version conflict: expected {expected}, current {actual}"
        )


class StockNotFound(ConfigError):
    pass


class DuplicateStock(ConfigError):
    pass


class ConfirmationError(ConfigError):
    pass


@dataclass(frozen=True, slots=True)
class StockConfig:
    id: str
    market: str
    code: str
    symbol: str
    name: str
    aliases: tuple[str, ...]
    focus: str
    enabled: bool
    effective_at: str
    created_at: str
    updated_at: str
    direction: str = "both"
    push_level: str = "important"

    @property
    def paused(self) -> bool:
        return not self.enabled

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "market": self.market,
            "code": self.code,
            "symbol": self.symbol,
            "name": self.name,
            "aliases": list(self.aliases),
            "focus": self.focus,
            "enabled": self.enabled,
            "effective_at": self.effective_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "direction": self.direction,
            "push_level": self.push_level,
            "paused": self.paused,
        }


@dataclass(frozen=True, slots=True)
class ThemeConfig:
    id: str
    name: str
    keywords: tuple[str, ...]
    enabled: bool
    effective_at: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "keywords": list(self.keywords),
            "enabled": self.enabled,
            "effective_at": self.effective_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    schema_version: int
    config_version: int
    updated_at: str
    push_levels: tuple[str, ...]
    stocks: tuple[StockConfig, ...]
    themes: tuple[ThemeConfig, ...]
    targets: tuple[Mapping[str, Any], ...]
    monitor_enabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "config_version": self.config_version,
            "updated_at": self.updated_at,
            "push_levels": list(self.push_levels),
            "stocks": [stock.to_dict() for stock in self.stocks],
            "themes": [theme.to_dict() for theme in self.themes],
            "targets": [dict(target) for target in self.targets],
            "monitor_enabled": self.monitor_enabled,
        }


@dataclass(frozen=True, slots=True)
class Confirmation:
    token: str
    user_id: str
    action: str
    config_version: int
    expires_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "user_id": self.user_id,
            "action": self.action,
            "config_version": self.config_version,
            "expires_at": _format_time(self.expires_at),
        }


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _normalize_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    match = _SYMBOL_RE.fullmatch(symbol)
    if match is None:
        raise ConfigError("stock symbol must include a market, for example 603986.SH")
    market = match.group("market")
    code = match.group("code")
    if market not in {"SH", "SZ", "BJ", "HK", "US"}:
        raise ConfigError("stock market must be SH, SZ, BJ, HK, or US")
    if market in {"SH", "SZ", "BJ"} and not re.fullmatch(r"\d{6}", code):
        raise ConfigError(f"{market} stock code must contain exactly 6 digits")
    if market == "HK" and not re.fullmatch(r"\d{4,5}", code):
        raise ConfigError("HK stock code must contain 4 or 5 digits")
    if market == "US" and not re.fullmatch(r"[A-Z][A-Z0-9_-]{0,9}", code):
        raise ConfigError("US ticker must start with a letter and contain at most 10 characters")
    return symbol


def _symbol_parts(symbol: str) -> tuple[str, str]:
    match = _SYMBOL_RE.fullmatch(symbol)
    if match is None:  # callers normally pass _normalize_symbol output
        raise ConfigError(f"invalid stock symbol: {symbol!r}")
    return match.group("market"), match.group("code")


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split()).casefold()


def _text(value: Any, *, field: str, max_length: int = 1_000) -> str:
    result = " ".join(str(value or "").strip().split())
    if len(result) > max_length:
        raise ConfigError(f"{field} is too long (maximum {max_length} characters)")
    return result


def _string_list(value: Any, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw: Iterable[Any] = re.split(r"[,，]", value)
    elif isinstance(value, Iterable):
        raw = value
    else:
        raise ConfigError(f"{field} must be a string or list")
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = _text(item, field=field, max_length=200)
        normalized = _normalized_text(text)
        if text and normalized not in seen:
            seen.add(normalized)
            result.append(text)
    return tuple(result)


def _direction(value: Any) -> str:
    normalized = _normalized_text(value)
    aliases = {
        "both": "both",
        "all": "both",
        "双向": "both",
        "全部": "both",
        "positive": "positive",
        "bullish": "positive",
        "up": "positive",
        "看多": "positive",
        "利好": "positive",
        "negative": "negative",
        "bearish": "negative",
        "down": "negative",
        "看空": "negative",
        "利空": "negative",
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ConfigError("direction must be both/positive/negative") from exc


def _push_level(value: Any) -> str:
    normalized = _normalized_text(value)
    aliases = {
        "all": "all",
        "high": "all",
        "全部": "all",
        "高": "all",
        "important": "important",
        "normal": "important",
        "重要": "important",
        "中": "important",
        "none": "none",
        "off": "none",
        "low": "none",
        "静默": "none",
        "关闭": "none",
        "低": "none",
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ConfigError("push_level must be all/important/none") from exc


def _push_levels(value: Any) -> tuple[str, ...]:
    values = _string_list(value, field="push_levels")
    aliases = {
        "重大": "重大",
        "major": "重大",
        "critical": "重大",
        "重要": "重要",
        "important": "重要",
        "普通": "普通",
        "normal": "普通",
    }
    result: list[str] = []
    for item in values:
        try:
            level = aliases[_normalized_text(item)]
        except KeyError as exc:
            raise ConfigError("push levels must be 重大, 重要, or 普通") from exc
        if level not in result:
            result.append(level)
    if not result:
        raise ConfigError("at least one push level is required")
    return tuple(result)


def _stable_id(symbol: str) -> str:
    if symbol in _DEFAULT_IDS:
        return _DEFAULT_IDS[symbol]
    digest = hashlib.sha256(f"stock-watch:{symbol}".encode("utf-8")).hexdigest()
    return f"stk_{digest[:16]}"


def _stable_theme_id(name: str) -> str:
    digest = hashlib.sha256(f"stock-watch-theme:{_normalized_text(name)}".encode("utf-8")).hexdigest()
    return f"theme_{digest[:16]}"


@contextmanager
def _write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    nested = conn.in_transaction
    if nested:
        conn.execute("SAVEPOINT config_write")
    else:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if nested:
            conn.execute("ROLLBACK TO SAVEPOINT config_write")
            conn.execute("RELEASE SAVEPOINT config_write")
        else:
            conn.rollback()
        raise
    else:
        if nested:
            conn.execute("RELEASE SAVEPOINT config_write")
        else:
            conn.commit()


class ConfigStore:
    """Read and atomically mutate the versioned configuration."""

    def __init__(self, conn: sqlite3.Connection, *, clock: Clock | None = None) -> None:
        self.conn = conn
        self.clock = clock or _default_clock

    def snapshot(self) -> ConfigSnapshot:
        return self._snapshot()

    def apply_operation(
        self,
        operation: Operation,
        *,
        actor: str,
        expected_version: int | None = None,
        source: str = "api",
        source_message_id: str | None = None,
    ) -> ConfigSnapshot:
        return self.apply_operations(
            [operation],
            actor=actor,
            expected_version=expected_version,
            source=source,
            source_message_id=source_message_id,
        )

    def apply_operations(
        self,
        operations: Sequence[Operation],
        *,
        actor: str,
        expected_version: int | None = None,
        source: str = "api",
        source_message_id: str | None = None,
    ) -> ConfigSnapshot:
        actor = _text(actor, field="actor", max_length=200)
        source = _text(source, field="source", max_length=100)
        if not actor:
            raise ConfigError("actor is required")
        if not source:
            raise ConfigError("source is required")
        if source_message_id is not None:
            source_message_id = _text(
                source_message_id, field="source_message_id", max_length=300
            ) or None
        materialized = list(operations)
        if not all(isinstance(item, Mapping) for item in materialized):
            raise ConfigError("every operation must be a mapping")

        with _write_transaction(self.conn):
            current = self._config_version()
            self._check_version(expected_version, current)
            if not materialized:
                return self._snapshot()

            changes = [self._apply_one(dict(item)) for item in materialized]
            new_version = current + 1
            now = _format_time(self.clock())
            self.conn.execute(
                "UPDATE config_state SET config_version=?, updated_at=? WHERE singleton=1",
                (new_version, now),
            )
            for action, target_id, before, after in changes:
                self._write_audit(
                    actor=actor,
                    source=source,
                    source_message_id=source_message_id,
                    action=action,
                    target_id=target_id,
                    before=before,
                    after=after,
                    from_version=current,
                    to_version=new_version,
                    now=now,
                )
            return self._snapshot()

    def request_confirmation(
        self,
        *,
        user_id: str,
        action: str = "clear_stocks",
        ttl_seconds: int = 300,
        payload: Mapping[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> Confirmation:
        user_id = _text(user_id, field="user_id", max_length=200)
        if not user_id:
            raise ConfirmationError("user_id is required")
        if action not in {"clear_stocks", "batch"}:
            raise ConfirmationError(f"unsupported confirmation action: {action}")
        if ttl_seconds <= 0:
            raise ConfirmationError("ttl_seconds must be positive")
        now_dt = _as_utc(self.clock())
        expires = now_dt + timedelta(seconds=ttl_seconds)
        token = secrets.token_urlsafe(9)
        with _write_transaction(self.conn):
            version = self._config_version()
            self._check_version(expected_version, version)
            if payload is None and action == "clear_stocks":
                snapshot = self._snapshot()
                payload = {
                    "op": "clear",
                    "stock_ids": [stock.id for stock in snapshot.stocks],
                    "theme_ids": [theme.id for theme in snapshot.themes],
                }
            if action == "batch":
                operations = (payload or {}).get("operations")
                if (
                    not isinstance(operations, list)
                    or not operations
                    or not all(isinstance(item, Mapping) for item in operations)
                ):
                    raise ConfirmationError("batch confirmation requires an operations list")
            self.conn.execute(
                "INSERT INTO confirmations(token,user_id,action,config_version,payload_json,created_at,expires_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    token,
                    user_id,
                    action,
                    version,
                    json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                    _format_time(now_dt),
                    _format_time(expires),
                ),
            )
        return Confirmation(token, user_id, action, version, expires)

    def apply_confirmation(
        self,
        token: str,
        *,
        user_id: str,
        actor: str | None = None,
        source: str = "command",
        source_message_id: str | None = None,
    ) -> ConfigSnapshot:
        token = _text(token, field="token", max_length=200)
        user_id = _text(user_id, field="user_id", max_length=200)
        if not token or not user_id:
            raise ConfirmationError("token and user_id are required")
        actor = _text(actor or user_id, field="actor", max_length=200)
        now_dt = _as_utc(self.clock())
        now = _format_time(now_dt)

        with _write_transaction(self.conn):
            row = self.conn.execute(
                "SELECT * FROM confirmations WHERE token=?", (token,)
            ).fetchone()
            if row is None or row["user_id"] != user_id:
                raise ConfirmationError("confirmation is invalid for this user")
            if row["consumed_at"] is not None:
                raise ConfirmationError("confirmation has already been used")
            if row["action"] not in {"clear_stocks", "batch"}:
                raise ConfirmationError("confirmation action is unsupported")
            if _parse_time(row["expires_at"]) <= now_dt:
                raise ConfirmationError("confirmation has expired")

            current = self._config_version()
            bound_version = int(row["config_version"])
            if current != bound_version:
                raise ConfigVersionConflict(bound_version, current)

            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ConfirmationError("confirmation payload is invalid") from exc
            if row["action"] == "clear_stocks":
                operations: list[Mapping[str, Any]] = [{"op": "clear"}]
            else:
                raw_operations = payload.get("operations") if isinstance(payload, Mapping) else None
                if (
                    not isinstance(raw_operations, list)
                    or not raw_operations
                    or not all(isinstance(item, Mapping) for item in raw_operations)
                ):
                    raise ConfirmationError("confirmation payload is invalid")
                operations = raw_operations
            changes = [self._apply_one(dict(operation)) for operation in operations]
            new_version = current + 1
            self.conn.execute(
                "UPDATE config_state SET config_version=?, updated_at=? WHERE singleton=1",
                (new_version, now),
            )
            self.conn.execute(
                "UPDATE confirmations SET consumed_at=? WHERE token=?", (now, token)
            )
            for change in changes:
                action, target_id, before, after = change
                self._write_audit(
                    actor=actor,
                    source=source,
                    source_message_id=source_message_id,
                    action=action,
                    target_id=target_id,
                    before=before,
                    after=after,
                    from_version=current,
                    to_version=new_version,
                    now=now,
                )
            return self._snapshot()

    def audit(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        rows = self.conn.execute(
            "SELECT * FROM config_audit ORDER BY id DESC LIMIT ?", (min(limit, 1_000),)
        ).fetchall()
        return [dict(row) for row in rows]

    def _config_version(self) -> int:
        row = self.conn.execute(
            "SELECT config_version FROM config_state WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise ConfigError("database is not initialized; call init_db first")
        return int(row[0])

    @staticmethod
    def _check_version(expected: int | None, actual: int) -> None:
        if expected is not None and int(expected) != actual:
            raise ConfigVersionConflict(int(expected), actual)

    def _snapshot(self) -> ConfigSnapshot:
        state = self.conn.execute(
            "SELECT config_version,push_levels_json,updated_at FROM config_state WHERE singleton=1"
        ).fetchone()
        if state is None:
            raise ConfigError("database is not initialized; call init_db first")
        version = int(state["config_version"])
        schema_row = self.conn.execute(
            "SELECT value FROM app_meta WHERE key='schema_version'"
        ).fetchone()
        schema_version = int(schema_row[0]) if schema_row else SCHEMA_VERSION
        stocks = tuple(
            self._stock_from_row(row)
            for row in self.conn.execute(
                "SELECT * FROM stocks ORDER BY symbol COLLATE NOCASE"
            ).fetchall()
        )
        themes = tuple(
            self._theme_from_row(row)
            for row in self.conn.execute(
                "SELECT * FROM themes ORDER BY normalized_name"
            ).fetchall()
        )
        try:
            stored_levels = json.loads(state["push_levels_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ConfigError("stored push levels are invalid") from exc
        push_levels = _push_levels(stored_levels)
        targets: list[Mapping[str, Any]] = []
        for stock in stocks:
            if not stock.enabled:
                continue
            queries = tuple(
                dict.fromkeys(
                    item
                    for item in (stock.name, *stock.aliases, stock.code, stock.symbol)
                    if item
                )
            )
            focus = tuple(
                item.strip()
                for item in re.split(r"[,，、;；]", stock.focus)
                if item.strip()
            )
            targets.append(
                {
                    "key": stock.id,
                    "kind": "stock",
                    "name": stock.name,
                    "query": queries[0],
                    "queries": list(queries),
                    "focus": list(focus),
                    "effective_at": stock.effective_at,
                    "symbol": stock.symbol,
                }
            )
        for theme in themes:
            if not theme.enabled:
                continue
            queries = theme.keywords or (theme.name,)
            targets.append(
                {
                    "key": theme.id,
                    "kind": "theme",
                    "name": theme.name,
                    "query": queries[0],
                    "queries": list(queries),
                    "focus": [],
                    "effective_at": theme.effective_at,
                }
            )
        return ConfigSnapshot(
            schema_version=schema_version,
            config_version=version,
            updated_at=state["updated_at"],
            push_levels=push_levels,
            stocks=stocks,
            themes=themes,
            targets=tuple(targets),
        )

    def _stock_from_row(self, row: sqlite3.Row) -> StockConfig:
        aliases = tuple(
            item[0]
            for item in self.conn.execute(
                "SELECT alias FROM stock_aliases WHERE stock_id=? "
                "ORDER BY normalized_alias",
                (row["id"],),
            ).fetchall()
        )
        return StockConfig(
            id=row["id"],
            market=row["market"],
            code=row["code"],
            symbol=row["symbol"],
            name=row["name"],
            aliases=aliases,
            focus=row["focus"],
            enabled=bool(row["enabled"]),
            effective_at=row["effective_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            direction=row["direction"],
            push_level=row["push_level"],
        )

    def _theme_from_row(self, row: sqlite3.Row) -> ThemeConfig:
        keywords = tuple(
            item[0]
            for item in self.conn.execute(
                "SELECT keyword FROM theme_keywords WHERE theme_id=? ORDER BY normalized_keyword",
                (row["id"],),
            ).fetchall()
        )
        return ThemeConfig(
            id=row["id"],
            name=row["name"],
            keywords=keywords,
            enabled=bool(row["enabled"]),
            effective_at=row["effective_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _resolve_id(self, reference: Any) -> str:
        ref = _text(reference, field="stock reference", max_length=200)
        if not ref:
            raise StockNotFound("stock reference is required")
        normalized = _normalized_text(ref)
        rows = self.conn.execute(
            """
            SELECT id FROM stocks
             WHERE id=? OR upper(symbol)=upper(?) OR lower(name)=lower(?)
            UNION
            SELECT stock_id FROM stock_aliases WHERE normalized_alias=?
            """,
            (ref, ref, ref, normalized),
        ).fetchall()
        if not rows:
            raise StockNotFound(f"stock not found: {ref}")
        if len(rows) > 1:
            choices = "、".join(self._stock_dict(str(row[0]))["symbol"] for row in rows)
            raise ConfigError(f"股票名称有歧义，请使用带市场的代码。候选：{choices}")
        return str(rows[0][0])

    def _stock_dict(self, stock_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM stocks WHERE id=?", (stock_id,)).fetchone()
        if row is None:
            raise StockNotFound(f"stock not found: {stock_id}")
        return self._stock_from_row(row).to_dict()

    def _resolve_theme_id(self, reference: Any) -> str:
        ref = _text(reference, field="theme reference", max_length=200)
        if not ref:
            raise StockNotFound("theme reference is required")
        row = self.conn.execute(
            "SELECT id FROM themes WHERE id=? OR normalized_name=?",
            (ref, _normalized_text(ref)),
        ).fetchone()
        if row is None:
            raise StockNotFound(f"theme not found: {ref}")
        return str(row[0])

    def _theme_dict(self, theme_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM themes WHERE id=?", (theme_id,)).fetchone()
        if row is None:
            raise StockNotFound(f"theme not found: {theme_id}")
        return self._theme_from_row(row).to_dict()

    def _replace_aliases(self, stock_id: str, aliases: Any) -> None:
        values = _string_list(aliases, field="aliases")
        self.conn.execute("DELETE FROM stock_aliases WHERE stock_id=?", (stock_id,))
        try:
            self.conn.executemany(
                "INSERT INTO stock_aliases(stock_id,alias,normalized_alias) VALUES (?,?,?)",
                [(stock_id, value, _normalized_text(value)) for value in values],
            )
        except sqlite3.IntegrityError as exc:
            raise ConfigError("an alias is already used by another stock") from exc

    def _replace_topics(self, stock_id: str, topics: Any) -> None:
        values = _string_list(topics, field="topics")
        self.conn.execute("DELETE FROM stock_topics WHERE stock_id=?", (stock_id,))
        self.conn.executemany(
            "INSERT INTO stock_topics(stock_id,topic,normalized_topic) VALUES (?,?,?)",
            [(stock_id, value, _normalized_text(value)) for value in values],
        )

    def _replace_theme_keywords(self, theme_id: str, keywords: Any) -> None:
        values = _string_list(keywords, field="keywords")
        if not values:
            theme = self.conn.execute(
                "SELECT name FROM themes WHERE id=?", (theme_id,)
            ).fetchone()
            if theme is None:
                raise StockNotFound(f"theme not found: {theme_id}")
            values = (str(theme[0]),)
        self.conn.execute("DELETE FROM theme_keywords WHERE theme_id=?", (theme_id,))
        self.conn.executemany(
            "INSERT INTO theme_keywords(theme_id,keyword,normalized_keyword) VALUES (?,?,?)",
            [(theme_id, value, _normalized_text(value)) for value in values],
        )

    def _apply_one(
        self, operation: dict[str, Any]
    ) -> tuple[str, str | None, Any, Any]:
        raw_action = operation.pop("op", operation.pop("action", operation.pop("type", "")))
        action = _normalized_text(raw_action).replace("-", "_")
        aliases = {
            "add": "add_stock",
            "delete": "remove_stock",
            "remove": "remove_stock",
            "delete_stock": "remove_stock",
            "pause": "pause_stock",
            "resume": "resume_stock",
            "set_watch": "set_focus",
            "set_watch_point": "set_focus",
            "set_watch_points": "set_focus",
            "set_alias": "set_aliases",
            "delete_theme": "remove_theme",
            "clear_stocks": "clear",
        }
        action = aliases.get(action, action)
        now = _format_time(self.clock())

        if action == "add_stock":
            symbol = _normalize_symbol(operation.get("symbol") or operation.get("target"))
            if self.conn.execute(
                "SELECT 1 FROM stocks WHERE symbol=? COLLATE NOCASE", (symbol,)
            ).fetchone():
                raise DuplicateStock(f"stock already exists: {symbol}")
            stock_id = _stable_id(symbol)
            name = _text(operation.get("name", ""), field="name", max_length=200)
            if not name:
                raise ConfigError("stock name is required with a market-qualified symbol")
            market, code = _symbol_parts(symbol)
            focus = _text(operation.get("focus", ""), field="focus")
            self.conn.execute(
                "INSERT INTO stocks(id,market,code,symbol,name,focus,enabled,paused,direction,push_level,effective_at,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,1,0,'both','important',?,?,?)",
                (
                    stock_id,
                    market,
                    code,
                    symbol,
                    name,
                    focus,
                    now,
                    now,
                    now,
                ),
            )
            self._replace_aliases(stock_id, operation.get("aliases", ()))
            self._replace_topics(stock_id, operation.get("topics", ()))
            return action, stock_id, None, self._stock_dict(stock_id)

        if action == "clear":
            snapshot = self._snapshot()
            before = {
                "stocks": [stock.to_dict() for stock in snapshot.stocks],
                "themes": [theme.to_dict() for theme in snapshot.themes],
            }
            self.conn.execute("DELETE FROM stocks")
            self.conn.execute("DELETE FROM themes")
            return action, None, before, {"stocks": [], "themes": []}

        if action == "set_push_levels":
            before = list(self._snapshot().push_levels)
            levels = _push_levels(operation.get("levels", operation.get("value")))
            self.conn.execute(
                "UPDATE config_state SET push_levels_json=? WHERE singleton=1",
                (json.dumps(levels, ensure_ascii=False, separators=(",", ":")),),
            )
            return action, None, before, list(levels)

        if action == "add_theme":
            name = _text(
                operation.get("name", operation.get("target", "")),
                field="theme name",
                max_length=200,
            )
            if not name:
                raise ConfigError("theme name is required")
            normalized_name = _normalized_text(name)
            if self.conn.execute(
                "SELECT 1 FROM themes WHERE normalized_name=?", (normalized_name,)
            ).fetchone():
                raise DuplicateStock(f"theme already exists: {name}")
            theme_id = _stable_theme_id(name)
            self.conn.execute(
                "INSERT INTO themes(id,name,normalized_name,enabled,effective_at,created_at,updated_at) "
                "VALUES (?,?,?,1,?,?,?)",
                (theme_id, name, normalized_name, now, now, now),
            )
            self._replace_theme_keywords(theme_id, operation.get("keywords", (name,)))
            return action, theme_id, None, self._theme_dict(theme_id)

        if action in {"remove_theme", "pause_theme", "resume_theme", "set_theme_keywords"}:
            reference = operation.get("target", operation.get("theme", operation.get("id")))
            theme_id = self._resolve_theme_id(reference)
            before = self._theme_dict(theme_id)
            if action == "remove_theme":
                self.conn.execute("DELETE FROM themes WHERE id=?", (theme_id,))
                return action, theme_id, before, None
            if action in {"pause_theme", "resume_theme"}:
                enabled = int(action == "resume_theme")
                if enabled:
                    self.conn.execute(
                        "UPDATE themes SET enabled=1,effective_at=?,updated_at=? WHERE id=?",
                        (now, now, theme_id),
                    )
                else:
                    self.conn.execute(
                        "UPDATE themes SET enabled=0,updated_at=? WHERE id=?",
                        (now, theme_id),
                    )
            else:
                self._replace_theme_keywords(
                    theme_id, operation.get("keywords", operation.get("value", ()))
                )
                self.conn.execute(
                    "UPDATE themes SET updated_at=? WHERE id=?", (now, theme_id)
                )
            return action, theme_id, before, self._theme_dict(theme_id)

        reference = operation.get("target", operation.get("stock", operation.get("id")))
        stock_id = self._resolve_id(reference)
        before = self._stock_dict(stock_id)

        if action == "remove_stock":
            self.conn.execute("DELETE FROM stocks WHERE id=?", (stock_id,))
            return action, stock_id, before, None
        if action in {"pause_stock", "resume_stock"}:
            if action == "resume_stock":
                self.conn.execute(
                    "UPDATE stocks SET enabled=1,paused=0,effective_at=?,updated_at=? WHERE id=?",
                    (now, now, stock_id),
                )
            else:
                self.conn.execute(
                    "UPDATE stocks SET enabled=0,paused=1,updated_at=? WHERE id=?",
                    (now, stock_id),
                )
        elif action == "set_focus":
            focus = _text(
                operation.get("focus", operation.get("value", "")), field="focus"
            )
            self.conn.execute(
                "UPDATE stocks SET focus=?, updated_at=? WHERE id=?",
                (focus, now, stock_id),
            )
        elif action == "set_aliases":
            self._replace_aliases(
                stock_id, operation.get("aliases", operation.get("value", ()))
            )
            self.conn.execute("UPDATE stocks SET updated_at=? WHERE id=?", (now, stock_id))
        elif action == "set_name":
            name = _text(operation.get("name", operation.get("value", "")), field="name")
            self.conn.execute(
                "UPDATE stocks SET name=?, updated_at=? WHERE id=?", (name, now, stock_id)
            )
        else:
            raise ConfigError(f"unsupported configuration operation: {raw_action!r}")
        return action, stock_id, before, self._stock_dict(stock_id)

    def _write_audit(
        self,
        *,
        actor: str,
        source: str,
        source_message_id: str | None,
        action: str,
        target_id: str | None,
        before: Any,
        after: Any,
        from_version: int,
        to_version: int,
        now: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO config_audit(
                actor,source,source_message_id,action,target_id,before_json,after_json,
                from_version,to_version,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                actor,
                source,
                source_message_id,
                action,
                target_id,
                None if before is None else json.dumps(before, ensure_ascii=False, sort_keys=True),
                None if after is None else json.dumps(after, ensure_ascii=False, sort_keys=True),
                from_version,
                to_version,
                now,
            ),
        )


__all__ = [
    "ConfigError",
    "ConfigSnapshot",
    "ConfigStore",
    "ConfigVersionConflict",
    "Confirmation",
    "ConfirmationError",
    "DuplicateStock",
    "Operation",
    "StockConfig",
    "StockNotFound",
    "ThemeConfig",
]
