"""Offline replay of event facts through scanning, acceptance and migration."""
from dataclasses import replace
from datetime import timedelta
import json

from stock_watch.analyzer import FakeAnalyzer
from stock_watch.analyzer import build_analysis_prompt, build_batch_prompt
from stock_watch.delivery import DeliveryDispatcher
from stock_watch.notifications import flush_notifications
from stock_watch.settings import Settings
from stock_watch.subscription_scan import prepare_history
from test_quality_pipeline import NOW, CORP, Fetcher, article, make_state, scan


class MajorAnalyzer(FakeAnalyzer):
    def analyze(self, item, targets):
        return replace(super().analyze(item, targets), level='重大')


class Sender:
    def __init__(self):
        self.sent = []

    def send_chunk(self, text, to_user=None):
        self.sent.append(to_user)


def deliver(path, *, now=NOW):
    sender = Sender()
    dispatcher = DeliveryDispatcher(Settings(data_dir=path.parent, database_path=path,
        wecom_corp_id=CORP), sender)
    for _ in range(10):
        if not dispatcher.deliver_one(now=now):
            break
    return sender


def report(key, *, title='合成企业2026年三季报公告：营收100亿元', summary='', when=NOW):
    return article(key, title, summary=summary, published=when)


def test_cross_title_reprints_have_one_pending_candidate_and_no_acceptance(tmp_path):
    path, conn, _, _ = make_state(tmp_path)
    try:
        analyzer = MajorAnalyzer()
        original = report('original')
        assert scan(path, Fetcher([original]), analyzer).queued == 1
        assert conn.execute('SELECT count(*) FROM notification_acceptances').fetchone()[0] == 0
        reprint = report('reprint', title='合成企业披露2026年前三季度营业收入100亿元', when=NOW+timedelta(minutes=5))
        assert scan(path, Fetcher([reprint]), analyzer, now=NOW+timedelta(minutes=5)).queued == 0
        assert conn.execute('SELECT count(*) FROM notification_candidates').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM business_events WHERE strong_identity=1').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM business_event_evidence').fetchone()[0] >= 2
    finally:
        conn.close()


def test_each_member_gets_first_fact_then_reprint_and_subset_are_suppressed(tmp_path):
    path, conn, _, members = make_state(tmp_path, users=('synthetic-alice', 'synthetic-bob'))
    try:
        analyzer = MajorAnalyzer()
        first = report('financial-release', summary='毛利率30%。')
        assert scan(path, Fetcher([first]), analyzer).queued == 2
        sent = deliver(path)
        assert set(sent.sent) == {member.user_id for member in members}
        assert conn.execute('SELECT count(*) FROM notification_acceptances WHERE complete=1').fetchone()[0] == 2
        short = report('other-media', title='合成企业2026年前三季度营业收入100亿元', when=NOW+timedelta(minutes=6))
        assert scan(path, Fetcher([short]), analyzer, now=NOW+timedelta(minutes=6)).queued == 0
        assert deliver(path, now=NOW+timedelta(minutes=6)).sent == []
    finally:
        conn.close()


def test_same_title_new_metric_is_reassessed_without_erasing_prior_receipt(tmp_path):
    path, conn, _, _ = make_state(tmp_path)
    try:
        analyzer = MajorAnalyzer()
        original = report('stable-url')
        scan(path, Fetcher([original]), analyzer)
        deliver(path)
        before = conn.execute('SELECT facts_json FROM notification_acceptances').fetchone()[0]
        detail = replace(original, summary='毛利率30%。', published_at=NOW+timedelta(minutes=5))
        outcome = scan(path, Fetcher([detail]), analyzer, now=NOW+timedelta(minutes=5))
        assert outcome.analysis_tasks == 1 and outcome.queued == 1
        assert conn.execute('SELECT facts_json FROM notification_acceptances').fetchone()[0] == before
        assert len(deliver(path, now=NOW+timedelta(minutes=5)).sent) == 1
        assert conn.execute('SELECT count(*) FROM notification_acceptances WHERE complete=1').fetchone()[0] == 2
    finally:
        conn.close()


def test_analysis_gets_structured_increment_separate_from_old_major_fact(tmp_path):
    path, conn, _, _ = make_state(tmp_path)
    try:
        analyzer = MajorAnalyzer()
        original = report('delta-source')
        scan(path, Fetcher([original]), analyzer)
        deliver(path)
        detail = replace(original, summary='毛利率30%。', published_at=NOW+timedelta(minutes=5))
        scan(path, Fetcher([detail]), analyzer, now=NOW+timedelta(minutes=5))
        analyzed, targets = analyzer.calls[-1]
        assert analyzed.fact_context is not None
        changes = analyzed.fact_context['changes']
        assert {item['after']['metric'] for item in changes} == {'gross_margin'}
        prompt = build_analysis_prompt(analyzed, targets)
        assert 'fact_context' in prompt and '旧重大事实' in prompt
        batch = build_batch_prompt([('delta-task', analyzed, targets)])
        assert 'fact_context' in batch and 'gross_margin' in batch
        # Context is derived, not a mutation of archived source material.
        archived = [json.loads(row[0]) for row in conn.execute('SELECT article_json FROM subscription_articles')]
        assert all('fact_context' not in item for item in archived)
    finally:
        conn.close()


