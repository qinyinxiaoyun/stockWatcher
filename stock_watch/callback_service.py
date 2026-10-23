"""持久化消息处理：修改与回执同事务，自然语言远程调用在事务外。"""
from __future__ import annotations

import json
import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone

from .config import ConfigError, ConfigStore, ConfigVersionConflict
from .db import connect, init_db
from .intent import DeepSeekIntentClient, IntentError
from .subscriptions import MemberConfigStore, MemberRegistry, MembershipError

LOG = logging.getLogger(__name__)
MAX_PENDING_TASKS = 100
MAX_MEMBER_PENDING_TASKS = 10
MAX_RUNNING_TASKS = 4
MAX_MEMBER_RUNNING_TASKS = 1


def now_text():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def queue_reply(conn, message_id, user, text, *, member=None):
    stamp = now_text()
    payload = {"kind": "receipt", "touser": user, "text": text}
    if member is not None:
        if user != member.user_id:
            raise PermissionError("receipt_recipient_mismatch")
        payload.update(member_id=member.id, member_generation=member.generation, corp_id=member.corp_id)
    conn.execute(
        "INSERT OR IGNORE INTO outbox(event_key,payload_json,available_at,created_at) VALUES(?,?,?,?)",
        ("reply:" + message_id, json.dumps(payload, ensure_ascii=False), stamp, stamp),
    )


def _authorized_member(conn, settings, user):
    enabled = conn.execute("SELECT 1 FROM app_meta WHERE key='independent_subscriptions'").fetchone()
    if enabled:
        try:
            registry = MemberRegistry(conn, settings.wecom_corp_id)
            if not registry.is_enabled():
                raise PermissionError("unauthorized_organization")
            member = registry.get(user)
            if member is None or not member.active:
                raise PermissionError("unauthorized_member")
            return member
        except MembershipError:
            raise PermissionError("unauthorized_member") from None
    if user not in settings.wecom_admin_userids:
        raise PermissionError("unauthorized_administrator")
    return None


def authorize_callback(settings, user):
    """The HTTP gate follows the same registry rules as the durable handler."""
    conn = connect(settings.database_path, busy_timeout_ms=700)
    try:
        init_db(conn)
        _authorized_member(conn, settings, user)
    finally:
        conn.close()


def _context_key(user, member):
    return user if member is None else f"member:{member.id}:{member.generation}"


def _task_member(conn, settings, payload):
    member = _authorized_member(conn, settings, payload["user"])
    if member is None:
        if "member_id" in payload:
            raise PermissionError("member_mode_required")
    elif (payload.get("member_id") != member.id
          or payload.get("member_generation") != member.generation
          or payload.get("corp_id") != member.corp_id):
        # Old global tasks never acquire member access when the mode changes.
        raise PermissionError("stale_member_scope")
    return member


def _store(conn, member):
    return ConfigStore(conn) if member is None else MemberConfigStore(conn, member.id)


def _task_owner(payload):
    return (payload.get("corp_id"), payload.get("member_id")) if "member_id" in payload else ("legacy", payload.get("user"))


def _format_snapshot(snapshot):
    data = snapshot.to_dict()
    lines = [f"当前监控（配置 v{data['config_version']}）："]
    for row in data.get("stocks", []):
        lines.append(f"{row['symbol']} {row['name']} {'暂停' if row.get('paused') else '启用'}；关注：{row.get('focus') or '无'}")
    for row in data.get("themes", []):
        lines.append(f"方向：{row['name']}")
    if not data.get("stocks") and not data.get("themes"):
        lines.append("空名单，全部目标暂停。")
    lines.append("推送等级：" + "、".join(data.get("push_levels", ["重大", "重要"])))
    return "\n".join(lines)


def _context(conn, user):
    row = conn.execute("SELECT * FROM context WHERE user_id=?", (user,)).fetchone()
    if not row or (row["expires_at"] and row["expires_at"] < now_text()):
        return {}
    return json.loads(row["context_json"])


def _save_context(conn, user, targets):
    if not targets:
        conn.execute("DELETE FROM context WHERE user_id=?", (user,))
        return
    stamp = now_text()
    expires = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO context(user_id,context_json,updated_at,expires_at) VALUES(?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET context_json=excluded.context_json,updated_at=excluded.updated_at,expires_at=excluded.expires_at",
        (user, json.dumps({"targets": list(dict.fromkeys(targets))}, ensure_ascii=False), stamp, expires),
    )


def _operation_context(snapshot, operations):
    data = snapshot.to_dict()
    targets = []
    for op in operations:
        reference = op.get("target") or op.get("symbol") or op.get("name")
        if not reference:
            continue
        entries = data.get("themes", []) if "theme" in op.get("op", "") else data.get("stocks", [])
        found = [row for row in entries if reference in {
            row["id"], row.get("symbol", ""), row["name"], *row.get("aliases", [])
        }]
        if len(found) == 1:
            targets.append(found[0]["id"])
    return targets


