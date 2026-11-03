"""Synthetic offline evidence replay for finite business-event templates."""
from dataclasses import replace
from datetime import datetime, timedelta
import json

import pytest

from stock_watch.db import connect, init_db
from stock_watch.events import extract_facts, register_event, strong_identity
from stock_watch.news import NewsArticle, NewsTarget, SHANGHAI
from stock_watch.quality import QualityStore, event_identity, revision_key


NOW = datetime(2026, 11, 3, 10, tzinfo=SHANGHAI)
TARGET = NewsTarget("123456.SH", "合成公司", "合成公司")


def article(key="a", title="合成公司公告2026年前三季度业绩", summary="营业收入100亿元。", **kwargs):
    return NewsArticle(key=key, raw_id=key, title=title, summary=summary,
                       url=f"https://example.invalid/{key}", published_at=kwargs.pop("published_at", NOW), **kwargs)


@pytest.fixture
def conn():
    value = connect(":memory:")
    init_db(value)
    yield value
    value.close()


def test_cross_title_and_cross_day_earnings_share_report_event_and_fact(conn):
    original = register_event(conn, article(), TARGET, NOW)
    later = register_event(conn, article("b", "合成公司2026年三季报公告：营收稳健", "营收1000000万元。", published_at=NOW+timedelta(days=1)), TARGET, NOW+timedelta(days=1))
    assert original.strong_identity and later.strong_identity
    assert original.event_id == later.event_id
    assert original.change_id == later.change_id
    assert original.facts[0]["value"] == "10000000000"
    assert later.first_public_at == NOW
    assert later.observation_kind == "duplicate"
    assert later.changes[0]["kind"] == "new_fact"
    assert conn.execute("SELECT count(*) FROM business_event_evidence").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM business_event_facts").fetchone()[0] == 1


def test_buyback_plan_reference_merges_progress_but_separate_plans_do_not(conn):
    def buyback(key, date, tail):
        return article(key, "合成公司回购进展公告", f"根据{date}披露的回购计划。{tail}")
    plan = register_event(conn, buyback("a", "2026年10月20日", "拟回购金额2亿元。"), TARGET, NOW)
    progress = register_event(conn, buyback("b", "2026年10月20日", "回购方案正式实施。"), TARGET, NOW)
    other = register_event(conn, buyback("c", "2026年11月2日", "拟回购金额2亿元。"), TARGET, NOW)
    assert plan.event_id == progress.event_id != other.event_id
    assert progress.facts[0]["status"] == "implemented"
    assert progress.change_id != plan.change_id


@pytest.mark.parametrize("first,second", [
    ("合成公司公告2026年前三季度业绩", "合成公司公告2026年第三季度业绩"),
    ("合成公司公告2026年上半年业绩", "合成公司公告2025年上半年业绩"),
    ("合成公司订单公告", "合成公司订单公告"),
])
def test_periods_and_unsupported_unanchored_orders_do_not_merge(first, second):
    a = article("a", first)
    b = article("b", second)
    assert event_identity(a, TARGET)[0] != event_identity(b, TARGET)[0] if first != second else strong_identity(a, TARGET) is None


def test_distinct_contract_identifiers_are_separate_even_same_amount(conn):
    a = article("a", "合成公司订单公告", "合同编号：ORDER-001。订单金额100万元。")
    b = article("b", "合成公司订单公告", "合同编号：ORDER-002。订单金额100万元。")
    assert register_event(conn, a, TARGET, NOW).event_id != register_event(conn, b, TARGET, NOW).event_id


def test_identity_ignores_model_explanation_and_rejects_implicit_subject():
    assert event_identity(article(), TARGET, {"event_type": "random"}) == event_identity(article(), TARGET)
    other = NewsTarget("other", "其他公司", "其他公司")
    assert strong_identity(article(), other) is None


def test_new_metric_is_same_event_new_fact_with_own_time(conn):
    before = register_event(conn, article(), TARGET, NOW)
    added = article("b", summary="毛利率为30%。", published_at=NOW+timedelta(hours=1))
    after = register_event(conn, added, TARGET, NOW+timedelta(hours=1))
    assert before.event_id == after.event_id
    assert before.change_id != after.change_id
    assert after.first_public_at == added.published_at
    assert after.facts[0]["metric"] == "gross_margin"
    assert conn.execute("SELECT count(*) FROM business_event_current").fetchone()[0] == 2


