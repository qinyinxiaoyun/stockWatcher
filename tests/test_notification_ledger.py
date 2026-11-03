"""Accepted event facts are distinct from research/candidates and frozen text."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from stock_watch.db import connect, init_db
from stock_watch.delivery import DeliveryDispatcher, render_payload
from stock_watch.notifications import (already_notified, enqueue_candidate, flush_notifications,
    record_delivery_acceptance, seed_sent, should_notify_event)
from stock_watch.events import register_event
from stock_watch.news import NewsTarget
from stock_watch.subscription_monitor import _read_article
from stock_watch.settings import Settings
from stock_watch.subscriptions import MemberConfigStore, MemberRegistry


NOW = datetime(2026, 11, 3, 2, 1, tzinfo=timezone.utc)


@pytest.fixture
def state(tmp_path):
    path = tmp_path / 'ledger.db'
    conn = connect(path)
    init_db(conn)
    registry = MemberRegistry(conn, 'offline-corp')
    alice, bob = registry.add('alice'), registry.add('bob')
    for member in (alice, bob):
        MemberConfigStore(conn, member.id, clock=lambda: NOW - timedelta(hours=1)).apply_operation(
            {'op': 'add_stock', 'symbol': '123456.SH', 'name': '示例公司', 'focus': '订单'}, actor=member.user_id)
    settings = Settings(data_dir=tmp_path, database_path=path, wecom_corp_id='offline-corp')
    yield conn, alice, bob, settings
    conn.close()


def news(conn, member, event='order-1', version='change-1', ids=('amount-10',)):
    snapshot = MemberConfigStore(conn, member.id).snapshot()
    target = snapshot.targets[0]
    link = {'event_id': event, 'event_version': version, 'target_ids': [target['key']],
            'event_facts': [{'fact_id': value, 'key': 'amount', 'value': value, 'kind': 'fact'} for value in ids],
            'fact_changes': []}
    return {'event': 'subscription.news', 'corp_id': member.corp_id, 'member_id': member.id,
            'member_generation': member.generation, 'touser': member.user_id,
            'event_id': event, 'event_version': version, 'event_links': [link],
            'mode': 'urgent', 'expires_at': (NOW + timedelta(hours=2)).isoformat(),
            'config_version': snapshot.config_version, 'matched_target_ids': [target['key']],
            'subscription_effective_at': {target['key']: target['effective_at']},
            'analysis_focus': {target['key']: target['focus']},
            'target_analyses': [{'target_id': target['key'], 'name': target['name'],
                'analysis': {'level': '重要', 'impact': '利好', 'reason': '合同订单增加',
                             'followup': '交付进度', 'matched_targets': [target['key']]}}],
            'article': {'key': event + version, 'title': '示例公司披露新增合同订单',
                        'summary': '公司公告合同订单金额', 'url': 'https://example.invalid/' + event,
                        'published_at': NOW.isoformat()}}


def enqueue(conn, value):
    return enqueue_candidate(conn, value, article_key=value['article']['key'], event_id=value['event_id'],
        event_version=value['event_version'], mode=value['mode'], expires_at=value['expires_at'], now=NOW)


def direct(conn, value, key='test'):
    return conn.execute('INSERT INTO outbox(event_key,payload_json,available_at,created_at) VALUES(?,?,?,?)',
        (key, json.dumps(value), NOW.isoformat(), NOW.isoformat())).lastrowid


class Sender:
    def __init__(self, fail_at=None):
        self.accepted = []
        self.fail_at = fail_at

    def send_chunk(self, text, to_user=None):
        if self.fail_at == len(self.accepted):
            self.fail_at = None
            raise TimeoutError('offline injected failure')
        self.accepted.append((text, to_user))


def test_candidates_and_failed_attempts_do_not_mean_notified(state):
    conn, alice, _, settings = state
    value = news(conn, alice)
    assert enqueue(conn, value) == 1
    flush_notifications(settings.database_path, NOW)
    assert conn.execute('SELECT count(*) FROM notification_acceptances').fetchone()[0] == 0
    sender = Sender(fail_at=0)
    assert DeliveryDispatcher(settings, sender).deliver_one(NOW)
    assert conn.execute('SELECT count(*) FROM notification_acceptances').fetchone()[0] == 0
    assert should_notify_event(conn, alice.corp_id, alice.id, value['event_links'][0])
    conn.execute('UPDATE outbox SET available_at=?', (NOW.isoformat(),))
    assert DeliveryDispatcher(settings, sender).deliver_one(NOW)
    accepted = conn.execute('SELECT * FROM notification_acceptances').fetchone()
    assert accepted['complete'] == 1 and json.loads(accepted['facts_json'])[0]['fact_id'] == 'amount-10'


def test_complete_fact_subsets_are_suppressed_across_change_ids_not_scopes(state):
    conn, alice, bob, _ = state
    original = news(conn, alice, ids=('revenue-100', 'profit-10'))
    record_delivery_acceptance(conn, original, outbox_id=1, now=NOW, complete=True)
    subset = news(conn, alice, version='different-observation', ids=('profit-10',))
    link = subset['event_links'][0]
    assert not should_notify_event(conn, alice.corp_id, alice.id, link)
    assert enqueue(conn, subset) == 0
    assert should_notify_event(conn, bob.corp_id, bob.id, link)
    assert should_notify_event(conn, 'another-corp', alice.id, link)
    resumed = dict(original, member_generation=alice.generation + 1)
    assert already_notified(conn, resumed, original['event_id'], original['event_version'])
    correction = news(conn, alice, version='correction', ids=('profit-8',))
    assert should_notify_event(conn, alice.corp_id, alice.id, correction['event_links'][0])


def test_partial_direct_freezes_original_and_does_not_claim_all_facts(state):
    conn, alice, _, settings = state
    value = news(conn, alice)
    value['article']['title'] += '经营订单' * 1000
    enqueue(conn, value)
    flush_notifications(settings.database_path, NOW)
    sender = Sender(fail_at=1)
    DeliveryDispatcher(settings, sender).deliver_one(NOW)
    row = conn.execute('SELECT * FROM notification_acceptances').fetchone()
    assert row['complete'] == 0 and json.loads(row['facts_json']) == []
    original = conn.execute('SELECT payload_json FROM outbox').fetchone()[0]
    retry = deepcopy(value)
    retry['target_analyses'][0]['analysis']['reason'] = '不可覆盖已接受正文'
    enqueue(conn, retry)
    assert conn.execute('SELECT payload_json FROM outbox').fetchone()[0] == original
    alternative = news(conn, alice, version='shorter-source')
    assert should_notify_event(conn, alice.corp_id, alice.id, alternative['event_links'][0])
    assert not should_notify_event(conn, alice.corp_id, alice.id, value['event_links'][0])
    conn.execute('UPDATE outbox SET available_at=?', (NOW.isoformat(),))
    DeliveryDispatcher(settings, sender).deliver_one(NOW)
    assert conn.execute('SELECT complete FROM notification_acceptances').fetchone()[0] == 1
    assert len(sender.accepted) == len(json.loads(original)['delivery_chunks'])


def test_partial_digest_does_not_mark_unseen_later_items(state):
    conn, alice, _, settings = state
    first, later = news(conn, alice), news(conn, alice, event='order-2')
    first['article']['title'] += '经营订单' * 1000
    digest = {key: first[key] for key in ('corp_id', 'member_id', 'member_generation', 'touser')}
    digest.update(event='subscription.digest', mode='urgent', items=[first, later])
    direct(conn, digest)
    sender = Sender(fail_at=1)
    DeliveryDispatcher(settings, sender).deliver_one(NOW)
    assert conn.execute('SELECT count(*) FROM notification_acceptances').fetchone()[0] == 0
    assert should_notify_event(conn, alice.corp_id, alice.id, later['event_links'][0])
    conn.execute('UPDATE outbox SET available_at=?', (NOW.isoformat(),))
    DeliveryDispatcher(settings, sender).deliver_one(NOW)
    rows = conn.execute('SELECT * FROM notification_acceptances').fetchall()
    assert len(rows) == 2 and all(row['complete'] for row in rows)


def test_queued_duplicate_is_pruned_at_delivery_boundary(state):
    conn, alice, _, settings = state
    first = news(conn, alice)
    record_delivery_acceptance(conn, first, outbox_id=100, now=NOW, complete=True)
    duplicate = news(conn, alice, version='reprint')
    direct(conn, duplicate)
    sender = Sender()
    DeliveryDispatcher(settings, sender).deliver_one(NOW)
    assert sender.accepted == []
    assert conn.execute('SELECT status FROM outbox').fetchone()[0] == 'cancelled'


def test_pending_superset_deduplicates_short_reprints_but_disjoint_facts_survive(state):
    conn, alice, _, _ = state
    assert enqueue(conn, news(conn, alice, ids=('revenue-100', 'profit-10'))) == 1
    assert enqueue(conn, news(conn, alice, version='subset', ids=('profit-10',))) == 0
    assert enqueue(conn, news(conn, alice, version='cashflow', ids=('cashflow-9',))) == 1
    rows = conn.execute("SELECT * FROM notification_candidates WHERE status='pending'").fetchall()
    assert len(rows) == 2


def test_correction_removes_only_unaccepted_old_digest_item(state):
    conn, alice, _, settings = state
    first = news(conn, alice)
    first['mode'] = 'realtime'
    another = news(conn, alice, event='independent-order')
    another['mode'] = 'realtime'
    enqueue(conn, first)
    enqueue(conn, another)
    flush_notifications(settings.database_path, NOW.replace(minute=30))
    correction = news(conn, alice, version='corrected', ids=('amount-8',))
    correction['event_links'][0]['event_facts'][0]['supersedes'] = 'amount-10'
    assert enqueue(conn, correction) == 1
    queued = json.loads(conn.execute('SELECT payload_json FROM outbox').fetchone()[0])
    assert [item['event_id'] for item in queued['items']] == ['independent-order']
    assert conn.execute("SELECT status FROM notification_candidates WHERE event_version='change-1' AND event_id='order-1'").fetchone()[0] == 'cancelled'


def test_seed_requires_real_acceptance_and_partial_digest_is_not_full_delivery(state):
    conn, alice, _, _ = state
    value = news(conn, alice)
    item_id = direct(conn, value)
    args = dict(article_key=value['article']['key'], event_id=value['event_id'],
                event_version=value['event_version'], outbox_id=item_id, now=NOW)
    assert seed_sent(conn, value, **args) == 0
    conn.execute("UPDATE outbox SET status='sent'")
    assert seed_sent(conn, value, **args) == 1
    assert already_notified(conn, value, value['event_id'], value['event_version'])
    other = news(conn, alice, event='digest-unseen')
    digest = {key: value[key] for key in ('corp_id', 'member_id', 'member_generation', 'touser')}
    digest.update(event='subscription.digest', items=[other], accepted_chunks=1)
    digest_id = direct(conn, digest, key='digest')
    assert seed_sent(conn, other, article_key='unseen', event_id='digest-unseen',
                     event_version='change-1', outbox_id=digest_id, now=NOW) == 0
    assert should_notify_event(conn, alice.corp_id, alice.id, other['event_links'][0])


@pytest.mark.parametrize('field,value', [('stale', True), ('disputed', True), ('kind', 'conflict'), ('kind', 'opinion')])
def test_non_notifiable_evidence_does_not_create_alert_eligibility(state, field, value):
    conn, alice, _, _ = state
    link = news(conn, alice)['event_links'][0]
    link['event_facts'][0][field] = value
    assert not should_notify_event(conn, alice.corp_id, alice.id, link)


def test_correction_renders_actual_member_baseline_not_intermediate_research(state):
    conn, alice, _, _ = state
    previous = news(conn, alice)
    before = dict(previous['event_links'][0]['event_facts'][0], key='net_profit:2026:Q3',
                  metric='net_profit', value='1000000000', unit='CNY', period='2026:Q3')
    previous['event_links'][0]['event_facts'] = [before]
    record_delivery_acceptance(conn, previous, outbox_id=1, now=NOW, complete=True)
    corrected = news(conn, alice, version='corrected', ids=('profit-8',))
    after = dict(before, fact_id='profit-8', kind='correction', value='800000000', supersedes=before['fact_id'])
    intermediate = dict(before, fact_id='research-only-9', value='900000000')
    corrected['event_links'][0].update(event_facts=[after],
        fact_changes=[{'kind': 'correction', 'before': intermediate, 'after': after}])
    assert enqueue(conn, corrected) == 1
    stored = json.loads(conn.execute("SELECT payload_json FROM notification_candidates").fetchone()[0])
    change = stored['event_links'][0]['member_fact_changes'][0]
    assert change['before'] == before
    rendered = render_payload(stored)
    assert '同一业务事件的后续更新' in rendered
    assert '此前通知 1000000000元 → 本次 800000000元' in rendered
    assert '900000000元' not in rendered


def test_new_correction_never_mutates_a_partially_accepted_outbox(state):
    conn, alice, _, settings = state
    previous = news(conn, alice)
    previous['article']['title'] += '经营订单' * 1000
    enqueue(conn, previous)
    flush_notifications(settings.database_path, NOW)
    DeliveryDispatcher(settings, Sender(fail_at=1)).deliver_one(NOW)
    frozen = conn.execute('SELECT payload_json FROM outbox').fetchone()[0]
    correction = news(conn, alice, version='corrected', ids=('amount-8',))
    correction['event_links'][0]['event_facts'][0]['supersedes'] = 'amount-10'
    assert enqueue(conn, correction) == 1
    assert conn.execute('SELECT payload_json FROM outbox').fetchone()[0] == frozen
    assert conn.execute("SELECT status FROM notification_candidates WHERE event_version='change-1'").fetchone()[0] == 'queued'
    update = json.loads(conn.execute("SELECT payload_json FROM notification_candidates WHERE event_version='corrected'").fetchone()[0])
    assert update['previous_event_versions'] == ['change-1']


def test_acceptance_checkpoint_and_ledger_are_one_transaction(state):
    conn, alice, _, settings = state
    direct(conn, news(conn, alice))
    conn.execute("""CREATE TRIGGER reject_acceptance BEFORE INSERT ON notification_acceptances
        BEGIN SELECT RAISE(ABORT,'offline injected checkpoint failure'); END""")
    sender = Sender()
    DeliveryDispatcher(settings, sender).deliver_one(NOW)
    assert len(sender.accepted) == 1
    row = conn.execute('SELECT status,payload_json FROM outbox').fetchone()
    assert row['status'] == 'pending'
    assert not json.loads(row['payload_json']).get('accepted_chunks')
    assert conn.execute('SELECT count(*) FROM notification_acceptances').fetchone()[0] == 0


def test_genuine_correction_keeps_its_own_lifetime_not_expired_original(state):
    conn, alice, _, _ = state
    old = news(conn, alice)
    old['expires_at'] = (NOW - timedelta(seconds=1)).isoformat()
    enqueue(conn, old)
    # Simulate an old pending candidate not yet expired by the periodic flusher.
    conn.execute("UPDATE notification_candidates SET status='pending'")
    new = news(conn, alice, version='corrected', ids=('amount-8',))
    new['event_links'][0]['event_facts'][0]['supersedes'] = 'amount-10'
    assert enqueue(conn, new) == 1
    row = conn.execute("SELECT status,expires_at FROM notification_candidates WHERE event_version='corrected'").fetchone()
    assert row['status'] == 'pending' and row['expires_at'] > NOW.isoformat()


def test_first_notification_of_correction_labels_source_before_not_member_before(state):
    conn, alice, _, _ = state
    value = news(conn, alice)
    before = {'fact_id': 'old-10', 'metric': 'order_amount', 'key': 'order_amount', 'value': '10', 'unit': 'CNY'}
    after = dict(before, fact_id='new-8', value='8', kind='correction', supersedes='old-10')
    value['event_links'][0].update(event_facts=[after], fact_changes=[
        {'kind': 'correction', 'fact_id': 'new-8', 'before': before, 'after': after}])
    enqueue(conn, value)
    stored = json.loads(conn.execute('SELECT payload_json FROM notification_candidates').fetchone()[0])
    rendered = render_payload(stored)
    assert '事实更正（同一事件）' in rendered and '原披露 10元 → 本次 8元' in rendered
    assert '此前通知' not in rendered


def test_accepted_pending_candidate_does_not_consume_a_digest_window(state):
    conn, alice, _, settings = state
    value = news(conn, alice)
    value['mode'] = 'realtime'
    enqueue(conn, value)
    record_delivery_acceptance(conn, value, outbox_id=99, now=NOW, complete=True)
    assert flush_notifications(settings.database_path, NOW.replace(minute=30)) == 0
    assert conn.execute('SELECT count(*) FROM notification_windows').fetchone()[0] == 0


@pytest.mark.parametrize('stage', ['pending', 'queued', 'sending', 'partial', 'partial_cancelled'])
def test_reserved_fact_union_deduplicates_combined_reprint_without_marking_acceptance(state, stage):
    conn, alice, _, settings = state
    a = news(conn, alice, version='source-a', ids=('fact-a',))
    b = news(conn, alice, version='source-b', ids=('fact-b',))
    a['mode'] = b['mode'] = 'realtime'
    b['event_links'][0]['event_facts'][0]['key'] = 'another-metric'
    assert enqueue(conn, a) == enqueue(conn, b) == 1
    frozen = None
    if stage != 'pending':
        flush_notifications(settings.database_path, NOW.replace(minute=30))
        queued = json.loads(conn.execute('SELECT payload_json FROM outbox').fetchone()[0])
        if stage.startswith('partial'):
            queued.update(accepted_chunks=1, delivery_chunks=['original accepted', 'original pending'])
            conn.execute('UPDATE outbox SET payload_json=?', (json.dumps(queued),))
        if stage == 'sending':
            conn.execute("UPDATE outbox SET status='sending'")
        elif stage == 'partial_cancelled':
            conn.execute("UPDATE outbox SET status='cancelled'")
            conn.execute("UPDATE notification_candidates SET status='expired'")
        frozen = conn.execute('SELECT payload_json FROM outbox').fetchone()[0]
    combined = news(conn, alice, version='combined-a-b', ids=('fact-a', 'fact-b'))
    combined['event_links'][0]['event_facts'][1]['key'] = 'another-metric'
    assert enqueue(conn, combined) == 0
    assert conn.execute('SELECT count(*) FROM notification_candidates').fetchone()[0] == 2
    assert conn.execute('SELECT count(*) FROM notification_acceptances').fetchone()[0] == 0
    if frozen:
        assert conn.execute('SELECT payload_json FROM outbox').fetchone()[0] == frozen


@pytest.mark.parametrize('stage', ['expired', 'cancelled', 'dead_unaccepted', 'old_generation'])
def test_unaccepted_inactive_fact_union_does_not_reserve_new_work(state, stage):
    conn, alice, _, settings = state
    a = news(conn, alice, version='source-a', ids=('fact-a',))
    b = news(conn, alice, version='source-b', ids=('fact-b',))
    a['mode'] = b['mode'] = 'realtime'
    b['event_links'][0]['event_facts'][0]['key'] = 'another-metric'
    enqueue(conn, a)
    enqueue(conn, b)
    if stage == 'expired':
        conn.execute('UPDATE notification_candidates SET expires_at=?', ((NOW - timedelta(seconds=1)).isoformat(),))
    elif stage == 'cancelled':
        conn.execute("UPDATE notification_candidates SET status='cancelled'")
    elif stage == 'dead_unaccepted':
        flush_notifications(settings.database_path, NOW.replace(minute=30))
        conn.execute("UPDATE outbox SET status='dead'")
    else:
        conn.execute('UPDATE notification_candidates SET member_generation=0')
    combined = news(conn, alice, version='combined-a-b', ids=('fact-a', 'fact-b'))
    combined['event_links'][0]['event_facts'][1]['key'] = 'another-metric'
    assert enqueue(conn, combined) == 1


def test_fact_union_is_strictly_member_local(state):
    conn, alice, bob, _ = state
    enqueue(conn, news(conn, alice, version='source-a', ids=('fact-a',)))
    enqueue(conn, news(conn, bob, version='source-b', ids=('fact-b',)))
    combined = news(conn, alice, version='combined-a-b', ids=('fact-a', 'fact-b'))
    assert enqueue(conn, combined) == 1


def legacy_with_metadata(conn, alice):
    """An upgrade adds metadata to the candidate, leaving old outbox untouched."""
    legacy = news(conn, alice)
    for key in ('event_links', 'event_id', 'event_version'):
        legacy.pop(key)
    legacy['article'].update(title='示例公司公告合同编号ORDER-2026-A，订单金额10亿元',
                             summary='公司公告合同编号ORDER-2026-A，新增订单金额10亿元',
                             target_keys=legacy['matched_target_ids'])
    outbox_id = direct(conn, legacy)
    target = MemberConfigStore(conn, alice.id).snapshot().targets[0]
    observed = register_event(conn, _read_article(json.dumps(legacy['article'])),
                              NewsTarget.from_value(target), NOW)
    metadata = deepcopy(legacy)
    metadata.update(event_id='legacy-article-key', event_version='legacy-revision',
                    event_links=[dict(observed.as_link(), target_ids=[target['key']])])
    enqueue(conn, metadata)
    conn.execute("UPDATE notification_candidates SET status='queued',outbox_id=?", (outbox_id,))
    return legacy, metadata, observed


def test_legacy_pending_acceptance_uses_additive_metadata_without_rewriting_text(state):
    conn, alice, _, settings = state
    legacy, metadata, observed = legacy_with_metadata(conn, alice)
    original_text = render_payload(legacy)
    assert conn.execute('SELECT count(*) FROM notification_acceptances').fetchone()[0] == 0
    sender = Sender()
    DeliveryDispatcher(settings, sender).deliver_one(NOW)
    assert sender.accepted == [(original_text, alice.user_id)]
    stored = json.loads(conn.execute('SELECT payload_json FROM outbox').fetchone()[0])
    assert 'event_links' not in stored
    accepted = conn.execute('SELECT * FROM notification_acceptances').fetchone()
    assert accepted['event_id'] == observed.event_id and accepted['complete'] == 1
    assert json.loads(accepted['facts_json']) == list(observed.facts)
    target = MemberConfigStore(conn, alice.id).snapshot().targets[0]
    reprint = deepcopy(legacy)
    reprint['article'].update(key='different-reprint', url='https://example.invalid/reprint',
        title='示例公司新订单10亿元', summary='公司公告合同编号ORDER-2026-A，合同订单金额10亿元')
    repeated = register_event(conn, _read_article(json.dumps(reprint['article'])),
                              NewsTarget.from_value(target), NOW + timedelta(minutes=1))
    assert repeated.event_id == observed.event_id
    assert not should_notify_event(conn, alice.corp_id, alice.id, repeated.as_link())


@pytest.mark.parametrize('mismatch', ['corp_id', 'member_id', 'member_generation', 'touser', 'article_identity', 'article_revision'])
def test_legacy_metadata_cannot_lend_other_scope_or_revision_facts(state, mismatch):
    conn, alice, bob, settings = state
    legacy, metadata, _ = legacy_with_metadata(conn, alice)
    if mismatch == 'article_identity':
        metadata['article']['url'] = 'https://example.invalid/different-original'
    elif mismatch == 'article_revision':
        metadata['article']['summary'] = '公司公告合同编号ORDER-2026-A，新增订单金额12亿元'
    elif mismatch == 'member_id':
        metadata[mismatch] = bob.id
    elif mismatch == 'member_generation':
        metadata[mismatch] += 1
    else:
        metadata[mismatch] = 'another-scope'
    conn.execute('UPDATE notification_candidates SET payload_json=?', (json.dumps(metadata),))
    DeliveryDispatcher(settings, Sender()).deliver_one(NOW)
    assert conn.execute('SELECT status FROM outbox').fetchone()[0] == 'sent'
    assert conn.execute('SELECT count(*) FROM notification_acceptances').fetchone()[0] == 0


def test_legacy_pending_duplicate_is_cancelled_before_remote_request(state):
    conn, alice, _, settings = state
    legacy, metadata, observed = legacy_with_metadata(conn, alice)
    accepted = deepcopy(legacy)
    accepted['event_links'] = metadata['event_links']
    record_delivery_acceptance(conn, accepted, outbox_id=99, now=NOW, complete=True)
    original = conn.execute('SELECT payload_json FROM outbox').fetchone()[0]
    sender = Sender()
    DeliveryDispatcher(settings, sender).deliver_one(NOW)
    row = conn.execute('SELECT status,payload_json FROM outbox').fetchone()
    assert sender.accepted == [] and row['status'] == 'cancelled'
    assert row['payload_json'] == original
