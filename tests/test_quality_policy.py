"""Synthetic, entirely offline regression coverage for alert quality policy."""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from stock_watch.db import connect, init_db
from stock_watch.news import NewsArticle, NewsTarget, SHANGHAI, normalize_article
from stock_watch.quality import (
    QualityStore, canonical_identity, canonicalize_article, canonical_url,
    classify_article, event_identity, freshness, revision_key,
)


NOW = datetime(2026, 10, 23, 10, tzinfo=SHANGHAI)
TARGET = NewsTarget("example", "示例公司", "示例公司")


def article(**kwargs):
    defaults = dict(key="raw-key", raw_id="12345", title="示例公司公告获得10亿元订单",
                    url="https://news.example/article/12345", published_at=NOW - timedelta(minutes=20),
                    summary="示例公司确认与客户签订订单。", target_keys=(TARGET.key,))
    defaults.update(kwargs)
    return NewsArticle(**defaults)


def test_highlighting_entities_width_and_cjk_spacing_are_one_identity():
    raw = {"title": "示例 <em>公司</em> 公告获得 １０ 亿元订单", "id": "12345",
           "url": article().url + "?utm_source=query#top", "date": NOW.isoformat(),
           "summary": "示例&nbsp;公司 <em>签订</em> 合同。"}
    normalized = normalize_article(raw, TARGET)
    assert normalized.title == article().title
    assert normalized.summary == "示例公司签订合同。"
    assert canonicalize_article(normalized).url == article().url
    assert canonical_identity(normalized) == canonical_identity(article())


def test_url_keeps_business_identity_and_provider_namespaces():
    assert canonical_url("HTTPS://NEWS.EXAMPLE:443/view?id=7&utm_campaign=a#frag") == "https://news.example/view?id=7"
    assert canonical_identity(article(raw_id=None, url="https://news.example/view?id=7")) != canonical_identity(article(raw_id=None, url="https://news.example/view?id=8"))
    assert canonical_identity(article(), "provider-a") != canonical_identity(article(), "provider-b")
    assert canonical_identity(article(source="媒体甲")) == canonical_identity(article(source="媒体乙"))


def test_snippets_media_and_refresh_time_do_not_revive_revision():
    original = article()
    observed = replace(original, summary="另一查询展示合同的不同段落，金额10亿元。", source="另一媒体",
                       url=original.url + "?utm_source=another", published_at=NOW)
    assert revision_key(original) == revision_key(observed)
    assert revision_key(original) != revision_key(replace(original, title="示例公司公告获得11亿元订单"))
    assert revision_key(original) != revision_key(replace(original, summary="公告更正订单金额为11亿元。"))
    assert revision_key(original) != revision_key(replace(original, summary="公司宣布撤销订单。"))


@pytest.mark.parametrize("title", [
    "示例公司获南向资金净买入1亿元", "港股通净卖出示例公司2亿元", "北向资金净流出5亿元",
    "示例公司融资余额1亿元，两融日报", "示例公司融资净偿还200万元", "示例公司股价上涨5%",
    "示例公司收盘成交额2亿元", "示例公司午盘涨幅10%", "示例公司MACD金叉",
    "券商维持示例公司买入评级，目标价20元",
])
def test_confident_pure_market_templates_are_noise(title):
    assert classify_article(article(title=title, summary="")).kind == "market_noise"


@pytest.mark.parametrize("title", [
    "示例公司股价跌停：公司披露业绩亏损", "示例公司股价上涨5%，新增订单10亿元",
    "控股股东计划减持示例公司股份", "示例公司回购股份计划正式实施", "示例公司高管增持股份",
    "芯片行业需求回升，产品价格上涨", "监管部门发布行业出口管制政策", "示例公司重大诉讼进展公告",
    "示例公司融资偿债方案发布", "示例公司下调业绩指引", "示例公司宣布新增产能",
    "示例公司净利润下跌80%", "示例公司收到客户撤销订单通知", "示例公司产品商业化获新进展",
    "DRAM价格上涨10%", "油价上涨5%", "NAND报价下降20%",
])
def test_mixed_operating_and_risk_facts_are_retained(title):
    assert classify_article(article(title=title, summary="")).kind == "fundamental"


def test_unknown_is_not_a_negative_cache():
    assert classify_article(article(title="示例公司新动态", summary="详情待核实")).kind == "unknown"


