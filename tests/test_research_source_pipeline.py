"""Offline integration of associated news, immutable context and delivery policy."""
from dataclasses import replace
from datetime import timedelta
import json

from stock_watch.analyzer import FakeAnalyzer
from stock_watch.news import NewsTarget
from stock_watch.notifications import flush_notifications
from stock_watch.quality import classify_article, freshness
from stock_watch.research import ResearchStore
from stock_watch.subscription_monitor import _article_dict, _read_article
from stock_watch.subscription_scan import _combine, _task_key
from test_quality_pipeline import NOW, Fetcher, article, candidates, make_state, scan
from test_research import sourced_document


def add_research(conn, *, at=None, revision='initial'):
    at = at or NOW - timedelta(hours=2)
    stamp = at.isoformat()
    doc = sourced_document()
    doc['company'] = {'company_id': 'synthetic-pipeline', 'name': '合成企业'}
    doc['securities'] = [{'symbol': '123456.SH', 'market': 'SH', 'currency': 'CNY'}]
    doc['as_of'] = stamp
    for source in doc['sources']:
        source.update(available_at=stamp, ingested_at=stamp)
    doc['assumptions'] = [{'hypothesis': '产品涨价可支持毛利率' + revision,
                           'support_evidence_ids': ['s'],
                           'counter_evidence': '成本追涨和需求下降可能抵消售价收益',
                           'review_condition': '核对实际售价和毛利率'}]
    doc['monitor_mappings'] = [{'event': '产品价格变动', 'business': '存储业务',
                                'metric': '毛利率', 'transmission': '售价和成本共同决定毛利',
                                'verify': '核对公司实际售价及成本', 'evidence_ids': ['s']}]
    return ResearchStore(conn).append_baseline(doc, stamp)


class Associated(Fetcher):
    security_associated = True

    def __init__(self, items, *, fail_ack=False):
        super().__init__(items)
        self.fail_ack = fail_ack

    def acknowledge(self, conn, now):
        assert conn.in_transaction
        assert conn.execute('SELECT count(*) FROM subscription_articles').fetchone()[0]
        conn.execute("INSERT OR REPLACE INTO monitor_state VALUES ('synthetic-source-watermark','complete',?)",
                     (now.isoformat(),))
        if self.fail_ack:
            raise RuntimeError('synthetic acknowledgement failure')


def test_associated_industry_article_gets_prior_research_and_frozen_candidate(tmp_path):
    path, conn, _, _ = make_state(tmp_path)
    try:
        baseline = add_research(conn)
        a = article('associated-industry', '存储产品价格上行，毛利率仍面临成本压力',
                    summary='成本追涨和需求下降可能抵消产品涨价。')
        analyzer = FakeAnalyzer()
        result = scan(path, Associated([a]), analyzer)
        assert result.analysis_tasks == 1
        context = analyzer.calls[0][0].research_context
        assert context['comparison_available'] is True
        assert context['baseline_ref']['version'] == baseline['version']
        assert '成本追涨' in json.dumps(context, ensure_ascii=False)
        payload = candidates(conn)[0]
        frozen = payload['target_analyses'][0]['research_context']
        assert frozen == context
        stored_context = json.loads(conn.execute("SELECT value FROM monitor_state WHERE key LIKE 'analysis-context-v1:%'").fetchone()[0])
        assert stored_context == context
        assert conn.execute("SELECT value FROM monitor_state WHERE key='synthetic-source-watermark'").fetchone()[0] == 'complete'
    finally:
        conn.close()


def test_failed_archive_and_ack_transaction_does_not_advance_source(tmp_path):
    path, conn, _, _ = make_state(tmp_path)
    try:
        result = scan(path, Associated([article('rollback-source')], fail_ack=True), FakeAnalyzer())
        assert result.status != 'succeeded'
        assert conn.execute('SELECT count(*) FROM subscription_articles').fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM monitor_state WHERE key='synthetic-source-watermark'").fetchone()[0] == 0
    finally:
        conn.close()


def test_research_update_does_not_replay_already_evaluated_news(tmp_path):
    path, conn, _, _ = make_state(tmp_path)
    try:
        add_research(conn)
        analyzer = FakeAnalyzer()
        original = article('already-evaluated')
        assert scan(path, Fetcher([original]), analyzer).analysis_tasks == 1
        count = len(candidates(conn))
        add_research(conn, at=NOW + timedelta(minutes=1), revision='updated')
        assert scan(path, Fetcher([original]), analyzer, now=NOW + timedelta(minutes=2)).analysis_tasks == 0
        assert len(candidates(conn)) == count
    finally:
        conn.close()


