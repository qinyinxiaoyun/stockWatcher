"""Offline digest cadence, isolation and segment-expiry regression tests."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from stock_watch.db import connect, init_db
from stock_watch.delivery import DeliveryDispatcher, render_payload
from stock_watch.notifications import SCHEMA_SQL, enqueue_candidate, flush_notifications, seed_sent
from stock_watch.settings import Settings
from stock_watch.subscriptions import MemberConfigStore, MemberRegistry


NOW = datetime(2026, 10, 23, 2, 1, tzinfo=timezone.utc)  # 10:01 Shanghai


@pytest.fixture
def state(tmp_path):
    path = tmp_path / 'notifications.db'
    conn = connect(path)
    init_db(conn)
    conn.executescript(SCHEMA_SQL)
    registry = MemberRegistry(conn, 'offline-corp')
    alice = registry.add('alice')
    bob = registry.add('bob')
    for member in (alice, bob):
        store = MemberConfigStore(conn, member.id, clock=lambda: NOW - timedelta(hours=1))
        for symbol, name in [('123456.SH', '示例公司'), ('654321.SH', '另一公司')]:
            store.apply_operation({'op': 'add_stock', 'symbol': symbol, 'name': name, 'focus': '订单'}, actor=member.user_id)
    settings = Settings(data_dir=tmp_path, database_path=path, wecom_corp_id='offline-corp', wecom_to_users=('broadcast',))
    yield path, conn, registry, alice, bob, settings
    conn.close()


def payload(conn, member, event='one', *, targets=None, expiry=None, mode='realtime'):
    snapshot = MemberConfigStore(conn, member.id).snapshot()
    selected = snapshot.targets if targets is None else [target for target in snapshot.targets if target['key'] in targets]
    return {'event': 'subscription.news', 'member_id': member.id, 'member_generation': member.generation,
            'corp_id': member.corp_id, 'touser': member.user_id, 'config_version': snapshot.config_version,
            'quality_policy_version': '1', 'mode': mode, 'event_id': event, 'event_version': 'v1',
            'expires_at': (expiry or NOW + timedelta(hours=2)).isoformat(),
            'matched_target_ids': [target['key'] for target in selected],
            'analysis_focus': {target['key']: target['focus'] for target in selected},
            'subscription_effective_at': {target['key']: target['effective_at'] for target in selected},
            'target_analyses': [{'target_id': target['key'], 'name': target['name'],
                                 'analysis': {'level': '重要', 'impact': '利好', 'reason': '新订单有助收入增长',
                                              'followup': '交付进展', 'matched_targets': [target['key']]}}
                                for target in selected],
            'article': {'key': event, 'title': f'示例公司披露新订单 {event}', 'summary': '新合同订单公告',
                        'url': f'https://example.invalid/{event}', 'published_at': NOW.isoformat()}}


def enqueue(conn, value, *, now=NOW):
    return enqueue_candidate(conn, value, article_key=value['article']['key'], event_id=value['event_id'],
                             event_version=value['event_version'], mode=value['mode'],
                             expires_at=value['expires_at'], now=now)


def outboxes(conn):
    return [json.loads(row[0]) for row in conn.execute('SELECT payload_json FROM outbox ORDER BY id')]


def direct(conn, value, key='direct', now=NOW):
    conn.execute('INSERT INTO outbox(event_key,payload_json,available_at,created_at) VALUES(?,?,?,?)',
                 (key, json.dumps(value, ensure_ascii=False), now.isoformat(), now.isoformat()))


def test_realtime_waits_for_boundary_max_three_and_fair_overflow(state):
    path, conn, _, alice, bob, _ = state
    for member in (alice, bob):
        for index in range(5):
            assert enqueue(conn, payload(conn, member, str(index))) == 1
    assert flush_notifications(path, now=NOW) == 0
    boundary = NOW.replace(minute=30)
    assert flush_notifications(path, now=boundary) == 2
    queued = outboxes(conn)
    assert {item['touser'] for item in queued} == {'alice', 'bob'}
    assert all([child['event_id'] for child in item['items']] == ['0', '1', '2'] for item in queued)
    assert flush_notifications(path, now=boundary + timedelta(minutes=20)) == 0
    assert flush_notifications(path, now=boundary + timedelta(minutes=30)) == 2
    assert all([child['event_id'] for child in item['items']] == ['3', '4'] for item in outboxes(conn)[2:])


def test_morning_once_between_0910_and_0915_without_empty_message(state):
    path, conn, _, alice, _, _ = state
    morning = NOW.replace(hour=1, minute=0)
    end = morning + timedelta(minutes=15)
    assert flush_notifications(path, now=morning + timedelta(minutes=10)) == 0
    for index in range(5):
        value = payload(conn, alice, str(index), expiry=end, mode='morning')
        enqueue(conn, value, now=morning)
    assert flush_notifications(path, now=morning + timedelta(minutes=9)) == 0
    assert flush_notifications(path, now=morning + timedelta(minutes=10)) == 1
    assert len(outboxes(conn)[0]['items']) == 3
    assert flush_notifications(path, now=morning + timedelta(minutes=14)) == 0
    assert flush_notifications(path, now=end) == 0
    assert conn.execute("SELECT count(*) FROM notification_candidates WHERE status='expired'").fetchone()[0] == 2


def test_event_uniqueness_survives_generation_and_fixed_expiry(state):
    path, conn, _, alice, _, _ = state
    value = payload(conn, alice, expiry=NOW + timedelta(minutes=5))
    assert enqueue(conn, value) == 1
    retry = dict(value, member_generation=alice.generation + 1, expires_at=(NOW + timedelta(days=1)).isoformat())
    assert enqueue(conn, retry) == 0
    assert flush_notifications(path, now=NOW + timedelta(minutes=30)) == 0
    assert conn.execute('SELECT status,expires_at FROM notification_candidates').fetchone()['status'] == 'expired'
    assert not outboxes(conn)


def test_new_fact_version_supersedes_unsent_version_and_rollback_is_atomic(state):
    path, conn, _, alice, _, _ = state
    value = payload(conn, alice)
    enqueue(conn, value)
    correction = dict(value, event_version='v2')
    conn.execute('BEGIN IMMEDIATE')
    assert enqueue(conn, correction) == 1
    conn.rollback()
    assert conn.execute('SELECT status FROM notification_candidates').fetchone()[0] == 'pending'
    assert enqueue(conn, correction) == 1
    assert flush_notifications(path, now=NOW.replace(minute=30)) == 1
    assert [item['event_version'] for item in outboxes(conn)[0]['items']] == ['v2']


def test_worker_flushes_due_candidates_when_automatic_scans_are_disabled(state):
    from stock_watch.worker import run_worker_once
    _, conn, _, alice, _, settings = state
    enqueue(conn, payload(conn, alice))
    calls = []
    class Sender:
        def send_chunk(self, text, to_user=None):
            calls.append(to_user)
    result = run_worker_once(None, DeliveryDispatcher(settings, Sender()), now=NOW.replace(minute=30))
    assert result.delivery_processed and calls == ['alice']


def test_revoked_candidates_do_not_consume_member_window(state):
    path, conn, registry, alice, _, _ = state
    enqueue(conn, payload(conn, alice))
    registry.revoke('alice')
    assert flush_notifications(path, now=NOW.replace(minute=30)) == 0
    assert conn.execute('SELECT status FROM notification_candidates').fetchone()[0] == 'cancelled'
    assert conn.execute('SELECT count(*) FROM notification_windows').fetchone()[0] == 0


def test_urgent_backfill_skip_windows_and_receipts_are_independent(state):
    path, conn, _, alice, _, settings = state
    for index, mode in enumerate(('urgent', 'backfill')):
        assert enqueue(conn, payload(conn, alice, str(index), mode=mode)) == 1
    assert flush_notifications(path, now=NOW) == 2
    assert all(item['event'] == 'subscription.news' for item in outboxes(conn))
    direct(conn, {'event': 'push', 'text': 'operator receipt', 'touser': 'alice'}, key='receipt')
    calls = []
    class Sender:
        def send_chunk(self, text, to_user=None):
            calls.append((text, to_user))
    dispatcher = DeliveryDispatcher(settings, Sender())
    for _ in range(3):
        assert dispatcher.deliver_one(now=NOW + timedelta(seconds=1))
    assert len(calls) == 3
    assert calls[-1] == ('operator receipt', 'alice')


def test_new_target_enriches_unsent_event_but_never_sent_event(state):
    path, conn, _, alice, _, _ = state
    keys = [target['key'] for target in MemberConfigStore(conn, alice.id).snapshot().targets]
    first = payload(conn, alice, targets=keys[:1])
    second = payload(conn, alice, targets=keys[1:])
    enqueue(conn, first)
    assert enqueue(conn, second) == 0
    flush_notifications(path, now=NOW.replace(minute=30))
    assert len(outboxes(conn)[0]['items'][0]['matched_target_ids']) == 2
    conn.execute("UPDATE outbox SET status='sent'")
    assert enqueue(conn, second) == 0
    assert len(outboxes(conn)) == 1
    assert seed_sent(conn, first, article_key='migration', event_id='seed', event_version='v1', outbox_id=1, now=NOW) == 1
    assert enqueue(conn, dict(first, event_id='seed')) == 0


def test_queued_target_enrichment_stops_after_first_accepted_segment(state):
    path, conn, _, alice, _, _ = state
    keys = [target['key'] for target in MemberConfigStore(conn, alice.id).snapshot().targets]
    first = payload(conn, alice, targets=keys[:1])
    second = payload(conn, alice, targets=keys[1:])
    enqueue(conn, first)
    flush_notifications(path, now=NOW.replace(minute=30))
    assert enqueue(conn, second) == 0
    assert len(outboxes(conn)[0]['items'][0]['matched_target_ids']) == 2
    queued = outboxes(conn)[0]
    queued.update(accepted_chunks=1, delivery_chunks=['original accepted', 'original pending'])
    conn.execute('UPDATE outbox SET payload_json=?', (json.dumps(queued),))
    changed = deepcopy(second)
    changed['target_analyses'][0]['analysis']['reason'] = '新增目标影响措辞'
    assert enqueue(conn, changed) == 0
    assert outboxes(conn)[0] == queued


def test_delivery_retries_share_slot_and_backlog_defers_without_extending_expiry(state):
    path, conn, _, alice, _, settings = state
    enqueue(conn, payload(conn, alice, 'first'))
    flush_notifications(path, now=NOW.replace(minute=30))
    # A provider timeout leaves the prior digest pending into the next window.
    attempts = []
    class Sender:
        def send_chunk(self, text, to_user=None):
            attempts.append(text)
            if len(attempts) == 1:
                raise TimeoutError('offline injected failure')
    dispatcher = DeliveryDispatcher(settings, Sender())
    assert dispatcher.deliver_one(now=NOW.replace(minute=30))
    enqueue(conn, payload(conn, alice, 'second'), now=NOW.replace(minute=40))
    next_hour = NOW.replace(hour=3, minute=0)
    assert flush_notifications(path, now=next_hour) == 1
    assert dispatcher.deliver_one(now=next_hour)  # first retry may use a new slot
    assert dispatcher.deliver_one(now=next_hour)  # second digest deferred
    rows = conn.execute('SELECT status,available_at,payload_json,last_error,attempts FROM outbox ORDER BY id').fetchall()
    assert rows[0]['status'] == 'sent'
    assert rows[1]['status'] == 'pending' and rows[1]['last_error'] == 'notification_window_deferred'
    assert rows[1]['attempts'] == 0
    assert rows[1]['available_at'] == next_hour.replace(minute=30).isoformat(timespec='seconds')
    assert json.loads(rows[1]['payload_json'])['items'][0]['expires_at'] == (NOW + timedelta(hours=2)).isoformat(timespec='seconds')
    assert dispatcher.deliver_one(now=next_hour.replace(minute=30))
    assert conn.execute('SELECT status FROM outbox ORDER BY id DESC LIMIT 1').fetchone()[0] == 'sent'
    assert len(attempts) == 3


def test_expired_digest_does_not_reserve_actual_delivery_window(state):
    path, conn, _, alice, _, settings = state
    enqueue(conn, payload(conn, alice, expiry=NOW + timedelta(minutes=40)))
    flush_notifications(path, now=NOW.replace(minute=30))
    class Sender:
        def send_chunk(self, *args, **kwargs):
            pytest.fail('expired digest cannot send or reserve window')
    assert DeliveryDispatcher(settings, Sender()).deliver_one(now=NOW + timedelta(minutes=50))
    assert conn.execute("SELECT count(*) FROM notification_windows WHERE kind='delivery_realtime'").fetchone()[0] == 0


def test_digest_prunes_removed_target_before_any_acceptance(state):
    _, conn, _, alice, _, settings = state
    value = payload(conn, alice)
    removed = value['matched_target_ids'][0]
    MemberConfigStore(conn, alice.id).apply_operation({'op': 'pause', 'target': removed}, actor='alice')
    direct(conn, {**{key: value[key] for key in ('member_id','member_generation','corp_id','touser')},
                  'event': 'subscription.digest', 'mode': 'realtime', 'items': [value]})
    sent = []
    class Sender:
        def send_chunk(self, text, to_user=None):
            sent.append(text)
    assert DeliveryDispatcher(settings, Sender()).deliver_one(now=NOW + timedelta(seconds=1))
    stored = outboxes(conn)[0]
    assert len(stored['items'][0]['target_analyses']) == 1
    assert removed not in stored['items'][0]['matched_target_ids']
    assert sent and conn.execute('SELECT status FROM outbox').fetchone()[0] == 'sent'


def test_digest_rejects_foreign_member_and_preserves_accepted_checkpoint(state):
    _, conn, _, alice, bob, settings = state
    value = payload(conn, alice)
    foreign = payload(conn, bob, 'foreign')
    digest = {**{key: value[key] for key in ('member_id','member_generation','corp_id','touser')},
              'event': 'subscription.digest', 'items': [value, foreign]}
    direct(conn, digest)
    class Sender:
        def send_chunk(self, *args, **kwargs):
            pytest.fail('cross-member digest must never send')
    assert DeliveryDispatcher(settings, Sender()).deliver_one(now=NOW + timedelta(seconds=1))
    assert conn.execute('SELECT status FROM outbox').fetchone()[0] == 'cancelled'
    expired = deepcopy(value)
    expired['expires_at'] = (NOW - timedelta(seconds=1)).isoformat()
    digest.update(items=[value, expired], accepted_chunks=1, delivery_chunks=['accepted', 'pending'])
    direct(conn, digest, key='accepted')
    assert DeliveryDispatcher(settings, Sender()).deliver_one(now=NOW + timedelta(seconds=1))
    stored = outboxes(conn)[1]
    assert stored['accepted_chunks'] == 1 and stored['delivery_chunks'] == ['accepted', 'pending']


@pytest.mark.parametrize('reason', ['noise', 'old'])
def test_legacy_queued_news_must_pass_current_noise_and_time_gates(state, reason):
    _, conn, _, alice, _, settings = state
    value = payload(conn, alice)
    for key in ('expires_at', 'mode', 'quality_policy_version'):
        value.pop(key)
    if reason == 'noise':
        value['article'].update(title='南向资金净卖出10亿元', summary='每日净买卖')
    else:
        value['article']['published_at'] = (NOW - timedelta(hours=10)).isoformat()
    direct(conn, value)
    class Sender:
        def send_chunk(self, *args, **kwargs):
            pytest.fail('unsafe legacy queued news was delivered')
    assert DeliveryDispatcher(settings, Sender()).deliver_one(now=NOW)
    assert conn.execute('SELECT status FROM outbox').fetchone()[0] == 'cancelled'


def test_expiry_between_chunks_stops_with_original_checkpoint(state, monkeypatch):
    _, conn, _, alice, _, settings = state
    value = payload(conn, alice, expiry=NOW + timedelta(seconds=10), mode='urgent')
    # Long title gives a real multisegment message without inflating analyses.
    value['article']['title'] += '经营事实' * 2000
    direct(conn, value)
    elapsed = [0.0]
    monkeypatch.setattr('stock_watch.delivery.time.monotonic', lambda: elapsed[0])
    calls = []
    class Sender:
        def send_chunk(self, text, to_user=None):
            calls.append(text)
            elapsed[0] = 11.0
    assert DeliveryDispatcher(settings, Sender()).deliver_one(now=NOW)
    stored = outboxes(conn)[0]
    assert len(calls) == 1 and stored['accepted_chunks'] == 1
    assert len(stored['delivery_chunks']) > 1
    assert conn.execute('SELECT status FROM outbox').fetchone()[0] == 'cancelled'


def test_render_shows_publication_overnight_and_fact_changes(state):
    _, conn, _, alice, _, _ = state
    value = payload(conn, alice, mode='morning')
    value['target_analyses'][0]['analysis'].update(event_date='2026-10-22', new_facts=['合同金额更正为2亿元'])
    text = render_payload(value)
    assert '发布时间：10-23 10:01' in text and '隔夜' in text
    assert '事实日期：2026-10-22' in text and '合同金额更正为2亿元' in text