class CallbackService:
    def __init__(self, settings, *, intent_client=None):
        self.settings = settings
        self.intent_client = intent_client or DeepSeekIntentClient(settings, usage_db_path=settings.database_path)

    def handle(self, message):
        from .commands import CommandService, parse_command

        user = message["from_user"]
        message_id = message["message_id"]
        conn = connect(self.settings.database_path, busy_timeout_ms=700)
        try:
            init_db(conn)
            conn.execute("BEGIN IMMEDIATE")
            member = _authorized_member(conn, self.settings, user)
            if member is not None:
                digest = hashlib.sha256(str(message_id).encode("utf-8")).hexdigest()
                # Upstream deduplication survives revocation/reactivation;
                # generation belongs to authorization, not message identity.
                message_id = f"member:{member.id}:{digest}"
            context_key = _context_key(user, member)
            if conn.execute("SELECT 1 FROM inbox WHERE message_id=?", (message_id,)).fetchone():
                conn.rollback()
                return
            conn.execute(
                "INSERT INTO inbox(message_id,user_id,payload_json,status,created_at) VALUES(?,?,?,?,?)",
                (message_id, user, json.dumps(dict(message), ensure_ascii=False), "pending", now_text()),
            )
            if message["msg_type"] != "text":
                conn.execute("UPDATE inbox SET status='ignored',processed_at=? WHERE message_id=?", (now_text(), message_id))
                conn.commit()
                return
            text = message.get("content", "").strip()
            if not text or len(text) > 2000:
                queue_reply(conn, message_id, user, "消息为空或过长，请发送“帮助”。", member=member)
                conn.execute("UPDATE inbox SET status='done',processed_at=? WHERE message_id=?", (now_text(), message_id))
                conn.commit()
                return
            store = _store(conn, member)
            try:
                parsed = parse_command(text)
            except ConfigError as exc:
                queue_reply(conn, message_id, user, str(exc), member=member)
                conn.execute("UPDATE inbox SET status='done',processed_at=? WHERE message_id=?", (now_text(), message_id))
                conn.commit()
                return
            if parsed is not None:
                try:
                    result = CommandService(store).execute(text, user_id=user, source_message_id=message_id)
                    reply = result.text if hasattr(result, "text") else str(result)
                    if result.status == "updated":
                        operation = getattr(parsed, "operation", None)
                        _save_context(conn, context_key, _operation_context(result.snapshot, [operation] if operation else []))
                except ConfigError as exc:
                    reply = str(exc)
                queue_reply(conn, message_id, user, reply, member=member)
                conn.execute("UPDATE inbox SET status='done',processed_at=? WHERE message_id=?", (now_text(), message_id))
            else:
                snapshot = store.snapshot().to_dict()
                payload = {"message_id": message_id, "user": user, "text": text,
                           "snapshot": snapshot, "context": _context(conn, context_key)}
                if member is not None:
                    payload.update(member_id=member.id, member_generation=member.generation, corp_id=member.corp_id)
                pending = conn.execute("SELECT payload_json FROM async_tasks WHERE status IN ('pending','running')").fetchall()
                owned = sum(_task_owner(json.loads(row[0])) == _task_owner(payload) for row in pending)
                if len(pending) >= MAX_PENDING_TASKS or owned >= MAX_MEMBER_PENDING_TASKS:
                    raise RuntimeError("management queue full")
                stamp = now_text()
                conn.execute(
                    "INSERT INTO async_tasks(task_key,task_type,payload_json,available_at,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    ("intent:" + message_id, "intent", json.dumps(payload, ensure_ascii=False), stamp, stamp, stamp),
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def run_one(self):
        """Claim one persistent intent task; a restart may reclaim its lease."""
        conn = connect(self.settings.database_path)
        task = None
        try:
            init_db(conn)
            conn.execute("BEGIN IMMEDIATE")
            candidates = conn.execute(
                "SELECT * FROM async_tasks WHERE task_type='intent' AND ((status='pending' AND available_at<=?) "
                "OR (status='running' AND locked_until<?)) ORDER BY id", (now_text(), now_text())
            ).fetchall()
            running = conn.execute(
                "SELECT payload_json FROM async_tasks WHERE task_type='intent' AND status='running' AND locked_until>=?",
                (now_text(),),
            ).fetchall()
            if len(running) < MAX_RUNNING_TASKS:
                owners = [_task_owner(json.loads(row[0])) for row in running]
                task = next((row for row in candidates if owners.count(_task_owner(json.loads(row["payload_json"])))
                             < MAX_MEMBER_RUNNING_TASKS), None)
            if task is None:
                conn.rollback()
                return False
            lease = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat(timespec="seconds")
            claim = uuid.uuid4().hex
            conn.execute(
                "UPDATE async_tasks SET status='running',attempts=attempts+1,locked_until=?,last_error=?,updated_at=? WHERE id=?",
                (lease, claim, now_text(), task["id"]),
            )
            conn.commit()
            payload = json.loads(task["payload_json"])
            user, message_id = payload["user"], payload["message_id"]
            member = None
            authorized = False
            try:
                member = _task_member(conn, self.settings, payload)
                authorized = True
                actual_version = _store(conn, member).snapshot().config_version
                if actual_version != payload["snapshot"]["config_version"]:
                    raise ConfigVersionConflict(payload["snapshot"]["config_version"], actual_version)
            except PermissionError:
                authorized = False
                reply = "该成员已无管理权限，未执行修改。"
                plan = None
            except ConfigVersionConflict:
                reply = "配置已被其他操作更新，本次未修改。请查看监控后重新发送请求。"
                plan = None
            if authorized and actual_version == payload["snapshot"]["config_version"]:
                try:
                    # A stored snapshot prevents silently rebasing model edits.
                    class Snapshot:
                        def to_dict(self):
                            return payload["snapshot"]
                    plan = self.intent_client.parse(payload["text"], Snapshot(), payload.get("context"))
                    # Revalidate the result against the frozen member snapshot;
                    # no model field can select a recipient or another member.
                    from .intent import validate_intent
                    if plan.config_version != payload["snapshot"]["config_version"]:
                        raise IntentError("模型返回的版本无效，配置未修改。")
                    plan = validate_intent({"operations": list(plan.operations)}, Snapshot(), payload.get("context"))
                    reply = None
                except (IntentError, TimeoutError, OSError) as exc:
                    plan = None
                    reply = str(exc) if isinstance(exc, IntentError) else "模型暂不可用，配置未修改；请使用明确指令。"
            conn.execute("BEGIN IMMEDIATE")
            current_task = conn.execute("SELECT status,last_error FROM async_tasks WHERE id=?", (task["id"],)).fetchone()
            if current_task["status"] != "running" or current_task["last_error"] != claim:
                conn.rollback()
                return False
            try:
                member = _task_member(conn, self.settings, payload)
            except PermissionError:
                # Reauthorization also happens after the model call. Revoked
                # tasks produce no receipt containing the old private data.
                authorized = False
                plan = None
                reply = "该成员已无管理权限，未执行修改。"
            if plan is not None:
                try:
                    store = _store(conn, member)
                    actual_version = store.snapshot().config_version
                    if actual_version != plan.config_version:
                        raise ConfigVersionConflict(plan.config_version, actual_version)
                    if plan.query:
                        reply = _format_snapshot(store.snapshot())
                    elif sum(op["op"] in {"remove_stock", "delete_theme", "remove_theme"} for op in plan.operations) >= 2:
                        confirmation = store.request_confirmation(user_id=user, action="batch",
                            payload={"operations": list(plan.operations)})
                        scope = "；".join(op.get("target", op.get("name", "")) for op in plan.operations)
                        reply = (f"批量操作范围：{scope}。配置尚未修改。请在5分钟内发送“确认 {confirmation.token}”；"
                                 "配置版本改变后需重新确认。")
                    else:
                        snapshot = store.apply_operations(list(plan.operations), actor=user,
                            expected_version=plan.config_version, source="wecom-natural-language", source_message_id=message_id)
                        reply = (f"已保存 {len(plan.operations)} 项修改，配置 v{snapshot.config_version}，生效时间 {now_text()}。"
                                 "在下一次定时任务开始时读取。")
                        _save_context(conn, _context_key(user, member), _operation_context(snapshot, plan.operations))
                except ConfigVersionConflict:
                    reply = "配置已被其他操作更新，本次未修改。请查看监控后重新发送请求。"
                except ConfigError as exc:
                    reply = f"配置未修改：{exc}"
            if authorized:
                queue_reply(conn, message_id, user, reply, member=member)
            stamp = now_text()
            conn.execute("UPDATE async_tasks SET status='done',completed_at=?,updated_at=?,locked_until=NULL,last_error=NULL WHERE id=?",
                         (stamp, stamp, task["id"]))
            conn.execute("UPDATE inbox SET status='done',processed_at=? WHERE message_id=?", (stamp, message_id))
            conn.commit()
            return True
        except BaseException:
            conn.rollback()
            LOG.error("intent_task_failed task_id=%s", task["id"] if task else None)
            if task is not None:
                # Unexpected failures also have bounded recovery; a broken task
                # cannot prevent all later management tasks from progressing.
                attempts = int(task["attempts"]) + 1
                retry_at = (datetime.now(timezone.utc) + timedelta(seconds=min(600, 10 * 2 ** min(attempts, 6)))).isoformat(timespec="seconds")
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    changed = conn.execute(
                        "UPDATE async_tasks SET status=?,available_at=?,locked_until=NULL,last_error='internal_error',updated_at=? "
                        "WHERE id=? AND status='running' AND last_error=?",
                        ("dead" if attempts >= 5 else "pending", retry_at, now_text(), task["id"], claim),
                    ).rowcount
                    if changed and attempts >= 5:
                        saved = json.loads(task["payload_json"])
                        try:
                            current_member = _task_member(conn, self.settings, saved)
                        except PermissionError:
                            pass
                        else:
                            queue_reply(conn, saved["message_id"], saved["user"], "处理失败，配置未修改。请使用明确指令重新发送。", member=current_member)
                    conn.commit()
                except Exception:
                    conn.rollback()
            raise
        finally:
            conn.close()


def create_callback_service(settings):
    return CallbackService(settings)