def test_research_inputs_are_frozen_even_when_news_does_not_notify(tmp_path):
    path, conn, _, _ = make_state(tmp_path)
    try:
        add_research(conn)
        analyzer = FakeAnalyzer(should_notify=False)
        result = scan(path, Associated([article('no-notification-context')]), analyzer)
        assert result.analysis_tasks == 1 and result.queued == 0
        stored = conn.execute("SELECT value FROM monitor_state WHERE key LIKE 'analysis-context-v1:%'").fetchall()
        assert len(stored) == 1
        assert json.loads(stored[0][0]) == analyzer.calls[0][0].research_context
    finally:
        conn.close()


def test_research_fingerprint_changes_cache_without_changing_report_identity(tmp_path):
    from types import SimpleNamespace
    from stock_watch.subscription_monitor import SubscriptionMonitorService, article_version
    service = SubscriptionMonitorService.__new__(SubscriptionMonitorService)
    service.corp_id, service.source_namespace = 'synthetic-corp', 'eastmoney'
    service.analyzer = FakeAnalyzer()
    member = SimpleNamespace(id=1, analysis_private=False)
    target = NewsTarget('synthetic-target', '合成企业', '合成企业')
    a = replace(article('cache-context'), research_context={'fingerprint': 'baseline-one'})
    b = replace(a, research_context={'fingerprint': 'baseline-two'})
    assert _task_key(service, a, target, member) != _task_key(service, b, target, member)
    assert article_version(a) == article_version(b)


def test_important_market_cause_can_notify_without_claiming_operating_change(tmp_path):
    class MarketAnalyzer(FakeAnalyzer):
        def analyze(self, article, targets):
            return replace(super().analyze(article, targets),
                           analysis_kind='market_context', is_fundamental=False,
                           level='重要', impact='不确定', should_notify=True)
    path, conn, _, _ = make_state(tmp_path)
    try:
        a = article('market-explanation', '半导体午后涨幅扩大，原因涉及风险偏好变化',
                    summary='海外不确定性缓解带来风险偏好变化，尚不能证明公司利润改善。')
        assert classify_article(a).kind == 'market_context'
        result = scan(path, Associated([a]), MarketAnalyzer())
        assert result.queued == 1
        assert candidates(conn)[0]['analysis']['is_fundamental'] is False
        assert flush_notifications(path, NOW + timedelta(minutes=30)) == 1
    finally:
        conn.close()


def test_overnight_cause_retains_morning_review_window():
    morning = NOW.replace(hour=1, minute=5)
    old = article('overnight-market', '半导体涨幅扩大原因分析', summary='风险偏好变化，仍需核实持续性。',
                  published=morning - timedelta(hours=12))
    assert freshness(old, morning).mode == 'morning'


def test_shorter_full_text_refresh_replaces_old_but_unavailable_does_not():
    a = replace(article('refresh-body', summary='旧全文经营数据和大量旧解释' * 20),
                evidence_status='full_text', evidence_checked_at=NOW.isoformat())
    updated = replace(a, summary='更正：订单金额调整为80万元。',
                      evidence_checked_at=(NOW + timedelta(minutes=16)).isoformat())
    assert _combine(a, updated).summary == updated.summary
    assert _combine(updated, replace(a, evidence_status='unavailable', summary='')).summary == updated.summary
    restored = _read_article(json.dumps(_article_dict(updated)))
    assert restored.evidence_checked_at == updated.evidence_checked_at
    assert restored.evidence_status == 'full_text'


def test_remote_analysis_runs_without_database_write_lock(tmp_path):
    from stock_watch.db import connect
    path, conn, _, _ = make_state(tmp_path)
    class LockProbe(FakeAnalyzer):
        def analyze(self, article, targets):
            other = connect(path, busy_timeout_ms=30)
            try:
                other.execute('BEGIN IMMEDIATE')
                other.rollback()
            finally:
                other.close()
            return super().analyze(article, targets)
    try:
        add_research(conn)
        assert scan(path, Associated([article('short-transaction')]), LockProbe()).analysis_tasks == 1
    finally:
        conn.close()