def test_equal_scope_conflict_is_retained_without_overwriting_current(conn):
    before = register_event(conn, article(), TARGET, NOW)
    conflict = register_event(conn, article("b", summary="营业收入90亿元。"), TARGET, NOW)
    assert conflict.changes[0]["kind"] == "conflict"
    assert conflict.facts[0]["disputed"] and not conflict.notify_eligible
    current = conn.execute("SELECT fact_id FROM business_event_current").fetchone()[0]
    assert current == before.facts[0]["fact_id"]
    assert conn.execute("SELECT count(*) FROM business_event_facts").fetchone()[0] == 2


def test_explicit_correction_appends_replacement_and_stale_reprint_does_not_revive(conn):
    before = register_event(conn, article(), TARGET, NOW)
    correction = register_event(conn, article("b", summary="公告更正：营业收入由100亿元改为90亿元。", published_at=NOW+timedelta(minutes=20)), TARGET, NOW+timedelta(minutes=20))
    assert correction.changes[0]["kind"] == "correction"
    assert correction.facts[0]["value"] == "9000000000"
    assert correction.facts[0]["supersedes"] == before.facts[0]["fact_id"]
    older = register_event(conn, article("c"), TARGET, NOW+timedelta(minutes=30))
    assert not older.notify_eligible
    assert older.facts[0]["stale"]
    assert older.as_link()["fact_ids"] == []
    assert conn.execute("SELECT count(*) FROM business_event_versions").fetchone()[0] == 2
    # Immutable original evidence is still present.
    assert json.loads(conn.execute("SELECT fact_json FROM business_event_facts WHERE fact_id=?", (before.facts[0]["fact_id"],)).fetchone()[0])["value"] == "10000000000"


def test_currency_basis_and_comparison_relations_never_average(conn):
    cny = register_event(conn, article("a", summary="营业收入100亿元。归母净利润10亿元，同比增长20%，环比下降5%。"), TARGET, NOW)
    usd = register_event(conn, article("b", summary="营业收入100亿美元。扣非净利润8亿元。"), TARGET, NOW)
    facts = [*cny.facts, *usd.facts]
    assert {f["unit"] for f in facts if f["metric"] == "revenue"} == {"CNY", "USD"}
    assert {f["basis"] for f in facts if f["metric"] == "net_profit"} == {"attributable", "nonrecurring_excluded"}
    assert {f["value"] for f in facts if f["relation"] == "qoq"} == {"-5"}
    assert all(not f["disputed"] for f in facts)


def test_combined_nonrecurring_attributable_basis_is_not_total_nonrecurring(conn):
    observed = register_event(conn, article(summary="扣非净利润8亿元。扣非归母净利润7亿元。"), TARGET, NOW)
    profits = [fact for fact in observed.facts if fact['metric'] == 'net_profit']
    assert {fact['basis'] for fact in profits} == {'nonrecurring_excluded', 'nonrecurring_excluded_attributable'}
    assert all(not fact['disputed'] for fact in profits)


def test_opinion_and_rumor_are_not_company_facts_or_notifications(conn):
    opinion = register_event(conn, article("a", summary="分析师认为营业收入有望达到100亿元。"), TARGET, NOW)
    rumor = register_event(conn, article("b", summary="未经证实的传闻：营业收入100亿元。"), TARGET, NOW)
    assert not opinion.notify_eligible and not rumor.notify_eligible
    assert conn.execute("SELECT count(*) FROM business_event_current").fetchone()[0] == 0
    official = register_event(conn, article("c", summary="公司确认营业收入100亿元。"), TARGET, NOW)
    assert official.changes[0]["kind"] == "confirmation" and official.notify_eligible


def test_forecasts_are_distinct_from_realized_financial_results(conn):
    estimate = register_event(conn, article("a", summary="公司预计营业收入100亿元。"), TARGET, NOW)
    actual = register_event(conn, article("b", summary="公司确认营业收入90亿元。"), TARGET, NOW)
    assert estimate.facts[0]["key"] != actual.facts[0]["key"]
    assert actual.changes[0]["kind"] == "new_fact"