def test_normal_age_boundary_and_future_quarantine():
    assert freshness(article(published_at=NOW - timedelta(minutes=119)), NOW).mode == "realtime"
    ended = freshness(article(published_at=NOW - timedelta(hours=2)), NOW)
    assert ended.mode == "expired"
    assert ended.expires_at == NOW
    assert freshness(article(published_at=NOW + timedelta(minutes=5, seconds=1)), NOW).mode == "future"
    assert freshness(article(published_at=NOW + timedelta(minutes=5)), NOW).mode == "realtime"


def test_morning_fixed_window_only_operating_facts():
    morning = NOW.replace(hour=9, minute=10)
    overnight = article(published_at=(morning - timedelta(days=1)).replace(hour=15, minute=0))
    result = freshness(overnight, morning)
    assert result.mode == "morning"
    assert result.expires_at == morning.replace(minute=15)
    assert freshness(overnight, morning.replace(minute=15)).mode == "expired"
    assert freshness(replace(overnight, title="南向资金净买入1亿元", summary=""), morning).mode == "expired"
    assert freshness(replace(overnight, published_at=overnight.published_at - timedelta(seconds=1)), morning).mode == "expired"


def test_urgent_exception_requires_new_disclosure_evidence_and_has_24h_limit():
    urgent = article(title="示例公司公告发生债务违约", published_at=NOW - timedelta(hours=15), summary="")
    assert freshness(urgent, NOW).mode == "urgent"
    assert freshness(urgent, NOW).expires_at == urgent.published_at + timedelta(hours=24)
    assert freshness(replace(urgent, published_at=NOW - timedelta(hours=24)), NOW).mode == "expired"
    assert freshness(replace(urgent, title="回顾示例公司债务违约"), NOW).mode == "expired"
    assert freshness(urgent, NOW.replace(hour=9, minute=10)).mode == "urgent"


@pytest.mark.parametrize("value", [None, "unparseable"])
def test_missing_source_time_is_auditable_not_fresh(value):
    unknown = normalize_article({"id": "1", "title": "示例公司订单公告", "url": article().url, "date": value}, TARGET)
    assert unknown.time_status == "unknown"
    result = freshness(unknown, NOW)
    assert result.mode == "unknown" and result.expires_at is None


def test_conflicting_source_time_fields_are_not_fresh():
    ambiguous = normalize_article({"id": "1", "title": "示例公司订单公告", "url": article().url,
                                   "date": NOW.isoformat(), "publishTime": (NOW - timedelta(days=1)).isoformat()}, TARGET)
    assert ambiguous.time_status == "conflict"
    assert ambiguous.source_published_at is not None
    assert freshness(ambiguous, NOW).mode == "unknown"


def test_store_aliases_merges_targets_and_preserves_observations_first_seen():
    conn = connect(":memory:")
    init_db(conn)
    store = QualityStore(conn)
    initial = store.observe(article(), NOW)
    later = store.observe(article(raw_id=None, target_keys=("theme",), summary="不同查询片段"), NOW + timedelta(minutes=3))
    assert initial.key == later.key
    assert later.target_keys == ("example", "theme")
    assert later.first_seen_at == NOW
    assert later.last_seen_at == NOW + timedelta(minutes=3)
    assert conn.execute("SELECT count(*) FROM quality_article_observations").fetchone()[0] == 2
    assert store.get_article_times(initial.key)["first_seen_at"] == NOW.isoformat()
    assert not conn.in_transaction


def test_store_timestamp_refresh_is_quarantined_and_cannot_extend_lifetime():
    conn = connect(":memory:")
    init_db(conn)
    store = QualityStore(conn)
    initial = store.observe(article(published_at=NOW - timedelta(hours=20)), NOW)
    later = store.observe(article(published_at=NOW), NOW + timedelta(minutes=5))
    assert later.published_at == initial.published_at
    assert later.time_status == "conflict"
    assert freshness(later, NOW).mode == "unknown"
    again = store.observe(article(published_at=initial.published_at), NOW + timedelta(minutes=6))
    assert again.time_status == "conflict"


def test_store_provider_namespace_prevents_cross_provider_id_collision():
    conn = connect(":memory:")
    init_db(conn)
    store = QualityStore(conn)
    a = store.observe(article(), NOW, namespace="a")
    b = store.observe(article(), NOW, namespace="b")
    assert a.key != b.key


def test_store_does_not_commit_or_rollback_the_callers_transaction():
    conn = connect(":memory:")
    init_db(conn)
    store = QualityStore(conn)
    conn.execute("BEGIN IMMEDIATE")
    store.observe(article(), NOW)
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT count(*) FROM quality_articles").fetchone()[0] == 0


