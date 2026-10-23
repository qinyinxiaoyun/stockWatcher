"""Offline membership, configuration isolation, and explicit migration checks."""

from datetime import datetime, timedelta, timezone
import json

import pytest

from stock_watch.config import ConfigError, ConfigStore, ConfigVersionConflict, ConfirmationError
from stock_watch.db import SCHEMA_VERSION, connect, init_db
from stock_watch.monitor import _baseline_key
from stock_watch.subscriptions import MemberConfigStore, MemberRegistry, MembershipError


NOW = datetime(2026, 10, 22, 1, tzinfo=timezone.utc)
ADD = {"op": "add_stock", "symbol": "000001.SZ", "name": "平安银行",
       "aliases": ["平安"], "topics": ["资本充足率"]}


@pytest.fixture
def members(tmp_path):
    conn = connect(tmp_path / "members.db")
    init_db(conn)
    registry = MemberRegistry(conn, "test-corp")
    a, b = registry.add("alice"), registry.add("bob")
    yield conn, registry, a, b
    conn.close()


def test_new_members_are_empty_without_enabling_or_mutating_legacy(members):
    conn, registry, a, b = members
    original = ConfigStore(conn).snapshot()
    assert SCHEMA_VERSION == 4
    assert not registry.is_enabled()
    assert a.role == "subscriber" and a.active and a.generation == 1
    assert not a.analysis_private
    for member in (a, b):
        snapshot = MemberConfigStore(conn, member.id).snapshot()
        assert snapshot.config_version == 1
        assert snapshot.stocks == snapshot.themes == snapshot.targets == ()
    assert ConfigStore(conn).snapshot() == original
    init_db(conn)
    assert registry.get("alice") == a
    assert conn.in_transaction is False


@pytest.mark.parametrize("user", ["@all", "@ALL", "a|b", "a,b", "a;b", "a b", "a\nb",
                                  "", " a", "a ", "a，b", "外部联系人", "a" * 65])
def test_registry_rejects_broadcast_or_multiple_recipients(members, user):
    _, registry, _, _ = members
    with pytest.raises(MembershipError):
        registry.add(user)


def test_members_cannot_edit_other_members_or_use_foreign_targets(members):
    conn, _, a, b = members
    alice = MemberConfigStore(conn, a.id, clock=lambda: NOW)
    bob = MemberConfigStore(conn, b.id, clock=lambda: NOW)
    alice.apply_operation(ADD, actor="alice")
    for actor in ("bob", "local-operator", " alice "):
        with pytest.raises(MembershipError):
            alice.apply_operation({"op": "clear"}, actor=actor)
    with pytest.raises(ConfigError):
        bob.apply_operation({"op": "remove", "target": "000001.SZ"}, actor="bob")
    assert len(alice.snapshot().stocks) == 1
    assert not bob.snapshot().stocks
    assert len(alice.audit()) == 1 and bob.audit() == []
    assert ConfigStore(conn).audit() == []


def test_member_config_uses_one_version_per_atomic_batch_and_preserves_rows(members):
    conn, _, a, _ = members
    store = MemberConfigStore(conn, a.id, clock=lambda: NOW)
    updated = store.apply_operations([
        ADD, {"op": "set_focus", "target": "平安", "value": "盈利、风险"},
        {"op": "pause", "target": "000001.SZ"},
        {"op": "add_theme", "name": "半导体", "keywords": ["设备", "材料"]},
        {"op": "pause_theme", "target": "半导体"},
        {"op": "set_push_levels", "levels": ["重大"]},
    ], actor="alice", expected_version=1, source="test", source_message_id="test-msg")
    assert updated.config_version == 2
    assert updated.stocks[0].aliases == ("平安",)
    assert updated.stocks[0].focus == "盈利、风险"
    assert not updated.stocks[0].enabled and not updated.themes[0].enabled
    assert updated.stocks[0].effective_at == "2026-10-22T01:00:00Z"
    assert updated.push_levels == ("重大",)
    document = json.loads(conn.execute("SELECT snapshot_json FROM member_configs WHERE member_id=?",
                                       (a.id,)).fetchone()[0])
    assert document["stock_topics"][0]["topic"] == "资本充足率"
    assert len(store.audit()) == 6
    assert {row["from_version"] for row in store.audit()} == {1}
    assert {row["to_version"] for row in store.audit()} == {2}
    with pytest.raises(ConfigVersionConflict):
        store.apply_operation({"op": "clear"}, actor="alice", expected_version=1)
    with pytest.raises(ConfigError):
        store.apply_operations([{"op": "clear"}, {"op": "bogus"}], actor="alice")
    assert store.snapshot() == updated


