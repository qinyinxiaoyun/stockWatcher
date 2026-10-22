"""Durable segment delivery. Unknown network outcomes may be duplicated."""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone

from .db import connect
from .monitor import OutboxStore
from .push import split_text


def render_payload(payload):
    if "text" in payload:
        return payload["text"]
    if payload.get("target_analyses"):
        article = payload["article"]
        lines = ["AI 自动新闻预警，未经独立事实核验。", article["title"]]
        for item in payload["target_analyses"]:
            analysis = item["analysis"]
            lines.extend([f"[{analysis['level']}] {item['name']}",
                          f"影响方向：{analysis['impact']}",
                          f"影响机制：{analysis['reason']}",
                          f"待核实：{analysis.get('followup', '')}"])
        lines.extend([f"来源：{article['url']}", f"配置 v{payload.get('config_version', '?')}"])
        return "\n".join(lines)
    article, analysis = payload["article"], payload["analysis"]
    targets = payload.get("target_names") or article.get("target_keys", [])
    return ("AI 自动新闻预警，未经独立事实核验。\n"
            f"[{analysis.get('level', '重要')}] {'、'.join(targets)}\n{article['title']}\n"
            f"影响方向：{analysis.get('impact', analysis.get('sentiment', '不确定'))}\n"
            f"影响机制：{analysis.get('reason', analysis.get('summary', ''))}\n"
            f"待核实：{analysis.get('followup', '')}\n来源：{article['url']}\n"
            f"配置 v{payload.get('config_version', '?')}")


class DeliveryDispatcher:
    def __init__(self, settings, client, *, owner=None):
        self.settings, self.client, self.owner = settings, client, owner or uuid.uuid4().hex
        self.store = OutboxStore(settings.database_path)

    def _member_recipient(self, conn, payload):
        """Fail closed for scoped news and receipts; never fall back to broadcast."""
        from .subscriptions import MemberConfigStore, MemberRegistry
        corp_id = payload.get("corp_id")
        recipient = payload.get("touser")
        if (not corp_id or corp_id != self.settings.wecom_corp_id
                or not recipient or "|" in recipient or recipient == "@all"):
            return None
        member = MemberRegistry(conn, corp_id).get(recipient)
        if (member is None or not member.active or member.id != payload.get("member_id")
                or member.generation != payload.get("member_generation")):
            return None
        if payload.get("event") == "subscription.news":
            snapshot = MemberConfigStore(conn, member.id).snapshot()
            targets = {target["key"]: target for target in snapshot.targets}
            matched = payload.get("matched_target_ids", ())
            if not matched:
                return None
            # Cancel the old combined message if any constituent was removed or
            # resumed. Regenerating it could repeat already accepted segments.
            expected = payload.get("subscription_effective_at", {})
            for key in matched:
                if key not in targets:
                    return None
                effective = targets[key].get("effective_at")
                old_effective = expected.get(key)
                if effective and old_effective:
                    from .news import parse_published_at
                    if parse_published_at(effective) != parse_published_at(old_effective):
                        return None
                elif bool(effective) != bool(old_effective):
                    return None
                from .subscription_monitor import MemberScan, SubscriptionMonitorService, _normal
                from .monitor import MonitorSettings
                from .analyzer import validate_analysis
                from .news import NewsTarget
                old_focus = payload.get("analysis_focus", {}).get(key)
                if old_focus is None or sorted({_normal(f) for f in old_focus}) != sorted({_normal(f) for f in targets[key].get("focus", ())}):
                    return None
                item = next((item for item in payload.get("target_analyses", ()) if item.get("target_id") == key), None)
                if item is None:
                    return None
                analysis = validate_analysis(item["analysis"], allowed_target_keys=(key,))
                scan = MemberScan(member, snapshot, MonitorSettings.from_snapshot(snapshot, environ={}),
                                  targets, {stock.id: stock.to_dict() for stock in snapshot.stocks})
                if not SubscriptionMonitorService._notify(scan, NewsTarget.from_value(targets[key]), analysis):
                    return None
        return recipient

    @staticmethod
    def _cancel(conn, item_id, owner):
        conn.execute("UPDATE outbox SET status='cancelled',locked_until=NULL,lease_owner=NULL,last_error='membership_or_subscription_changed' WHERE id=? AND lease_owner=? AND status='sending'",
                     (item_id, owner))

    def deliver_one(self, now=None):
        current = now or datetime.now(timezone.utc)
        started = time.monotonic()
        owner = self.owner + ":" + uuid.uuid4().hex
        items = self.store.claim_due(now=current, owner=owner, limit=1, lease_seconds=120)
        if not items:
            return False
        item = items[0]
        payload = dict(item.payload)
        try:
            conn = connect(self.settings.database_path)
            try:
                member_mode = conn.execute("SELECT value FROM app_meta WHERE key='independent_subscriptions'").fetchone()
                if member_mode and member_mode[0] != self.settings.wecom_corp_id:
                    self._cancel(conn, item.id, owner)
                    return True
                # Explicit HTTP/operator pushes have a separate payload kind.
                # Old scoped-less alerts and management receipts cannot safely
                # regain broadcast privileges after retrying a dead record.
                if member_mode and "member_id" not in payload and payload.get("event") != "push":
                    self._cancel(conn, item.id, owner)
                    return True
            finally:
                conn.close()
            text = render_payload(payload)
            scoped = "member_id" in payload or payload.get("event") == "subscription.news"
            recipient = payload.get("touser") if scoped else payload.get("delivery_touser") or payload.get("touser") or "|".join(self.settings.wecom_to_users)
            if not recipient:
                if not scoped:
                    raise ValueError("no_recipient_configured")
                conn = connect(self.settings.database_path)
                try:
                    self._cancel(conn, item.id, owner)
                finally:
                    conn.close()
                return True
            chunks = payload.get("delivery_chunks") or split_text(text)
            if "delivery_chunks" not in payload and len(chunks) > 1:
                chunks = [f"[{index}/{len(chunks)}]\n{chunk}" for index, chunk in enumerate(chunks, 1)]
            payload["delivery_chunks"] = chunks
            payload["delivery_touser"] = recipient
            accepted = int(payload.get("accepted_chunks", 0))
            for index in range(accepted, len(chunks)):
                conn = connect(self.settings.database_path)
                try:
                    if scoped:
                        recipient = self._member_recipient(conn, payload)
                        if recipient is None:
                            self._cancel(conn, item.id, owner)
                            return True
                    checkpoint = current + timedelta(seconds=time.monotonic() - started)
                    until = (checkpoint + timedelta(seconds=120)).isoformat(timespec="seconds")
                    claimed = conn.execute("UPDATE outbox SET payload_json=?,locked_until=? WHERE id=? AND lease_owner=? AND status='sending' AND locked_until>?",
                        (json.dumps(payload, ensure_ascii=False), until, item.id, owner,
                         checkpoint.isoformat(timespec="seconds"))).rowcount
                    if not claimed:
                        return True
                finally:
                    conn.close()
                # Each successful segment is committed before the next request.
                self.client.send_chunk(chunks[index], to_user=recipient)
                payload["accepted_chunks"] = index + 1
                conn = connect(self.settings.database_path)
                try:
                    conn.execute("UPDATE outbox SET payload_json=? WHERE id=? AND lease_owner=? AND status='sending'",
                        (json.dumps(payload, ensure_ascii=False), item.id, owner))
                finally:
                    conn.close()
            self.store.mark_sent(item.id, owner=owner, now=current)
        except Exception as exc:
            self.store.mark_failed(item.id, owner=owner, now=current,
                                   error=type(exc).__name__, attempts=item.attempts)
        return True