def test_untrusted_observation_cannot_lend_or_poison_fact_time(conn):
    untrusted = register_event(conn, article("a", time_status="unknown"), TARGET, NOW)
    assert not untrusted.notify_eligible and not untrusted.facts
    assert conn.execute("SELECT count(*) FROM business_event_facts").fetchone()[0] == 0
    verified = register_event(conn, article("a"), TARGET, NOW)
    assert verified.notify_eligible and verified.first_public_at == NOW


def test_registration_does_not_commit_callers_transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    register_event(conn, article(), TARGET, NOW)
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT count(*) FROM business_events").fetchone()[0] == 0


def test_same_title_new_metrics_and_negative_qoq_revise_without_correction_word(conn):
    original = article(summary="营业收入100亿元。")
    first = QualityStore(conn).observe(original, NOW)
    added = replace(original, summary="营业收入100亿元。净利润10亿元，环比下降20%。")
    newer = QualityStore(conn).observe(added, NOW+timedelta(minutes=1))
    assert revision_key(first) != revision_key(newer)
    facts = extract_facts(newer)
    assert any(f["relation"] == "qoq" and f["value"] == "-20" for f in facts)
    truncated = QualityStore(conn).observe(original, NOW+timedelta(minutes=2))
    assert revision_key(truncated) == revision_key(newer)
    assert len(conn.execute("SELECT * FROM quality_article_observations").fetchall()) == 3


def test_new_member_can_evaluate_globally_duplicate_fact(conn):
    first = register_event(conn, article(), TARGET, NOW)
    second = register_event(conn, article("b"), TARGET, NOW)
    assert second.notify_eligible
    assert first.as_link()["fact_ids"] == second.as_link()["fact_ids"]


def test_conflict_cannot_reserve_an_id_and_block_later_explicit_correction(conn):
    register_event(conn, article(), TARGET, NOW)
    disputed = register_event(conn, article("b", summary="营业收入90亿元。"), TARGET, NOW)
    corrected = register_event(conn, article("c", summary="公告更正营业收入为90亿元。"), TARGET, NOW)
    assert corrected.facts[0]["fact_id"] != disputed.facts[0]["fact_id"]
    assert corrected.notify_eligible and corrected.facts[0]["kind"] == "correction"
    duplicate = register_event(conn, article("c", summary="公告更正营业收入为90亿元。"), TARGET, NOW)
    assert duplicate.change_id == corrected.change_id
    assert duplicate.changes[0]["before"]["value"] == "10000000000"


def test_older_source_discovered_after_reprint_only_tightens_effective_time(conn):
    newer = register_event(conn, article("reprint"), TARGET, NOW)
    original_time = NOW - timedelta(hours=5)
    older = register_event(conn, article("original", published_at=original_time), TARGET, NOW)
    again = register_event(conn, article("third", published_at=NOW+timedelta(hours=1)), TARGET, NOW+timedelta(hours=1))
    assert newer.change_id == older.change_id == again.change_id
    assert older.first_public_at == again.first_public_at == original_time
    assert conn.execute("SELECT count(*) FROM business_event_versions").fetchone()[0] == 1


def test_new_opinion_does_not_lend_its_time_to_old_fact(conn):
    old_time = NOW-timedelta(hours=5)
    register_event(conn, article("a", published_at=old_time), TARGET, NOW)
    added = register_event(conn, article("b", summary="营业收入100亿元。分析师认为毛利率将达30%。"), TARGET, NOW)
    assert added.first_public_at == old_time
    assert len(added.as_link()["fact_ids"]) == 1


def test_same_symbol_display_aliases_do_not_change_strong_business_identity():
    alias = NewsTarget(TARGET.key, "合成企业", "合成企业")
    first = article()
    second = article("b", title="合成企业2026年前三季度业绩公告")
    assert event_identity(first, TARGET) == event_identity(second, alias)
    another_symbol = NewsTarget("654321.SH", TARGET.name, TARGET.query)
    assert event_identity(first, TARGET)[0] != event_identity(first, another_symbol)[0]


