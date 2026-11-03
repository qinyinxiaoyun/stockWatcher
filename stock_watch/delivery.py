"""Durable segment delivery. Unknown network outcomes may be duplicated."""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone

from .db import connect
from .monitor import OutboxStore, _utc, _iso, write_transaction
from .push import split_text
from .notifications import (record_delivery_acceptance, should_notify_event, _event_links,
                            receipt_event_links)


def render_payload(payload):
    if payload.get('event') == 'subscription.digest':
        heading = '晨间经营事实摘要（隔夜）' if payload.get('mode') == 'morning' else '重要经营事实摘要'
        return heading + '\n\n' + '\n\n'.join(render_payload(item) for item in payload.get('items', ()))
    if "text" in payload:
        return payload["text"]
    if payload.get("target_analyses"):
        article = payload["article"]
        lines = ["AI 自动新闻预警，未经独立事实核验。", article["title"], *_source_times(payload)]
        for item in payload["target_analyses"]:
            analysis = item["analysis"]
            lines.extend([f"[{analysis['level']}] {item['name']}",
                          f"影响方向：{analysis['impact']}",
                          f"经营影响：{_brief(analysis['reason'], 180)}",
                          f"待核实：{_brief(analysis.get('followup', ''), 80)}"])
        lines.extend([f"来源：{article['url']}", f"配置 v{payload.get('config_version', '?')}"])
        return "\n".join(lines)
    article, analysis = payload["article"], payload["analysis"]
    targets = payload.get("target_names") or article.get("target_keys", [])
    return ("AI 自动新闻预警，未经独立事实核验。\n"
            f"[{analysis.get('level', '重要')}] {'、'.join(targets)}\n{article['title']}\n"
            + '\n'.join(_source_times(payload)) + '\n'
            f"影响方向：{analysis.get('impact', analysis.get('sentiment', '不确定'))}\n"
            f"经营影响：{_brief(analysis.get('reason', analysis.get('summary', '')), 180)}\n"
            f"待核实：{_brief(analysis.get('followup', ''), 80)}\n来源：{article['url']}\n"
            f"配置 v{payload.get('config_version', '?')}")


