"""Offline integration: late target impacts enrich only untouched messages."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo

import pytest

from stock_watch.analyzer import FakeAnalyzer
from stock_watch.db import connect, init_db
from stock_watch.news import NewsArticle
from stock_watch.notifications import flush_notifications
from stock_watch.subscriptions import MemberConfigStore, MemberRegistry
from stock_watch.subscription_monitor import SubscriptionMonitorService


NOW = datetime(2026, 10, 23, 2, 1, tzinfo=timezone.utc)
CORP = 'target-merge-offline'


@pytest.fixture
def state(tmp_path):
    path = tmp_path / 'target-merge.sqlite'
    conn = connect(path)
    init_db(conn)
    registry = MemberRegistry(conn, CORP)
    member = registry.add('alice')
    store = MemberConfigStore(conn, member.id, clock=lambda: NOW - timedelta(hours=1))
    for symbol, name in [('123456.SH', '合成企业'), ('654321.SH', '另一企业')]:
        store.apply_operation({'op': 'add_stock', 'symbol': symbol, 'name': name, 'focus': '订单'}, actor='alice')
    yield path, conn, registry, member
    conn.close()


class Fetcher:
    def __init__(self, *, second=False, second_key='shared-report'):
        self.second, self.second_key = second, second_key

    def fetch_target(self, target, **kwargs):
        if target.name == '另一企业' and not self.second:
            return []
        key = self.second_key if target.name == '另一企业' else 'shared-report'
        return [NewsArticle(key=key, raw_id=key, title='合成企业公告获得新订单100万元',
                            summary='新合同订单支持收入增长。', url=f'https://example.invalid/{key}',
                            published_at=NOW, target_keys=(target.key,))]


def scan(path, fetcher, analyzer, *, now=NOW):
    return SubscriptionMonitorService(path, fetcher, analyzer, corp_id=CORP,
        environ={'MONITOR_ENABLED': 'false'}).run_once(now=now, force=True)


def candidate(conn):
    return json.loads(conn.execute('SELECT payload_json FROM notification_candidates ORDER BY id').fetchone()[0])


@pytest.mark.parametrize('queued', [False, True])
def test_late_target_enriches_one_pending_or_queued_fact(state, queued):
    path, conn, _, _ = state
    analyzer = FakeAnalyzer()
    assert scan(path, Fetcher(), analyzer).queued == 1
    if queued:
        assert flush_notifications(path, NOW.replace(minute=30)) == 1
    result = scan(path, Fetcher(second=True), analyzer, now=NOW.replace(minute=31) if queued else NOW + timedelta(minutes=1))
    assert result.analysis_tasks == 1 and len(analyzer.calls) == 2
    assert conn.execute('SELECT count(*) FROM notification_candidates').fetchone()[0] == 1
    value = json.loads(conn.execute('SELECT payload_json FROM outbox').fetchone()[0])['items'][0] if queued else candidate(conn)
    assert len(value['target_analyses']) == 2
    assert set(value['target_names']) == {'合成企业', '另一企业'}
    assert set(value['matched_target_ids']) == {item['target_id'] for item in value['target_analyses']}


def test_exact_reprint_new_target_merges_original_representative(state):
    path, conn, _, _ = state
    analyzer = FakeAnalyzer()
    scan(path, Fetcher(), analyzer)
    result = scan(path, Fetcher(second=True, second_key='other-outlet'), analyzer, now=NOW + timedelta(minutes=1))
    assert result.analysis_tasks == 1 and len(analyzer.calls) == 2
    assert conn.execute('SELECT count(*) FROM notification_candidates').fetchone()[0] == 1
    assert len(candidate(conn)['target_analyses']) == 2
    assert candidate(conn)['article']['url'] == 'https://example.invalid/shared-report'


@pytest.mark.parametrize('status,accepted', [('pending', 1), ('sent', 1), ('sending', 0)])
def test_started_notification_keeps_original_content_and_still_saves_new_target_analysis(state, status, accepted):
    path, conn, _, _ = state
    analyzer = FakeAnalyzer()
    scan(path, Fetcher(), analyzer)
    flush_notifications(path, NOW.replace(minute=30))
    original = json.loads(conn.execute('SELECT payload_json FROM outbox').fetchone()[0])
    original.update(accepted_chunks=accepted, delivery_chunks=['original accepted', 'original pending'])
    conn.execute('UPDATE outbox SET status=?,payload_json=?', (status, json.dumps(original)))
    result = scan(path, Fetcher(second=True), analyzer, now=NOW.replace(minute=31))
    assert result.analysis_tasks == 1 and len(analyzer.calls) == 2
    assert conn.execute('SELECT count(*) FROM notification_candidates').fetchone()[0] == 1
    assert json.loads(conn.execute('SELECT payload_json FROM outbox').fetchone()[0]) == original
    assert conn.execute('SELECT count(*) FROM member_evaluations').fetchone()[0] == 2


class PartialAnalyzer(FakeAnalyzer):
    def __init__(self, *, major=False):
        super().__init__()
        self.fail = True
        self.major = major
        self.major_name = '合成企业'

    def analyze(self, article, targets):
        selected = tuple(targets)
        if selected[0].name == '另一企业' and self.fail:
            raise TimeoutError('offline synthetic failure')
        result = super().analyze(article, selected)
        return replace(result, level='重大') if self.major and selected[0].name == self.major_name else result


@pytest.mark.parametrize('major', [False, True])
def test_one_failed_target_cannot_block_successful_target_and_recovery_enriches(state, major):
    path, conn, _, _ = state
    analyzer = PartialAnalyzer(major=major)
    result = scan(path, Fetcher(second=True), analyzer)
    assert result.status == 'partial' and result.queued == 1
    assert conn.execute('SELECT count(*) FROM member_evaluations').fetchone()[0] == 1
    assert len(candidate(conn)['target_analyses']) == 1
    if major:
        assert conn.execute('SELECT mode FROM notification_candidates').fetchone()[0] == 'urgent'
        assert conn.execute('SELECT count(*) FROM outbox').fetchone()[0] == 1
    analyzer.fail = False
    result = scan(path, Fetcher(second=True), analyzer, now=NOW + timedelta(minutes=6))
    assert result.analysis_tasks == 1 and conn.execute('SELECT count(*) FROM member_evaluations').fetchone()[0] == 2
    assert conn.execute('SELECT count(*) FROM notification_candidates').fetchone()[0] == 1
    value = json.loads(conn.execute('SELECT payload_json FROM outbox').fetchone()[0]) if major else candidate(conn)
    assert len(value['target_analyses']) == 2
    assert value['analysis']['level'] == ('重大' if major else '重要')


def test_member_generation_cannot_enrich_prior_membership_candidate(state):
    path, conn, registry, member = state
    analyzer = PartialAnalyzer()
    scan(path, Fetcher(second=True), analyzer)
    before = candidate(conn)
    registry.set_analysis_private('alice', True)
    analyzer.fail = False
    result = scan(path, Fetcher(second=True), analyzer, now=NOW + timedelta(minutes=6))
    assert result.analysis_tasks == 1
    assert candidate(conn) == before
    assert conn.execute('SELECT count(*) FROM notification_candidates').fetchone()[0] == 1
    assert conn.execute('SELECT count(*) FROM member_evaluations').fetchone()[0] == 2


@pytest.mark.parametrize('queued', [False, True])
def test_recovered_major_target_promotes_pending_or_unaccepted_digest_without_extending_expiry(state, queued):
    path, conn, _, _ = state
    analyzer = PartialAnalyzer(major=True)
    analyzer.major_name = '另一企业'
    scan(path, Fetcher(second=True), analyzer)
    expiry = conn.execute('SELECT expires_at FROM notification_candidates').fetchone()[0]
    if queued:
        assert flush_notifications(path, NOW.replace(minute=30)) == 1
    analyzer.fail = False
    scan(path, Fetcher(second=True), analyzer, now=NOW.replace(minute=31) if queued else NOW + timedelta(minutes=6))
    rows = conn.execute('SELECT status,payload_json FROM outbox ORDER BY id').fetchall()
    active = [json.loads(row['payload_json']) for row in rows if row['status'] != 'cancelled']
    assert len(active) == 1 and active[0]['event'] == 'subscription.news'
    assert active[0]['mode'] == 'urgent' and active[0]['analysis']['level'] == '重大'
    assert active[0]['expires_at'] == expiry and len(active[0]['target_analyses']) == 2
    assert conn.execute('SELECT mode,expires_at FROM notification_candidates').fetchone()['mode'] == 'urgent'


def test_major_extraction_keeps_other_digest_items_and_existing_delivery_window(state):
    from stock_watch.notifications import enqueue_candidate
    from stock_watch.delivery import DeliveryDispatcher
    from stock_watch.settings import Settings
    path, conn, _, _ = state
    analyzer = PartialAnalyzer(major=True)
    analyzer.major_name = '另一企业'
    scan(path, Fetcher(second=True), analyzer)
    other = candidate(conn)
    other.update(event_id='independent-order', event_version='v1')
    other['article'] = dict(other['article'], key='independent', title='合成企业新增另一项目订单200万元')
    enqueue_candidate(conn, other, article_key='independent', event_id='independent-order', event_version='v1',
                      mode='realtime', expires_at=other['expires_at'], now=NOW)
    flush_notifications(path, NOW.replace(minute=30))
    # A different digest already used this actual-send slot. The newly major
    # event must be immediate, while the retained routine item obeys cadence.
    conn.execute("INSERT INTO notification_windows(corp_id,member_id,kind,window_key,outbox_id,created_at) VALUES(?,?,'delivery_realtime',?,?,?)",
                 (other['corp_id'], other['member_id'], NOW.replace(minute=30, second=0, microsecond=0).astimezone(ZoneInfo('Asia/Shanghai')).isoformat(), 999, NOW.isoformat()))
    analyzer.fail = False
    scan(path, Fetcher(second=True), analyzer, now=NOW.replace(minute=31))
    rows = conn.execute('SELECT payload_json FROM outbox ORDER BY id').fetchall()
    digest, urgent = [json.loads(row[0]) for row in rows]
    assert len(digest['items']) == 1 and digest['items'][0]['event_id'] == 'independent-order'
    assert urgent['event'] == 'subscription.news' and urgent['mode'] == 'urgent'
    assert len(urgent['target_analyses']) == 2
    sent = []
    class Sender:
        def send_chunk(self, text, to_user=None):
            sent.append((text, to_user))
    settings = Settings(data_dir=path.parent, database_path=path, wecom_corp_id=CORP)
    dispatcher = DeliveryDispatcher(settings, Sender())
    assert dispatcher.deliver_one(now=NOW.replace(minute=31))
    assert dispatcher.deliver_one(now=NOW.replace(minute=31))
    assert len(sent) == 1 and sent[0][1] == 'alice'
    assert [row[0] for row in conn.execute('SELECT status FROM outbox ORDER BY id')] == ['pending', 'sent']