def test_same_article_title_revision_retains_weak_event_binding(conn):
    raw = article("same", "合成公司订单公告", "订单金额100万元。")
    before = register_event(conn, raw, TARGET, NOW)
    after = register_event(conn, replace(raw, title="合成公司订单更正公告", summary="公告更正订单金额为90万元。"), TARGET, NOW)
    assert not before.strong_identity and before.event_id == after.event_id
    assert after.facts[0]["supersedes"] == before.facts[0]["fact_id"]


def test_same_title_new_metric_has_new_trusted_time_after_old_window(conn):
    old_time = NOW-timedelta(hours=5)
    old = article(published_at=old_time)
    observed = QualityStore(conn).observe(old, NOW)
    register_event(conn, observed, TARGET, NOW)
    current = QualityStore(conn).observe(replace(old, published_at=NOW, summary="营业收入100亿元。毛利率30%。"), NOW)
    assert current.time_status == "trusted" and current.source_published_at == NOW
    result = register_event(conn, current, TARGET, NOW)
    facts = {fact["metric"]: fact for fact in result.facts}
    assert facts["revenue"]["first_public_at"] == old_time.isoformat()
    assert facts["gross_margin"]["first_public_at"] == NOW.isoformat()


def test_new_metric_does_not_erase_an_accepted_correction_in_same_article(conn):
    initial = article()
    QualityStore(conn).observe(initial, NOW)
    correction = QualityStore(conn).observe(replace(initial, summary="公告更正营业收入为90亿元。"), NOW)
    enriched = QualityStore(conn).observe(replace(initial, summary="毛利率为30%。"), NOW)
    facts = {fact["metric"]: fact for fact in extract_facts(enriched)}
    assert facts["revenue"]["value"] == "9000000000"
    assert facts["gross_margin"]["value"] == "30"
    assert revision_key(enriched) != revision_key(correction)


def test_profit_loss_is_negative_and_is_not_equated_with_positive_profit(conn):
    positive = register_event(conn, article(summary="净利润5亿元。"), TARGET, NOW)
    loss = register_event(conn, article("b", summary="公告更正净利润为亏损5亿元。"), TARGET, NOW)
    assert positive.facts[0]["value"] == "500000000"
    assert loss.facts[0]["value"] == "-500000000"


def test_weak_events_are_scoped_by_stable_target_not_just_same_article(conn):
    implicit = article(title="供应链订单动态", summary="订单金额100万元。")
    a = register_event(conn, implicit, TARGET, NOW)
    other = NewsTarget("654321.SH", "另一公司", "另一公司")
    b = register_event(conn, implicit, other, NOW)
    assert a.event_id != b.event_id


def test_complete_numeric_ranges_are_not_recorded_as_a_point_value():
    low = extract_facts(article(summary="预计营业收入10至12亿元。净利润同比下降10%至20%。"))
    values = {fact["metric"]: fact["value"] for fact in low}
    assert values["revenue"] == "1000000000..1200000000"
    assert values["net_profit"] == "-20..-10"
    mixed = extract_facts(article(summary="营业收入1亿美元至2亿元。"))
    assert mixed == ()


@pytest.mark.parametrize("summary", [
    "传闻回购方案正式实施。", "分析师认为回购计划已完成。", "预计回购方案将正式实施。",
    "公司否认回购已实施。", "回购尚未正式实施。", "回购计划可能已完成。",
    "预测回购方案已完成。", "回购方案有望已完成。", "公司计划正式实施回购。",
    "回购计划将于下月正式实施。",
])
def test_uncertain_or_negated_lifecycle_never_becomes_realized_current(conn, summary):
    value = register_event(conn, article(title="合成公司回购计划编号PLAN-001公告", summary=summary), TARGET, NOW)
    assert not value.notify_eligible
    assert conn.execute("SELECT count(*) FROM business_event_current").fetchone()[0] == 0