def test_actual_correction_has_a_new_disclosure_lifetime():
    conn = connect(":memory:")
    init_db(conn)
    store = QualityStore(conn)
    old = article(published_at=NOW - timedelta(days=1))
    store.observe(old, NOW)
    corrected = store.observe(replace(old, title="示例公司公告更正订单金额为11亿元", published_at=NOW), NOW)
    assert corrected.time_status == "trusted"
    assert corrected.source_published_at == NOW
    assert freshness(corrected, NOW).mode == "realtime"
    assert store.get_article_times(corrected.key)["first_seen_at"] == NOW.isoformat()


def test_excerpt_variants_cannot_roll_back_an_accepted_correction_after_restart():
    conn = connect(":memory:")
    init_db(conn)
    store = QualityStore(conn)
    original = article(summary="公司披露订单金额为10亿元。")
    first = store.observe(original, NOW)
    corrected_raw = replace(original, summary="公司公告更正订单金额由10亿元改为8亿元，其余合同条款继续有效。",
                            published_at=NOW+timedelta(minutes=10))
    corrected = store.observe(corrected_raw, NOW+timedelta(minutes=10))
    assert revision_key(corrected) != revision_key(first)
    assert corrected.source_published_at == corrected_raw.published_at
    excerpts = [
        replace(original, summary="公司继续履行合同，其余条款保持有效。"),
        replace(corrected_raw, summary="订单金额更正为8亿元…"),
        replace(corrected_raw, summary="公告更正订单金额为80000万元，较原10亿元下调。"),
        replace(corrected_raw, summary="更正：订单金额为8亿…"),
        replace(original, summary="公司披露订单金额为10亿元。"),
    ]
    for index, excerpt in enumerate(excerpts, start=11):
        # A process restart must not discard the accepted representative.
        observed = QualityStore(conn).observe(excerpt, NOW+timedelta(minutes=index))
        assert revision_key(observed) == revision_key(corrected)
        assert observed.summary == corrected.summary
        assert observed.time_status == "trusted"
        assert observed.source_published_at == corrected_raw.published_at
    originals = [row[0] for row in conn.execute("SELECT summary FROM quality_article_observations ORDER BY id")]
    assert originals == [original.summary, corrected_raw.summary, *(item.summary for item in excerpts)]


def test_successive_real_amount_corrections_each_receive_a_new_revision():
    conn = connect(":memory:")
    init_db(conn)
    store = QualityStore(conn)
    original = article(summary="披露订单金额为10亿元。")
    initial = store.observe(original, NOW)
    eight = store.observe(replace(original, summary="公告更正：订单金额为8亿元。"), NOW+timedelta(minutes=1))
    six = store.observe(replace(original, summary="公司再次更正订单金额为6亿元。"), NOW+timedelta(minutes=2))
    assert len({revision_key(item) for item in (initial, eight, six)}) == 3
    stale_eight = store.observe(replace(original, summary="公告更正：订单金额为8亿元。"), NOW+timedelta(minutes=3))
    assert revision_key(stale_eight) == revision_key(six)
    assert stale_eight.summary == six.summary
    assert conn.execute("SELECT count(*) FROM quality_article_revisions").fetchone()[0] == 3


@pytest.mark.parametrize('time_status', ['unknown', 'conflict'])
def test_changed_revision_cannot_borrow_prior_time_or_replace_trusted_representative(time_status):
    conn = connect(':memory:')
    init_db(conn)
    original = article(summary='公司公告更正订单金额为8亿元。')
    accepted = QualityStore(conn).observe(original, NOW)
    changed = replace(original, summary='公司再次更正订单金额为6亿元。', time_status=time_status,
                      source_published_at=None,
                      published_at=NOW if time_status == 'conflict' else datetime(1970, 1, 1, tzinfo=SHANGHAI))
    pending = QualityStore(conn).observe(changed, NOW + timedelta(minutes=1))
    assert revision_key(pending) != revision_key(accepted)
    assert pending.time_status == time_status and freshness(pending, NOW).mode == 'unknown'
    assert pending.source_published_at == (changed.published_at if time_status == 'conflict' else None)
    current = conn.execute('SELECT * FROM quality_articles').fetchone()
    assert current['revision_key'] == revision_key(accepted) and current['time_status'] == 'trusted'
    assert current['source_published_at'] == accepted.source_published_at.isoformat()
    assert conn.execute('SELECT count(*) FROM quality_article_revisions').fetchone()[0] == 1
    observation = conn.execute('SELECT * FROM quality_article_observations ORDER BY id DESC LIMIT 1').fetchone()
    assert observation['summary'] == changed.summary and observation['time_status'] == time_status
    assert observation['revision_key'] == revision_key(changed)
    # This was pending evidence, not a historical accepted correction: once its
    # date is verified it must be able to become the new representative.
    verified = QualityStore(conn).observe(replace(changed, time_status='trusted', published_at=NOW), NOW + timedelta(minutes=2))
    assert revision_key(verified) == revision_key(changed)
    assert verified.time_status == 'trusted' and verified.source_published_at == NOW
    assert freshness(verified, NOW).mode == 'realtime'
    conn.close()


