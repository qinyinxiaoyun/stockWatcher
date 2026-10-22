"""Offline acceptance tests for shared work and independently recoverable delivery."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from stock_watch.analyzer import DeepSeekAnalyzer, FakeAnalyzer
from stock_watch.db import connect, init_db
from stock_watch.delivery import DeliveryDispatcher
from stock_watch.news import NewsArticle
from stock_watch.settings import Settings
from stock_watch.subscriptions import MemberConfigStore, MemberRegistry
from stock_watch.subscription_monitor import SubscriptionMonitorService, article_version


NOW = datetime(2026, 10, 22, 2, tzinfo=timezone.utc)


@pytest.fixture
def state(tmp_path):
    path = tmp_path / "subscriptions.db"
    conn = connect(path)
    init_db(conn)
    registry = MemberRegistry(conn, "test-corp")
    members = [registry.add("alice"), registry.add("bob")]
    for member in members:
        MemberConfigStore(conn, member.id, clock=lambda: NOW-timedelta(hours=1)).apply_operation(
            {"op": "add_stock", "symbol": "123456.SH", "name": "示例公司", "focus": "订单"}, actor=member.user_id)
    yield path, conn, registry, members
    conn.close()


class Fetcher:
    def __init__(self, *, keys=("news-1",), sources=("东方财富",), published=NOW):
        self.calls = []
        self.keys, self.sources, self.published = keys, sources, published

    def fetch_target(self, target, **kwargs):
        self.calls.append(target)
        return [NewsArticle(key=key, raw_id=key, title=f"新闻 {key}", summary="订单发生变化",
                            url=f"https://example.invalid/{key}", source=source,
                            published_at=self.published, target_keys=(target.key,))
                for source in self.sources for key in self.keys]


def run_service(path, fetcher, analyzer=None, **kwargs):
    return SubscriptionMonitorService(path, fetcher, analyzer or FakeAnalyzer(), corp_id="test-corp",
                                      environ={"MONITOR_ENABLED": "false"}, **kwargs)


def pending(conn):
    return [json.loads(row[0]) for row in conn.execute("SELECT payload_json FROM outbox ORDER BY id")]


def test_shared_query_and_analysis_fan_out_to_both_members(state):
    path, conn, _, members = state
    fetcher, analyzer = Fetcher(), FakeAnalyzer()
    result = run_service(path, fetcher, analyzer).run_once(now=NOW, force=True)
    assert result.queued == 2 and result.analyzed == 1
    assert len(fetcher.calls) == 1 and len(analyzer.calls) == 1
    assert {row["member_id"] for row in pending(conn)} == {m.id for m in members}
    assert {row["touser"] for row in pending(conn)} == {"alice", "bob"}
    assert all(len(targets) == 1 for _, targets in analyzer.calls)


def test_different_focus_and_private_scope_never_share_analysis(state):
    path, conn, registry, members = state
    MemberConfigStore(conn, members[1].id).apply_operation(
        {"op": "set_focus", "target": "123456.SH", "focus": "风险"}, actor="bob")
    analyzer = FakeAnalyzer()
    assert run_service(path, Fetcher(), analyzer).run_once(now=NOW, force=True).analyzed == 2
    registry.set_analysis_private("bob", True)
    analyzer = FakeAnalyzer()
    # A new article has identical focus text but Bob explicitly requests isolation.
    MemberConfigStore(conn, members[1].id).apply_operation(
        {"op": "set_focus", "target": "123456.SH", "focus": "订单"}, actor="bob")
    result = run_service(path, Fetcher(keys=("news-2",)), analyzer).run_once(now=NOW, force=True)
    assert result.analyzed == 2 and result.queued == 2


def test_member_failure_does_not_swallow_others_and_restart_uses_pool(state):
    path, conn, _, members = state
    MemberConfigStore(conn, members[0].id).apply_operation(
        {"op": "set_focus", "target": "123456.SH", "focus": "失败组"}, actor="alice")
    class Analyzer(FakeAnalyzer):
        def analyze(self, article, targets):
            targets = tuple(targets)
            if targets[0].focus == ("失败组",):
                raise TimeoutError("private upstream content")
            return super().analyze(article, targets)
    first = run_service(path, Fetcher(), Analyzer()).run_once(now=NOW, force=True)
    assert first.status == "partial" and first.queued == 1
    assert pending(conn)[0]["touser"] == "bob"
    # Upstream no longer returns the item; the durable pool and Bob's completed
    # evaluation survive a process restart without replaying Bob's notification.
    second = run_service(path, Fetcher(keys=()), FakeAnalyzer()).run_once(now=NOW, force=True)
    assert second.queued == 1
    assert len(pending(conn)) == 2
    assert {p["touser"] for p in pending(conn)} == {"alice", "bob"}


def test_new_subscription_does_not_replay_pool_history(state):
    path, conn, registry, _ = state
    run_service(path, Fetcher()).run_once(now=NOW, force=True)
    charlie = registry.add("charlie")
    MemberConfigStore(conn, charlie.id, clock=lambda: NOW+timedelta(minutes=1)).apply_operation(
        {"op": "add_stock", "symbol": "123456.SH", "name": "示例公司", "focus": "订单"}, actor="charlie")
    result = run_service(path, Fetcher(keys=())).run_once(now=NOW+timedelta(minutes=2), force=True)
    assert result.queued == 0
    assert not any(p["touser"] == "charlie" for p in pending(conn))


def test_threshold_change_does_not_reanalyze_or_replay_history(state):
    path, conn, _, members = state
    analyzer = FakeAnalyzer()
    run_service(path, Fetcher(), analyzer).run_once(now=NOW, force=True)
    MemberConfigStore(conn, members[0].id).apply_operation(
        {"op": "set_push_levels", "levels": ["重大"]}, actor="alice")
    result = run_service(path, Fetcher(), analyzer).run_once(now=NOW, force=True)
    assert result.analyzed == 0 and result.queued == 0
    assert len(analyzer.calls) == 1


def test_same_member_multiple_targets_merge_one_notification(state):
    path, conn, _, members = state
    for member in members:
        MemberConfigStore(conn, member.id, clock=lambda: NOW-timedelta(hours=1)).apply_operation(
            {"op": "add_stock", "symbol": "654321.SH", "name": "另一公司", "focus": "订单"}, actor=member.user_id)
    analyzer = FakeAnalyzer()
    result = run_service(path, Fetcher(), analyzer).run_once(now=NOW, force=True)
    assert result.analyzed == 2 and result.queued == 2
    assert all(len(p["matched_target_ids"]) == 2 for p in pending(conn))
    assert all(len(targets) == 1 for _, targets in analyzer.calls)


def test_source_namespaces_and_content_revisions_are_independent(state):
    path, conn, _, _ = state
    first = run_service(path, Fetcher(sources=("source-a", "source-b"))).run_once(now=NOW, force=True)
    assert first.queued == 4
    assert conn.execute("SELECT count(*) FROM subscription_articles").fetchone()[0] == 2
    one = NewsArticle("id", "标题", "https://example.invalid", NOW, source="one")
    assert article_version(one)[0] != article_version(replace(one, source="two"))[0]
    assert article_version(one)[0] != article_version(replace(one, summary="新增材料"))[0]


def test_revocation_before_delivery_cancels_without_broadcast(state, tmp_path):
    path, conn, registry, _ = state
    run_service(path, Fetcher()).run_once(now=NOW, force=True)
    registry.revoke("alice")
    calls = []
    class Sender:
        def send_chunk(self, text, to_user=None):
            calls.append(to_user)
    settings = Settings(data_dir=tmp_path, database_path=path, wecom_corp_id="test-corp", wecom_to_users=("broadcast",))
    dispatcher = DeliveryDispatcher(settings, Sender())
    assert dispatcher.deliver_one(now=NOW+timedelta(minutes=1))
    assert dispatcher.deliver_one(now=NOW+timedelta(minutes=1))
    assert calls == ["bob"]
    assert [r[0] for r in conn.execute("SELECT status FROM outbox ORDER BY id")] == ["cancelled", "sent"]


def test_cancellation_between_segments_stops_remaining_chunks(state, tmp_path):
    path, conn, registry, members = state
    stamp = NOW.isoformat(timespec="seconds")
    payload = {"text": "长文本"*2000, "member_id": members[0].id, "member_generation": members[0].generation,
               "corp_id": "test-corp", "touser": "alice"}
    conn.execute("INSERT INTO outbox(event_key,payload_json,available_at,created_at) VALUES ('receipt',?,?,?)",
                 (json.dumps(payload), stamp, stamp))
    calls = []
    class Sender:
        def send_chunk(self, text, to_user=None):
            calls.append(to_user)
            registry.revoke("alice")
    settings = Settings(data_dir=tmp_path, database_path=path, wecom_corp_id="test-corp", wecom_to_users=("broadcast",))
    assert DeliveryDispatcher(settings, Sender()).deliver_one(now=NOW+timedelta(minutes=1))
    row = conn.execute("SELECT status,payload_json FROM outbox").fetchone()
    assert row[0] == "cancelled" and json.loads(row[1])["accepted_chunks"] == 1
    assert calls == ["alice"]


@pytest.mark.parametrize("runtime_corp", ["", "another-corp"])
@pytest.mark.parametrize("member_mode", [False, True])
def test_scoped_delivery_rejects_runtime_organization_mismatch(state, tmp_path, runtime_corp, member_mode):
    path, conn, _, members = state
    if member_mode:
        conn.execute("INSERT INTO app_meta(key,value) VALUES('independent_subscriptions','test-corp')")
    member = members[0]
    stamp = NOW.isoformat(timespec="seconds")
    payload = {"kind": "receipt", "text": "private offline receipt", "corp_id": "test-corp",
               "member_id": member.id, "member_generation": member.generation, "touser": member.user_id}
    conn.execute("INSERT INTO outbox(event_key,payload_json,available_at,created_at) VALUES ('wrong-corp',?,?,?)",
                 (json.dumps(payload), stamp, stamp))
    class Sender:
        def send_chunk(self, *args, **kwargs):
            pytest.fail("a private message crossed runtime organization boundaries")
    settings = Settings(data_dir=tmp_path, database_path=path, wecom_corp_id=runtime_corp,
                        wecom_to_users=("must-never-broadcast",))
    assert DeliveryDispatcher(settings, Sender()).deliver_one(now=NOW+timedelta(minutes=1))
    assert conn.execute("SELECT status FROM outbox").fetchone()[0] == "cancelled"


def test_member_mode_runtime_mismatch_cannot_fall_back_to_push_delivery(state, tmp_path):
    path, conn, _, _ = state
    conn.execute("INSERT INTO app_meta(key,value) VALUES('independent_subscriptions','test-corp')")
    stamp = NOW.isoformat(timespec="seconds")
    payload = {"event": "push", "text": "offline operator push", "touser": "alice"}
    conn.execute("INSERT INTO outbox(event_key,payload_json,available_at,created_at) VALUES ('wrong-mode-corp',?,?,?)",
                 (json.dumps(payload), stamp, stamp))
    class Sender:
        def send_chunk(self, *args, **kwargs):
            pytest.fail("organization mismatch regained unscoped delivery privileges")
    settings = Settings(data_dir=tmp_path, database_path=path, wecom_corp_id="another-corp",
                        wecom_to_users=("must-never-broadcast",))
    assert DeliveryDispatcher(settings, Sender()).deliver_one(now=NOW+timedelta(minutes=1))
    assert conn.execute("SELECT status FROM outbox").fetchone()[0] == "cancelled"


def test_paused_subscription_cancels_prepared_notification(state, tmp_path):
    path, conn, _, members = state
    run_service(path, Fetcher()).run_once(now=NOW, force=True)
    MemberConfigStore(conn, members[0].id).apply_operation({"op": "pause", "target": "123456.SH"}, actor="alice")
    class Sender:
        def send_chunk(self, *args, **kwargs):
            pytest.fail("cancelled subscription was delivered")
    settings = Settings(data_dir=tmp_path, database_path=path, wecom_corp_id="test-corp", wecom_to_users=("broadcast",))
    assert DeliveryDispatcher(settings, Sender()).deliver_one(now=NOW+timedelta(minutes=1))
    assert conn.execute("SELECT status FROM outbox ORDER BY id LIMIT 1").fetchone()[0] == "cancelled"


def test_total_analysis_budget_and_member_fairness(state):
    path, conn, _, _ = state
    # Distinct focus groups ensure the second member needs its own allowance.
    MemberConfigStore(conn, 2).apply_operation({"op": "set_focus", "target": "123456.SH", "focus": "风险"}, actor="bob")
    result = run_service(path, Fetcher(keys=tuple(f"news-{i}" for i in range(12)))).run_once(now=NOW, force=True)
    assert result.analyzed == 10
    assert {p["touser"] for p in pending(conn)} == {"alice", "bob"}
    assert result.status == "partial"


def test_dry_run_writes_no_pool_cache_or_notifications_and_disabled_by_default(state):
    path, conn, _, _ = state
    service = run_service(path, Fetcher())
    assert service.run_once(now=NOW).reason == "monitor_disabled"
    assert service.run_once(now=NOW, force=True, dry_run=True).queued == 2
    for table in ("subscription_articles", "subscription_analysis", "member_evaluations", "outbox", "monitor_runs"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_backfill_requires_explicit_member_and_affects_only_that_member(state):
    path, conn, _, members = state
    service = run_service(path, Fetcher(published=NOW-timedelta(hours=2)))
    with pytest.raises(ValueError, match="member"):
        service.run_once(now=NOW, force=True, backfill_hours=4, backfill_target="123456.SH")
    result = service.run_once(now=NOW, force=True, backfill_hours=4,
                              backfill_target="123456.SH", member_id=members[0].id)
    assert result.queued == 1 and pending(conn)[0]["touser"] == "alice"


def test_lost_lease_cannot_commit_analysis_or_notification(state):
    path, conn, _, _ = state
    class Stealer(FakeAnalyzer):
        def analyze(self, article, targets):
            conn.execute("UPDATE monitor_leases SET lease_owner='replacement'")
            return super().analyze(article, targets)
    result = run_service(path, Fetcher(), Stealer()).run_once(now=NOW, force=True)
    assert result.reason == "lease_lost"
    assert conn.execute("SELECT count(*) FROM subscription_analysis").fetchone()[0] == 0
    assert pending(conn) == []


def test_intersecting_member_queries_are_unioned_into_one_fetch(state):
    path, conn, _, members = state
    MemberConfigStore(conn, members[0].id).apply_operation(
        {"op": "set_aliases", "target": "123456.SH", "aliases": ["共同别名", "甲别名"]}, actor="alice")
    MemberConfigStore(conn, members[1].id).apply_operation(
        {"op": "set_aliases", "target": "123456.SH", "aliases": ["共同别名", "乙别名"]}, actor="bob")
    fetcher = Fetcher()
    result = run_service(path, fetcher).run_once(now=NOW, force=True)
    assert result.queued == 2 and len(fetcher.calls) == 1
    assert {"共同别名", "甲别名", "乙别名"}.issubset(fetcher.calls[0].queries)
    assert fetcher.calls[0].queries.count("共同别名") == 1


def test_member_ordinary_threshold_is_local_and_does_not_split_analysis(state):
    path, conn, _, members = state
    for member, level in zip(members, ("重大", "普通")):
        MemberConfigStore(conn, member.id).apply_operation(
            {"op": "set_push_levels", "levels": [level]}, actor=member.user_id)
    class Ordinary(FakeAnalyzer):
        def analyze(self, article, targets):
            return replace(super().analyze(article, targets), level="普通")
    result = run_service(path, Fetcher(), Ordinary()).run_once(now=NOW, force=True)
    assert result.analyzed == 1 and result.queued == 1
    assert pending(conn)[0]["touser"] == "bob"


def test_more_than_ten_members_make_progress_across_persistent_rounds(state):
    path, conn, registry, members = state
    members = list(members)
    for number in range(2, 13):
        member = registry.add(f"subscriber-{number}")
        MemberConfigStore(conn, member.id, clock=lambda: NOW-timedelta(hours=1)).apply_operation(
            {"op": "add_stock", "symbol": "123456.SH", "name": "示例公司", "focus": "订单"}, actor=member.user_id)
        members.append(member)
    for member in members:
        registry.set_analysis_private(member.user_id, True)
    first = run_service(path, Fetcher(keys=("first",))).run_once(now=NOW, force=True)
    assert first.analyzed == 10 and first.queued == 10
    # New work keeps arriving; a fresh process must resume at the saved member.
    second = run_service(path, Fetcher(keys=("second",))).run_once(now=NOW+timedelta(minutes=1), force=True)
    assert second.analyzed == 10
    assert {p["member_id"] for p in pending(conn)} == {m.id for m in members}


@pytest.mark.parametrize("invalid", ["not json", '{"level":"forged"}'])
def test_corrupt_cached_analysis_is_repaired_without_losing_fanout(state, invalid):
    path, conn, _, _ = state
    run_service(path, Fetcher()).run_once(now=NOW, force=True)
    # Emulate an interrupted fan-out after the analysis commit.
    conn.execute("DELETE FROM member_evaluations")
    conn.execute("DELETE FROM outbox")
    conn.execute("UPDATE subscription_analysis SET result_json=?", (invalid,))
    result = run_service(path, Fetcher(keys=())).run_once(now=NOW, force=True)
    assert result.status == "succeeded" and result.analyzed == 1 and result.queued == 2
    stored = json.loads(conn.execute("SELECT result_json FROM subscription_analysis").fetchone()[0])
    assert stored["level"] == "重要"


def test_generic_failure_before_analysis_finishes_run_and_releases_lease(state):
    path, conn, _, _ = state
    # Failing pool persistence occurs before the analysis loop starts.
    conn.execute("CREATE TRIGGER fail_pool BEFORE INSERT ON subscription_articles BEGIN SELECT RAISE(ABORT, 'injected'); END")
    result = run_service(path, Fetcher()).run_once(now=NOW, force=True)
    assert result.status == "failed" and result.analyzed == result.queued == 0
    assert conn.execute("SELECT status FROM monitor_runs").fetchone()[0] == "failed"
    assert conn.execute("SELECT count(*) FROM monitor_leases").fetchone()[0] == 0


def test_new_provider_baselines_history_but_media_names_do_not_rebaseline(state):
    path, conn, _, _ = state
    run_service(path, Fetcher(sources=("媒体甲",))).run_once(now=NOW, force=True)
    changed_media = run_service(path, Fetcher(keys=("new-media",), sources=("媒体乙",))).run_once(now=NOW, force=True)
    assert changed_media.queued == 2
    fetcher = Fetcher(keys=("new-provider",), sources=("媒体丙",))
    fetcher.source_namespace = "announcements"
    result = run_service(path, fetcher).run_once(now=NOW, force=True)
    assert result.queued == 0 and result.baseline_seen == 2


def test_revoke_while_fetching_prevents_stale_private_model_call(state):
    path, conn, registry, _ = state
    registry.set_analysis_private("alice", True)
    class RevokingFetcher(Fetcher):
        def fetch_target(self, target, **kwargs):
            registry.revoke("alice")
            return super().fetch_target(target, **kwargs)
    analyzer = FakeAnalyzer()
    result = run_service(path, RevokingFetcher(), analyzer).run_once(now=NOW, force=True)
    assert result.analyzed == 1 and result.queued == 1
    assert pending(conn)[0]["touser"] == "bob"


def test_privacy_changed_during_model_call_cannot_store_old_public_result(state):
    path, conn, registry, _ = state
    class PrivacyChanger(FakeAnalyzer):
        changed = False
        def analyze(self, article, targets):
            if not self.changed:
                registry.set_analysis_private("alice", True)
                self.changed = True
            return super().analyze(article, targets)
    result = run_service(path, Fetcher(), PrivacyChanger()).run_once(now=NOW, force=True)
    # Bob is still entitled to his own public analysis, so Alice's stale result
    # cannot be the one committed to the shared cache.
    assert result.analyzed == 2 and result.queued == 1
    assert pending(conn)[0]["touser"] == "bob"


def test_threshold_change_after_queue_cancels_old_analysis_before_sending(state, tmp_path):
    path, conn, _, members = state
    run_service(path, Fetcher()).run_once(now=NOW, force=True)
    MemberConfigStore(conn, members[0].id).apply_operation(
        {"op": "set_push_levels", "levels": ["重大"]}, actor="alice")
    class Sender:
        def send_chunk(self, *args, **kwargs):
            pytest.fail("current member threshold should cancel the old alert")
    settings = Settings(data_dir=tmp_path, database_path=path, wecom_corp_id="test-corp", wecom_to_users=("broadcast",))
    assert DeliveryDispatcher(settings, Sender()).deliver_one(now=NOW+timedelta(minutes=1))
    assert conn.execute("SELECT status FROM outbox ORDER BY id LIMIT 1").fetchone()[0] == "cancelled"


def test_member_mode_cannot_retry_legacy_broadcast(state, tmp_path):
    path, conn, _, _ = state
    conn.execute("INSERT INTO app_meta(key,value) VALUES('independent_subscriptions','test-corp')")
    stamp = NOW.isoformat(timespec="seconds")
    conn.execute("INSERT INTO outbox(event_key,payload_json,available_at,created_at) VALUES ('old-alert',?,?,?)",
                 (json.dumps({"event": "news.analysis", "text": "旧新闻"}), stamp, stamp))
    class Sender:
        def send_chunk(self, *args, **kwargs):
            pytest.fail("legacy queue gained broadcast permission")
    settings = Settings(data_dir=tmp_path, database_path=path, wecom_corp_id="test-corp", wecom_to_users=("broadcast",))
    assert DeliveryDispatcher(settings, Sender()).deliver_one(now=NOW+timedelta(minutes=1))
    assert conn.execute("SELECT status FROM outbox").fetchone()[0] == "cancelled"


def test_retention_bounds_pool_cache_evaluations_and_legacy_seen(state):
    path, conn, _, members = state
    run_service(path, Fetcher()).run_once(now=NOW, force=True)
    cutoff = (NOW-timedelta(days=16)).isoformat(timespec="seconds")
    conn.execute("UPDATE subscription_articles SET published_at=?", (cutoff,))
    conn.execute("UPDATE subscription_analysis SET created_at=?", (cutoff,))
    conn.execute("UPDATE member_evaluations SET evaluated_at=?", (cutoff,))
    conn.execute("INSERT INTO member_legacy_seen VALUES (?,?,?)", (members[0].id, "old", cutoff))
    run_service(path, Fetcher(keys=())).run_once(now=NOW, force=True)
    for table in ("subscription_articles", "subscription_analysis", "member_evaluations", "member_legacy_seen"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert len(pending(conn)) == 2  # Outbox idempotency and accepted history persist.


def test_real_analyzer_retry_copy_enforces_ten_external_request_budget(state):
    path, conn, _, members = state
    MemberConfigStore(conn, members[1].id).apply_operation(
        {"op": "set_focus", "target": "123456.SH", "focus": "风险"}, actor="bob")
    requests = []
    def failing_transport(**kwargs):
        requests.append(kwargs)
        raise TimeoutError()
    analyzer = DeepSeekAnalyzer(failing_transport, max_attempts=2, sleeper=lambda delay: None)
    service = run_service(path, Fetcher(keys=tuple(f"news-{i}" for i in range(20))), analyzer)
    assert service.analyzer is not analyzer
    assert service.analyzer.max_attempts == 1 and analyzer.max_attempts == 2
    assert service.analyzer.model == analyzer.model
    assert service.analyzer.transport is analyzer.transport
    result = service.run_once(now=NOW, force=True)
    assert result.analyzed == len(requests) == 10
    assert result.queued == 0
    assert conn.execute("SELECT count(*) FROM member_evaluations").fetchone()[0] == 0


def test_inherited_seen_binds_first_content_version_and_allows_later_revision(state):
    path, conn, _, members = state
    for member in members:
        MemberConfigStore(conn, member.id, clock=lambda: NOW-timedelta(hours=1)).apply_operation(
            {"op": "add_stock", "symbol": "654321.SH", "name": "另一公司", "focus": "订单"}, actor=member.user_id)
        conn.execute("INSERT INTO member_legacy_seen VALUES (?,?,?)", (member.id, "news-1", NOW.isoformat()))
    first = run_service(path, Fetcher()).run_once(now=NOW, force=True)
    assert first.queued == 0 and first.analyzed == 0
    assert conn.execute("SELECT count(*) FROM member_evaluations").fetchone()[0] == 4
    again = run_service(path, Fetcher()).run_once(now=NOW, force=True)
    assert again.queued == again.analyzed == 0
    class CorrectedFetcher(Fetcher):
        def fetch_target(self, target, **kwargs):
            return [replace(article, summary="更正：原订单已经撤销，存在新增经营风险")
                    for article in super().fetch_target(target, **kwargs)]
    changed = run_service(path, CorrectedFetcher()).run_once(now=NOW, force=True)
    assert changed.queued == 2 and changed.analyzed == 2
    assert all(len(payload["matched_target_ids"]) == 2 for payload in pending(conn))


def test_explicit_backfill_delivers_with_real_subscription_activation_time(state, tmp_path):
    path, conn, _, members = state
    snapshot = MemberConfigStore(conn, members[0].id).snapshot()
    result = run_service(path, Fetcher(published=NOW-timedelta(hours=2))).run_once(
        now=NOW, force=True, backfill_hours=3, backfill_target="123456.SH", member_id=members[0].id)
    assert result.queued == result.analyzed == 1
    payload = pending(conn)[0]
    assert payload["subscription_effective_at"] == {
        target["key"]: target["effective_at"] for target in snapshot.targets
    }
    delivered = []
    class Sender:
        def send_chunk(self, text, to_user=None):
            delivered.append(to_user)
    settings = Settings(data_dir=tmp_path, database_path=path, wecom_corp_id="test-corp", wecom_to_users=("never-broadcast",))
    assert DeliveryDispatcher(settings, Sender()).deliver_one(now=NOW+timedelta(minutes=1))
    assert delivered == ["alice"]
    assert conn.execute("SELECT status FROM outbox").fetchone()[0] == "sent"