def test_confirmations_bind_member_user_version_expiry_and_consumption(members):
    conn, _, a, b = members
    now = [NOW]
    alice = MemberConfigStore(conn, a.id, clock=lambda: now[0])
    bob = MemberConfigStore(conn, b.id, clock=lambda: now[0])
    alice.apply_operation(ADD, actor="alice")
    token = alice.request_confirmation(user_id="alice")
    with pytest.raises(MembershipError):
        alice.request_confirmation(user_id="bob")
    with pytest.raises(ConfirmationError):
        bob.apply_confirmation(token.token, user_id="bob")
    with pytest.raises(MembershipError):
        alice.apply_confirmation(token.token, user_id="alice", actor="bob")
    bob.apply_operation(ADD, actor="bob")
    assert alice.apply_confirmation(token.token, user_id="alice").stocks == ()
    assert len(bob.snapshot().stocks) == 1
    with pytest.raises(ConfirmationError):
        alice.apply_confirmation(token.token, user_id="alice")
    expired = alice.request_confirmation(user_id="alice", ttl_seconds=1)
    now[0] += timedelta(seconds=1)
    with pytest.raises(ConfirmationError):
        alice.apply_confirmation(expired.token, user_id="alice")
    stale = alice.request_confirmation(user_id="alice")
    alice.apply_operation(ADD, actor="alice")
    with pytest.raises(ConfigVersionConflict):
        alice.apply_confirmation(stale.token, user_id="alice")


def test_member_batch_confirmation_is_atomic(members):
    conn, _, a, _ = members
    store = MemberConfigStore(conn, a.id)
    confirmation = store.request_confirmation(user_id="alice", action="batch",
                                             payload={"operations": [ADD, {"op": "add_theme", "name": "AI"}]})
    result = store.apply_confirmation(confirmation.token, user_id="alice")
    assert result.config_version == 2 and len(result.stocks) == len(result.themes) == 1
    assert len(store.audit()) == 2


def test_revoke_rejoin_and_privacy_changes_fence_old_confirmation(members):
    conn, registry, a, _ = members
    store = MemberConfigStore(conn, a.id)
    store.apply_operation(ADD, actor="alice")
    token = store.request_confirmation(user_id="alice")
    revoked = registry.revoke("alice")
    assert not revoked.active and revoked.generation == 2
    assert registry.all()[0] == revoked
    assert len(registry.active()) == 1
    for action in (store.snapshot, lambda: store.apply_operation({"op": "clear"}, actor="alice")):
        with pytest.raises(MembershipError):
            action()
    rejoined = registry.add("alice")
    assert rejoined.id == a.id and rejoined.generation == 3
    assert len(store.snapshot().stocks) == 1
    with pytest.raises(ConfirmationError):
        store.apply_confirmation(token.token, user_id="alice")
    second = store.request_confirmation(user_id="alice")
    private = registry.set_analysis_private("alice", True)
    assert private.analysis_private and private.generation == 4
    assert registry.set_analysis_private("alice", True).generation == 4
    with pytest.raises(ConfirmationError):
        store.apply_confirmation(second.token, user_id="alice")
    assert registry.set_analysis_private("alice", False).generation == 5


def test_member_write_rechecks_generation_after_local_validation(members):
    conn, registry, a, _ = members
    revoked = [False]
    def clock():
        if not revoked[0]:
            revoked[0] = True
            registry.revoke("alice")
        return NOW
    store = MemberConfigStore(conn, a.id, clock=clock)
    with pytest.raises(MembershipError):
        store.apply_operation(ADD, actor="alice")
    registry.add("alice")
    assert store.snapshot().stocks == ()
    assert [row["action"] for row in store.audit()] == ["reactivate_subscription"]