@pytest.mark.parametrize("time_status", ["unknown", "conflict"])
def test_untrusted_changed_article_cannot_replace_binding_or_create_strong_relationship(conn, time_status):
    source = article()
    accepted = register_event(conn, source, TARGET, NOW)
    changed = register_event(conn, replace(source, title="合成公司2026年年度业绩公告", time_status=time_status), TARGET, NOW)
    assert not changed.strong_identity and not changed.notify_eligible
    assert conn.execute("SELECT count(*) FROM business_events WHERE strong_identity=1").fetchone()[0] == 1
    assert conn.execute("SELECT event_id FROM business_event_article_bindings").fetchone()[0] == accepted.event_id
    next_excerpt = register_event(conn, replace(source, title="合成公司业绩动态"), TARGET, NOW)
    assert next_excerpt.event_id == accepted.event_id
    assert not next_excerpt.notify_eligible
    assert conn.execute("SELECT count(*) FROM business_event_current").fetchone()[0] == 1
    assert conn.execute("SELECT fact_id FROM business_event_current").fetchone()[0] == accepted.facts[0]["fact_id"]


def test_untrusted_first_observation_has_no_current_binding(conn):
    pending = register_event(conn, article(time_status="unknown"), TARGET, NOW)
    assert not pending.strong_identity
    assert conn.execute("SELECT count(*) FROM business_event_article_bindings").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM business_event_evidence").fetchone()[0] == 1


def test_late_discovery_of_older_correction_does_not_rollback_newer_current(conn):
    latest = article("latest", summary="公告更正营业收入为90亿元。")
    b = register_event(conn, latest, TARGET, NOW)
    earlier = article("earlier", summary="公告更正营业收入为100亿元。", published_at=NOW-timedelta(hours=2))
    a = register_event(conn, earlier, TARGET, NOW+timedelta(minutes=1))
    assert a.event_id == b.event_id
    assert a.facts[0]["stale"] and a.facts[0]["kind"] == "historical"
    assert not a.notify_eligible and a.facts[0]["supersedes"] is None
    assert conn.execute("SELECT fact_id FROM business_event_current").fetchone()[0] == b.facts[0]["fact_id"]
    reprint = register_event(conn, replace(latest, key="reprint", raw_id="reprint", url="https://example.invalid/reprint", published_at=NOW+timedelta(minutes=10)), TARGET, NOW+timedelta(minutes=10))
    assert reprint.change_id == b.change_id
    assert not reprint.facts[0].get("stale") and reprint.notify_eligible
    assert reprint.first_public_at == NOW
    assert conn.execute("SELECT count(*) FROM business_event_facts").fetchone()[0] == 2


def test_temporal_comparison_uses_tightened_fact_time_not_original_snapshot(conn):
    first = register_event(conn, article("reprint", summary="营业收入90亿元。"), TARGET, NOW)
    register_event(conn, article("original", summary="营业收入90亿元。", published_at=NOW-timedelta(hours=2)), TARGET, NOW)
    correction = register_event(conn, article("correction", summary="公告更正营业收入为100亿元。", published_at=NOW-timedelta(hours=1)), TARGET, NOW)
    assert correction.notify_eligible and not correction.facts[0].get("stale")
    assert correction.facts[0]["supersedes"] == first.facts[0]["fact_id"]
    assert correction.changes[0]["before"]["first_public_at"] == (NOW-timedelta(hours=2)).isoformat()


def test_reprint_of_historical_again_correction_cannot_reassert_stale_value(conn):
    latest = register_event(conn, article("latest", summary="公司再次更正营业收入为90亿元。"), TARGET, NOW)
    old = article("old", summary="公司再次更正营业收入为100亿元。", published_at=NOW-timedelta(hours=2))
    historical = register_event(conn, old, TARGET, NOW)
    reprint = register_event(conn, replace(old, key="reprint", raw_id="reprint", url="https://example.invalid/reprint", published_at=NOW+timedelta(hours=1)), TARGET, NOW+timedelta(hours=1))
    assert historical.facts[0]["kind"] == "historical"
    assert reprint.change_id == historical.change_id
    assert not reprint.notify_eligible and reprint.facts[0]["stale"]
    assert conn.execute("SELECT fact_id FROM business_event_current").fetchone()[0] == latest.facts[0]["fact_id"]