def test_different_report_period_and_buyback_plan_never_merge(tmp_path):
    path, conn, _, _ = make_state(tmp_path)
    try:
        items = [report('q1', title='合成企业2026年一季报公告营收100亿元'),
                 report('q3'),
                 report('plan-a', title='合成企业公告回购计划编号PLAN-A：拟回购1亿元'),
                 report('plan-b', title='合成企业公告回购计划编号PLAN-B：拟回购1亿元')]
        outcome = scan(path, Fetcher(items), MajorAnalyzer())
        assert outcome.queued == 4
        assert conn.execute('SELECT count(DISTINCT event_id) FROM notification_candidates').fetchone()[0] == 4
    finally:
        conn.close()


def test_old_fact_reprint_cannot_borrow_new_publication_window(tmp_path):
    path, conn, _, _ = make_state(tmp_path)
    try:
        old = report('old', when=NOW-timedelta(hours=5))
        assert scan(path, Fetcher([old]), MajorAnalyzer()).queued == 0
        reprint = report('new-url', title='合成企业2026年前三季度公告营业收入100亿元')
        assert scan(path, Fetcher([reprint]), MajorAnalyzer(), now=NOW+timedelta(minutes=1)).queued == 0
        assert conn.execute('SELECT count(*) FROM notification_candidates').fetchone()[0] == 0
    finally:
        conn.close()


def test_schema4_upgrade_uses_actual_acceptance_and_leaves_outbox_and_config_unchanged(tmp_path):
    path, conn, _, _ = make_state(tmp_path)
    try:
        scan(path, Fetcher([report('old-sent')]), MajorAnalyzer())
        deliver(path)
        scan(path, Fetcher([report('pending-period', title='合成企业2026年一季报公告营收80亿元')]), MajorAnalyzer(), now=NOW+timedelta(minutes=1))
        before = [tuple(row) for row in conn.execute('SELECT id,status,payload_json FROM outbox ORDER BY id')]
        configs = [tuple(row) for row in conn.execute('SELECT * FROM member_configs')]
        # Model an installed schema4 DB whose old migration marker already ran.
        conn.execute('DELETE FROM notification_acceptances')
        conn.execute("DELETE FROM app_meta WHERE key LIKE 'business_event_receipts_v1:%'")
        conn.execute('INSERT OR REPLACE INTO app_meta VALUES(?,?)', ('independent_subscriptions', CORP))
        prepare_history(conn, NOW+timedelta(minutes=2))
        receipts = conn.execute('SELECT event_id,complete FROM notification_acceptances').fetchall()
        assert receipts and all(row['complete'] for row in receipts)
        assert len({row['event_id'] for row in receipts}) == 1
        assert before == [tuple(row) for row in conn.execute('SELECT id,status,payload_json FROM outbox ORDER BY id')]
        assert configs == [tuple(row) for row in conn.execute('SELECT * FROM member_configs')]
        count = conn.execute('SELECT count(*) FROM notification_acceptances').fetchone()[0]
        prepare_history(conn, NOW+timedelta(minutes=3))
        assert conn.execute('SELECT count(*) FROM notification_acceptances').fetchone()[0] == count
    finally:
        conn.close()


def test_legacy_pending_outbox_gets_metadata_not_a_false_receipt(tmp_path):
    path, conn, _, _ = make_state(tmp_path)
    try:
        analyzer = MajorAnalyzer()
        scan(path, Fetcher([report('legacy-pending')]), analyzer)
        row = conn.execute('SELECT id,payload_json FROM outbox').fetchone()
        old = json.loads(row['payload_json'])
        old.pop('event_links')
        old['event_id'], old['event_version'] = 'legacy-article', 'legacy-v1'
        frozen = json.dumps(old)
        conn.execute('UPDATE outbox SET payload_json=? WHERE id=?', (frozen, row['id']))
        conn.execute('DELETE FROM notification_candidates')
        conn.execute("DELETE FROM app_meta WHERE key LIKE 'business_event_receipts_v1:%'")
        conn.execute('INSERT OR REPLACE INTO app_meta VALUES(?,?)', ('independent_subscriptions', CORP))
        prepare_history(conn, NOW+timedelta(minutes=1))
        assert conn.execute('SELECT count(*) FROM notification_acceptances').fetchone()[0] == 0
        metadata = json.loads(conn.execute('SELECT payload_json FROM notification_candidates').fetchone()[0])
        assert metadata['event_links'][0]['strong_identity']
        assert conn.execute('SELECT payload_json FROM outbox').fetchone()[0] == frozen
        assert len(deliver(path, now=NOW+timedelta(minutes=2)).sent) == 1
        assert conn.execute('SELECT count(*) FROM notification_acceptances WHERE complete=1').fetchone()[0] >= 1
        reprint = report('after-old-delivery', title='合成企业披露2026年前三季度营业收入100亿元', when=NOW+timedelta(minutes=3))
        assert scan(path, Fetcher([reprint]), analyzer, now=NOW+timedelta(minutes=3)).queued == 0
    finally:
        conn.close()