def _brief(value, limit):
    text = ' '.join(str(value or '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _source_times(payload):
    from .news import parse_published_at
    article = payload['article']
    published = article.get('source_published_at') or article.get('published_at')
    try:
        published = parse_published_at(published).strftime('%m-%d %H:%M') + '（北京时间）'
    except (TypeError, ValueError):
        published = '待核实'
    lines = [f'发布时间：{published}']
    if payload.get('mode') == 'morning':
        lines.append('隔夜披露')
    elif payload.get('mode') == 'backfill':
        lines.append('历史补查')
    elif payload.get('freshness_reason') == 'major_disclosure_24h_exception':
        lines.append('重大披露补充')
    event_at = payload.get('event_at') or article.get('event_at')
    if event_at:
        lines.append('事实日期：' + str(event_at)[:10])
    fact = payload.get('new_fact') or payload.get('fact_change')
    if fact:
        lines.append('新增事实：' + _brief(fact, 180))
    lines.extend(_fact_changes(payload))
    for item in payload.get('target_analyses', ()):
        analysis = item.get('analysis', {})
        event_at = analysis.get('event_date') or analysis.get('event_at')
        fact = '；'.join(analysis.get('new_facts') or ()) or analysis.get('new_fact')
        if event_at and '事实日期：' + str(event_at)[:10] not in lines:
            lines.append('事实日期：' + str(event_at)[:10])
        if fact and '新增事实：' + _brief(fact, 180) not in lines:
            lines.append('新增事实：' + _brief(fact, 180))
    return lines


def _fact_changes(payload):
    """Render validated fact values, not a free-form model claim of novelty."""
    metric_names = {'revenue': '营业收入', 'net_profit': '净利润', 'gross_margin': '毛利率',
                    'operating_cash_flow': '经营现金流', 'buyback_amount': '回购金额',
                    'order_amount': '订单金额', 'acquisition_amount': '收购金额',
                    'capacity': '产能', 'ownership': '持股比例', 'lifecycle': '实施状态'}
    units = {'CNY': '元', 'USD': '美元', 'HKD': '港元', 'state': ''}
    states = {'planned': '计划', 'implemented': '已实施', 'completed': '已完成', 'cancelled': '已取消'}
    bases = {'attributable': '归母', 'nonrecurring_excluded': '扣非',
             'nonrecurring_excluded_attributable': '扣非归母'}
    relations = {'yoy': '同比', 'qoq': '环比', 'forecast_absolute': '指引',
                 'forecast_yoy': '同比指引', 'forecast_qoq': '环比指引'}
    lines = []
    if payload.get('previous_event_versions'):
        lines.append('同一业务事件的后续更新，保留此前提醒记录。')
    for link in _event_links(payload):
        for change in link.get('member_fact_changes', link.get('fact_changes', ())):
            after, before = change.get('after') or {}, change.get('before')
            if not after or after.get('stale'):
                continue
            kind = change.get('kind')
            if kind in {'opinion', 'rumor', 'additional_evidence'}:
                continue
            metric = metric_names.get(after.get('metric'), after.get('metric') or after.get('key') or '指标')
            label = ' '.join(str(value) for value in (after.get('period'), bases.get(after.get('basis'), ''),
                            metric, relations.get(after.get('relation'), '')) if value)
            def value(fact):
                raw = str(fact.get('value', '待核实'))
                return _brief(states.get(raw, raw) + units.get(fact.get('unit'), fact.get('unit') or ''), 100)
            if kind == 'conflict' or after.get('disputed'):
                text = f'争议待核实：{label}，'
                text += f'此前通知 {value(before)}；本次来源 {value(after)}，尚未替代确定值。' if before else f'本次来源 {value(after)}，尚未确认为经营事实。'
            elif before is not None and before.get('fact_id') != after.get('fact_id'):
                action = {'correction': '事实更正', 'progress': '实质进展', 'confirmation': '官方确认'}.get(kind, '事实更新')
                text = f'{action}（同一事件）：{label}，此前通知 {value(before)} → 本次 {value(after)}'
            elif change.get('source_before') and kind in {'correction', 'progress', 'confirmation'}:
                action = {'correction': '事实更正', 'progress': '实质进展', 'confirmation': '官方确认'}[kind]
                text = f'{action}（同一事件）：{label}，原披露 {value(change["source_before"])} → 本次 {value(after)}'
            else:
                text = f'本次事实：{label} {value(after)}'
            if text not in lines:
                lines.append(text)
    return lines


class DeliveryDispatcher:
    def __init__(self, settings, client, *, owner=None):
        self.settings, self.client, self.owner = settings, client, owner or uuid.uuid4().hex
        self.store = OutboxStore(settings.database_path)

    @staticmethod
    def _quality_valid(payload, current):
        """Old queued news must obey today's policy without acquiring a new life."""
        from .news import NewsArticle, parse_published_at
        from .quality import classify_article, freshness
        from dataclasses import fields
        try:
            article_data = {key: value for key, value in payload['article'].items()
                            if key in {field.name for field in fields(NewsArticle)}}
            for key in ('published_at', 'source_published_at', 'first_seen_at', 'last_seen_at'):
                if article_data.get(key):
                    article_data[key] = parse_published_at(article_data[key])
            article = NewsArticle(**article_data)
            if classify_article(article).kind == 'market_noise':
                return False
            if payload.get('expires_at'):
                if parse_published_at(payload['expires_at']) <= current:
                    return False
                # Unknown/conflicting or future source dates cannot be rescued
                # by a forged expiry. Explicit backfill keeps its fixed expiry.
                if article.time_status != 'trusted' or (article.source_published_at or article.published_at) > current + timedelta(minutes=5):
                    return False
                return True
            if payload.get('mode') == 'backfill':
                # A historical record with no fixed approval lifetime is unsafe.
                return False
            decision = freshness(article, current)
            if decision.mode not in {'realtime', 'urgent'} or decision.expires_at is None:
                return False
            payload['expires_at'] = decision.expires_at.isoformat()
            payload['mode'] = decision.mode
            return True
        except (KeyError, ValueError, TypeError):
            return False

    def _prepare_payload(self, conn, payload, current, *, outbox_id=None):
        """Prune unsent invalid constituents; freeze content after acceptance."""
        accepted = int(payload.get('accepted_chunks', 0))
        event = payload.get('event')
        if event == 'subscription.digest':
            if self._member_recipient(conn, payload) is None:
                return None
            valid = []
            for item in payload.get('items', ()):
                if item.get('event') != 'subscription.news':
                    return None
                if any(item.get(key) != payload.get(key) for key in
                       ('corp_id', 'member_id', 'member_generation', 'touser')):
                    return None
                prepared = self._prepare_news(conn, dict(item), current, frozen=bool(accepted), outbox_id=outbox_id)
                if prepared is None:
                    if accepted:
                        return None
                else:
                    valid.append(prepared)
            if not valid:
                return None
            result = dict(payload, items=valid)
        elif event in {'subscription.news', 'news.analysis'}:
            result = self._prepare_news(conn, dict(payload), current, frozen=bool(accepted), outbox_id=outbox_id)
            if result is None:
                return None
        else:
            result = dict(payload)
        if not accepted:
            result.pop('delivery_chunks', None)
        return result

    def _prepare_news(self, conn, payload, current, *, frozen, outbox_id=None):
        if not self._quality_valid(payload, current):
            return None
        if payload.get('event') != 'subscription.news':
            return payload
        if self._member_recipient(conn, dict(payload, event='subscription.digest')) is None:
            return None
        if not frozen:
            restored = outbox_id is not None and not payload.get('event_links')
            links = receipt_event_links(conn, payload, outbox_id) if restored else _event_links(payload)
            remaining = [link for link in links if should_notify_event(
                conn, payload['corp_id'], payload['member_id'], link)]
            if links and not remaining:
                return None
            if len(remaining) != len(links) and not restored:
                # Without an explicit target-to-event map the rendered material
                # cannot safely be split into accepted and unaccepted facts.
                if any(not link.get('target_ids') for link in remaining):
                    return None
                targets = {key for link in remaining for key in link['target_ids']}
                payload['matched_target_ids'] = [key for key in payload.get('matched_target_ids', ()) if key in targets]
                payload['event_links'] = remaining
        valid = []
        for key in payload.get('matched_target_ids', ()):
            item = next((item for item in payload.get('target_analyses', ()) if item.get('target_id') == key), None)
            if item is None:
                if frozen:
                    return None
                continue
            candidate = dict(payload, matched_target_ids=[key], target_analyses=[item])
            if self._member_recipient(conn, candidate) is None:
                if frozen:
                    return None
            else:
                valid.append(item)
        if not valid:
            return None
        if 'event_links' in payload and not frozen:
            targets = {item['target_id'] for item in valid}
            payload['event_links'] = [dict(link, target_ids=[key for key in link['target_ids'] if key in targets])
                                     if 'target_ids' in link else link
                                     for link in payload['event_links']
                                     if not link.get('target_ids') or targets.intersection(link['target_ids'])]
        return dict(payload, matched_target_ids=[item['target_id'] for item in valid], target_analyses=valid)

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
    def _cancel(conn, item_id, owner, reason='membership_subscription_or_expiry_changed'):
        conn.execute("UPDATE outbox SET status='cancelled',locked_until=NULL,lease_owner=NULL,last_error=? WHERE id=? AND lease_owner=? AND status='sending'",
                     (reason, item_id, owner))

    @staticmethod
    def _reserve_news_window(conn, item_id, owner, payload, current):
        """Retries of one message share a slot; queued backlog cannot burst."""
        if (payload.get('event') != 'subscription.digest'
                or payload.get('mode') != 'realtime' or payload.get('accepted_chunks', 0)):
            return True
        from .scheduler import as_shanghai
        local = as_shanghai(current)
        boundary = local.replace(minute=(local.minute // 30) * 30, second=0, microsecond=0)
        window = boundary.isoformat()
        with write_transaction(conn):
            live = conn.execute("SELECT 1 FROM outbox WHERE id=? AND lease_owner=? AND status='sending' AND locked_until>?",
                                (item_id, owner, _iso(current))).fetchone()
            if not live:
                return False
            existing = conn.execute('''SELECT outbox_id FROM notification_windows
                WHERE corp_id=? AND member_id=? AND kind='delivery_realtime' AND window_key=?''',
                (payload['corp_id'], payload['member_id'], window)).fetchone()
            if existing and existing['outbox_id'] != item_id:
                conn.execute("""UPDATE outbox SET status='pending',available_at=?,locked_until=NULL,
                    lease_owner=NULL,last_error='notification_window_deferred',payload_json=?,
                    attempts=MAX(0,attempts-1)
                    WHERE id=? AND lease_owner=? AND status='sending'""",
                    (_iso(boundary + timedelta(minutes=30)), json.dumps(payload, ensure_ascii=False), item_id, owner))
                return False
            conn.execute('''INSERT OR IGNORE INTO notification_windows(
                corp_id,member_id,kind,window_key,outbox_id,created_at)
                VALUES(?,?,'delivery_realtime',?,?,?)''',
                (payload['corp_id'], payload['member_id'], window, item_id, _iso(current)))
        return True

    def deliver_one(self, now=None):
        current = _utc(now or datetime.now(timezone.utc))
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
                payload = self._prepare_payload(conn, payload, current, outbox_id=item.id)
                if payload is None:
                    self._cancel(conn, item.id, owner)
                    return True
            finally:
                conn.close()
            text = render_payload(payload)
            scoped = "member_id" in payload or payload.get("event") in {'subscription.news', 'subscription.digest'}
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
            index = accepted
            while index < len(chunks):
                conn = connect(self.settings.database_path)
                try:
                    checkpoint = current + timedelta(seconds=time.monotonic() - started)
                    prepared = self._prepare_payload(conn, payload, checkpoint, outbox_id=item.id)
                    if prepared is None:
                        self._cancel(conn, item.id, owner)
                        return True
                    # No accepted segment may ever be re-rendered or reordered.
                    if index == 0 and not payload.get('accepted_chunks', 0):
                        chunks = split_text(render_payload(prepared))
                        if len(chunks) > 1:
                            chunks = [f'[{part}/{len(chunks)}]\n{chunk}' for part, chunk in enumerate(chunks, 1)]
                        prepared['delivery_chunks'] = chunks
                    payload = prepared
                    if scoped:
                        recipient = self._member_recipient(conn, payload)
                        if recipient is None:
                            self._cancel(conn, item.id, owner)
                            return True
                    if index == 0 and not self._reserve_news_window(conn, item.id, owner, payload, checkpoint):
                        return True
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
                accepted_at = current + timedelta(seconds=time.monotonic() - started)
                conn = connect(self.settings.database_path)
                try:
                    with write_transaction(conn):
                        changed = conn.execute("UPDATE outbox SET payload_json=? WHERE id=? AND lease_owner=? AND status='sending'",
                            (json.dumps(payload, ensure_ascii=False), item.id, owner)).rowcount
                        if not changed:
                            return True
                        record_delivery_acceptance(conn, payload, outbox_id=item.id,
                                                   now=accepted_at, complete=index + 1 == len(chunks))
                finally:
                    conn.close()
                index += 1
            conn = connect(self.settings.database_path)
            try:
                with write_transaction(conn):
                    changed = conn.execute("""UPDATE outbox SET status='sent',sent_at=?,locked_until=NULL,
                        lease_owner=NULL,last_error=NULL WHERE id=? AND status='sending' AND lease_owner=?""",
                        (_iso(current + timedelta(seconds=time.monotonic() - started)), item.id, owner)).rowcount
                    if changed:
                        record_delivery_acceptance(conn, payload, outbox_id=item.id,
                                                   now=current + timedelta(seconds=time.monotonic() - started), complete=True)
            finally:
                conn.close()
        except Exception as exc:
            self.store.mark_failed(item.id, owner=owner, now=current,
                                   error=type(exc).__name__, attempts=item.attempts)
        return True