def test_member_write_compare_and_swap_does_not_lose_concurrent_update(members):
    conn, _, a, _ = members
    other = MemberConfigStore(conn, a.id, clock=lambda: NOW)
    raced = [False]
    def clock():
        if not raced[0]:
            raced[0] = True
            other.apply_operation({"op": "add_theme", "name": "AI"}, actor="alice")
        return NOW
    store = MemberConfigStore(conn, a.id, clock=clock)
    with pytest.raises(ConfigVersionConflict):
        store.apply_operation(ADD, actor="alice")
    snapshot = store.snapshot()
    assert snapshot.config_version == 2 and not snapshot.stocks
    assert snapshot.themes[0].name == "AI"
    assert len(store.audit()) == 1


def test_assign_legacy_owner_preserves_complete_configuration_and_history(members):
    conn, registry, a, b = members
    legacy = ConfigStore(conn, clock=lambda: NOW)
    legacy.apply_operations([
        {"op": "add_stock", "symbol": "000001.SZ", "name": "平安", "topics": ["利率"], "aliases": ["PA"]},
        {"op": "pause", "target": "603986.SH"},
        {"op": "add_theme", "name": "AI", "keywords": ["人工智能"]},
    ], actor="original-owner")
    conn.execute("UPDATE stocks SET direction='negative',push_level='all' WHERE symbol='000001.SZ'")
    before = legacy.snapshot()
    conn.execute("INSERT INTO seen_articles VALUES('legacy-article','2026-10-22T00:00:00Z')")
    conn.execute("INSERT INTO monitor_state VALUES(?,'1','2026-10-22T00:00:00Z')",
                 (_baseline_key("中芯国际"),))
    registry.assign_legacy_owner("alice")
    assert registry.is_enabled()
    assert MemberConfigStore(conn, a.id).snapshot() == before == legacy.snapshot()
    assert MemberConfigStore(conn, b.id).snapshot().stocks == ()
    assert len(MemberConfigStore(conn, a.id).audit()) == len(legacy.audit())
    document = json.loads(conn.execute("SELECT snapshot_json FROM member_configs WHERE member_id=?",
                                       (a.id,)).fetchone()[0])
    assert document["stock_topics"][0]["topic"] == "利率"
    assert conn.execute("SELECT article_key FROM member_legacy_seen WHERE member_id=?", (a.id,)).fetchone()[0] == "legacy-article"
    assert conn.execute("SELECT target_key FROM member_baselines WHERE member_id=?", (a.id,)).fetchone()[0] == "stk_263899307d615441"
    with pytest.raises(MembershipError):
        registry.assign_legacy_owner("bob")
    with pytest.raises(MembershipError):
        MemberRegistry(conn, "another-corp").add("mallory")


@pytest.mark.parametrize("queued", ["outbox", "intent", "monitor_run", "monitor_lease"])
def test_activation_requires_drained_legacy_jobs_and_no_active_monitor(members, queued):
    conn, registry, _, _ = members
    stamp = "2999-01-01T00:00:00Z"
    if queued == "outbox":
        conn.execute("INSERT INTO outbox(event_key,payload_json,available_at,created_at) VALUES('event','{}',?,?)", (stamp, stamp))
    elif queued == "intent":
        conn.execute("INSERT INTO async_tasks(task_key,task_type,payload_json,available_at,created_at,updated_at) VALUES('intent:old','intent','{}',?,?,?)", (stamp, stamp, stamp))
    elif queued == "monitor_run":
        conn.execute("INSERT INTO monitor_runs(slot,status,lease_until) VALUES('old','running',?)", (stamp,))
    else:
        conn.execute("INSERT INTO monitor_leases VALUES('monitor','old-owner',?,'old',?)", (stamp, stamp))
    with pytest.raises(MembershipError):
        registry.assign_legacy_owner("alice")
    assert not registry.is_enabled()
    assert MemberConfigStore(conn, registry.get("alice").id).snapshot().stocks == ()