def test_same_trusted_revision_with_missing_time_reuses_fixed_disclosure_evidence():
    conn = connect(':memory:')
    init_db(conn)
    original = article(summary='公司公告更正订单金额为8亿元。')
    accepted = QualityStore(conn).observe(original, NOW)
    missing = replace(original, time_status='unknown', source_published_at=None,
                      published_at=datetime(1970, 1, 1, tzinfo=SHANGHAI))
    observed = QualityStore(conn).observe(missing, NOW + timedelta(minutes=10))
    assert revision_key(observed) == revision_key(accepted)
    assert observed.time_status == 'trusted' and observed.source_published_at == accepted.source_published_at
    assert freshness(observed, NOW).expires_at == freshness(accepted, NOW).expires_at
    assert conn.execute('SELECT time_status FROM quality_article_observations ORDER BY id DESC LIMIT 1').fetchone()[0] == 'unknown'
    conn.close()


@pytest.mark.parametrize("summary", ["已完成", "公司更正相关资料…", "取消", "公告更正订单金额为…", "公司尚未取消订单",
                                     "更正前订单金额为10亿元", "公司尚未更正订单金额为8亿元", "公司拟更正订单金额为8亿元"])
def test_update_words_without_a_completed_fact_do_not_create_revisions(summary):
    assert revision_key(article(summary=summary)) == revision_key(article(summary="普通查询摘录"))


def test_cancellation_excerpt_wording_is_stable_and_remains_a_revision():
    initial = article(summary="公司已签订订单。")
    cancelled = replace(initial, summary="公司公告撤销订单，其余业务正常。")
    truncated = replace(initial, summary="该订单已取消…")
    assert revision_key(cancelled) != revision_key(initial)
    assert revision_key(cancelled) == revision_key(truncated)


def test_representative_recovers_from_existing_observations_without_revision_rows():
    conn = connect(":memory:")
    init_db(conn)
    corrected = QualityStore(conn).observe(article(summary="公司更正订单金额为8亿元。"), NOW)
    conn.execute("DELETE FROM quality_article_revisions")
    later = QualityStore(conn).observe(article(summary="不同查询的普通片段"), NOW+timedelta(minutes=1))
    assert revision_key(later) == revision_key(corrected)
    assert later.summary == corrected.summary


def test_event_reprint_merges_only_exact_evidence_and_distinct_facts_survive():
    original = article()
    reprint = replace(original, raw_id="999", url="https://another.example/999", title="快讯：示例公司公告获得10亿元订单！")
    assert event_identity(original, TARGET) == event_identity(reprint, TARGET)
    assert event_identity(original, TARGET)[0] != event_identity(replace(original, title="示例公司公告获得11亿元订单"), TARGET)[0]
    assert event_identity(original, TARGET)[0] != event_identity(replace(original, title="示例公司公告回购10亿元股份"), TARGET)[0]
    assert event_identity(original, TARGET)[0] != event_identity(replace(original, published_at=NOW - timedelta(days=1)), TARGET)[0]


def test_unknown_or_implicit_subject_events_are_article_scoped():
    a = article(title="行业产能变化")
    b = replace(a, raw_id="different", url="https://another.example/other")
    assert event_identity(a, TARGET)[0] != event_identity(b, TARGET)[0]


def test_schema_four_is_idempotent_and_keeps_existing_config_evidence():
    conn = connect(":memory:")
    init_db(conn)
    conn.execute("INSERT INTO seen_articles(article_key,seen_at) VALUES('old','2026-01-01')")
    config = conn.execute("SELECT * FROM config_state").fetchone()
    conn.execute("PRAGMA user_version=3")
    init_db(conn)
    init_db(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    assert tuple(config) == tuple(conn.execute("SELECT * FROM config_state").fetchone())
    assert conn.execute("SELECT article_key FROM seen_articles").fetchone()[0] == "old"
    assert conn.execute("SELECT 1 FROM quality_article_observations").fetchone() is None
