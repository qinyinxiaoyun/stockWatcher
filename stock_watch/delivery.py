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
            text = render_payload(payload)
            recipient = payload.get("delivery_touser") or payload.get("touser") or "|".join(self.settings.wecom_to_users)
            if not recipient:
                raise ValueError("no_recipient_configured")
            chunks = payload.get("delivery_chunks") or split_text(text)
            if "delivery_chunks" not in payload and len(chunks) > 1:
                chunks = [f"[{index}/{len(chunks)}]\n{chunk}" for index, chunk in enumerate(chunks, 1)]
            payload["delivery_chunks"] = chunks
            payload["delivery_touser"] = recipient
            accepted = int(payload.get("accepted_chunks", 0))
            for index in range(accepted, len(chunks)):
                conn = connect(self.settings.database_path)
                try:
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