def test_assign_requires_explicit_empty_active_owner_and_rolls_back_atomically(members, monkeypatch):
    conn, registry, a, _ = members
    with pytest.raises(MembershipError):
        registry.assign_legacy_owner("unknown")
    assert registry.get("unknown") is None
    store = MemberConfigStore(conn, a.id)
    store.apply_operation(ADD, actor="alice")
    with pytest.raises(MembershipError):
        registry.assign_legacy_owner("alice")
    import stock_watch.subscription_monitor as monitor
    def fail(*_):
        raise RuntimeError("simulated migration interruption")
    monkeypatch.setattr(monitor, "copy_legacy_state", fail)
    with pytest.raises(RuntimeError):
        registry.assign_legacy_owner("bob")
    assert not registry.is_enabled()
    assert MemberConfigStore(conn, registry.get("bob").id).snapshot().stocks == ()
    assert conn.in_transaction is False


def test_outer_transaction_controls_member_writes(members):
    conn, _, a, _ = members
    conn.execute("BEGIN IMMEDIATE")
    store = MemberConfigStore(conn, a.id)
    store.apply_operation(ADD, actor="alice")
    assert conn.in_transaction
    conn.rollback()
    assert store.snapshot().stocks == ()
    assert store.audit() == []


def test_reactivation_refreshes_only_enabled_targets_and_versions_once(members, monkeypatch):
    conn, registry, a, _ = members
    store = MemberConfigStore(conn, a.id, clock=lambda: NOW)
    before = store.apply_operations([
        ADD,
        {"op": "add_stock", "symbol": "000002.SZ", "name": "万科"},
        {"op": "pause", "target": "000002.SZ"},
        {"op": "add_theme", "name": "AI"},
        {"op": "add_theme", "name": "房地产"},
        {"op": "pause_theme", "target": "房地产"},
    ], actor="alice")
    registry.revoke("alice")
    import stock_watch.subscriptions as subscriptions
    resumed_at = "2026-10-23T02:00:00Z"
    monkeypatch.setattr(subscriptions, "_now", lambda: resumed_at)
    resumed = registry.add("alice")
    after = store.snapshot()
    assert resumed.generation == 3
    assert after.config_version == before.config_version + 1
    assert after.updated_at == resumed_at
    assert len(after.targets) == 2
    assert {target["effective_at"] for target in after.targets} == {resumed_at}
    paused = [stock for stock in after.stocks if not stock.enabled]
    paused.extend(theme for theme in after.themes if not theme.enabled)
    assert len(paused) == 2
    assert {item.effective_at for item in paused} == {"2026-10-22T01:00:00Z"}
    assert store.audit()[0]["action"] == "reactivate_subscription"
    assert store.audit()[0]["actor"] == "local-operator"
    # Repeated admission and changing the role must not start new windows.
    registry.add("alice")
    registry.add("alice", role="admin")
    assert store.snapshot() == after


def test_rejoin_during_member_validation_fences_old_generation_write(members):
    conn, registry, a, _ = members
    raced = [False]
    def clock():
        if not raced[0]:
            raced[0] = True
            registry.revoke("alice")
            registry.add("alice")
        return NOW
    store = MemberConfigStore(conn, a.id, clock=clock)
    with pytest.raises(MembershipError):
        store.apply_operation(ADD, actor="alice")
    snapshot = store.snapshot()
    assert snapshot.config_version == 2 and not snapshot.stocks
    assert [row["action"] for row in store.audit()] == ["reactivate_subscription"]


def test_expired_monitor_leases_allow_atomic_activation(members):
    conn, registry, a, _ = members
    expired = "1970-01-01T00:00:00Z"
    conn.execute("INSERT INTO monitor_runs(slot,status,lease_until) VALUES('old','running',?)", (expired,))
    conn.execute("INSERT INTO monitor_leases VALUES('monitor','old-owner',?,'old',?)", (expired, expired))
    registry.assign_legacy_owner("alice")
    assert registry.is_enabled()
    assert MemberConfigStore(conn, a.id).snapshot().stocks == ConfigStore(conn).snapshot().stocks
    assert not conn.in_transaction
