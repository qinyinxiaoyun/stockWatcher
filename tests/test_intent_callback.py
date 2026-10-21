import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from stock_watch.callback_service import CallbackService
from stock_watch.config import ConfigStore
from stock_watch.db import connect, init_db
from stock_watch.intent import DeepSeekIntentClient, IntentError, IntentPlan, validate_intent
from stock_watch.settings import Settings


@pytest.fixture
def configured(tmp_path):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "test.db",
                        wecom_admin_userids=("admin",), wecom_to_users=("broadcast-a", "broadcast-b"))
    conn = connect(settings.database_path)
    init_db(conn)
    conn.close()
    return settings


def message(key, text, user="admin"):
    return {"message_id": key, "from_user": user, "to_user": "corp", "agent_id": "1",
            "msg_type": "text", "content": text, "create_time": "1"}


def snapshot(settings):
    conn = connect(settings.database_path)
    try:
        return ConfigStore(conn).snapshot()
    finally:
        conn.close()


def rows(settings, sql):
    conn = connect(settings.database_path)
    try:
        return [dict(row) for row in conn.execute(sql)]
    finally:
        conn.close()


def test_duplicate_callback_changes_once_and_receipt_only_to_sender(configured):
    service = CallbackService(configured)
    before = snapshot(configured).config_version
    service.handle(message("msg1", "添加方向 半导体设备"))
    # Simulate restarting the HTTP app and receiving the same callback again.
    CallbackService(configured).handle(message("msg1", "添加方向 半导体设备"))
    assert snapshot(configured).config_version == before + 1
    receipts = rows(configured, "SELECT * FROM outbox")
    assert len(receipts) == 1
    assert json.loads(receipts[0]["payload_json"])["touser"] == "admin"
    assert rows(configured, "SELECT * FROM inbox")[0]["status"] == "done"


def test_transaction_rolls_back_config_if_receipt_cannot_be_saved(configured):
    conn = connect(configured.database_path)
    conn.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'test'); END")
    conn.close()
    before = snapshot(configured).config_version
    with pytest.raises(Exception):
        CallbackService(configured).handle(message("msg2", "暂停监控 603986.SH"))
    assert snapshot(configured).config_version == before
    assert rows(configured, "SELECT * FROM inbox") == []


def test_natural_language_is_async_and_conflict_does_not_overwrite(configured):
    class Model:
        def parse(self, text, snap, context):
            return IntentPlan(({"op": "pause", "target": "603986.SH"},), snap.to_dict()["config_version"])
    service = CallbackService(configured, intent_client=Model())
    before = snapshot(configured).config_version
    service.handle(message("nl1", "以后先别看兆易创新了"))
    assert snapshot(configured).config_version == before
    service.handle(message("explicit1", "添加方向 先进封装"))
    assert service.run_one()
    assert snapshot(configured).config_version == before + 1
    assert not next(stock for stock in snapshot(configured).stocks if stock.symbol == "603986.SH").paused
    receipt = next(row for row in rows(configured, "SELECT * FROM outbox") if row["event_key"] == "reply:nl1")
    assert "本次未修改" in json.loads(receipt["payload_json"])["text"]


def test_model_failure_and_atomic_multi_operation(configured):
    class Model:
        def parse(self, *_args):
            raise IntentError("模型返回错误JSON，配置未修改")
    service = CallbackService(configured, intent_client=Model())
    before = snapshot(configured).config_version
    service.handle(message("nl2", "把它暂停了"))
    assert service.run_one()
    assert snapshot(configured).config_version == before
    assert not service.run_one()


def test_invalid_second_operation_rolls_back_first(configured):
    class Model:
        def parse(self, text, snap, context):
            return IntentPlan(({"op": "pause", "target": "603986.SH"},
                               {"op": "remove_stock", "target": "000001.SZ"}), snap.to_dict()["config_version"])
    service = CallbackService(configured, intent_client=Model())
    before = snapshot(configured).config_version
    service.handle(message("atomic1", "暂停兆易创新再删另一个"))
    assert service.run_one()
    assert snapshot(configured).config_version == before
    assert not next(stock for stock in snapshot(configured).stocks if stock.symbol == "603986.SH").paused


def test_batch_delete_requests_confirmation_bound_to_originator(configured):
    class Model:
        def parse(self, text, snap, context):
            return IntentPlan(({"op": "remove_stock", "target": "603986.SH"},
                               {"op": "remove_stock", "target": "688981.SH"}), snap.to_dict()["config_version"])
    service = CallbackService(configured, intent_client=Model())
    before = snapshot(configured).config_version
    service.handle(message("batch1", "把这两家公司删掉"))
    assert service.run_one()
    assert snapshot(configured).config_version == before
    confirmations = rows(configured, "SELECT * FROM confirmations")
    assert len(confirmations) == 1
    assert confirmations[0]["user_id"] == "admin"
    token = confirmations[0]["token"]
    service.handle(message("batch-confirm", "确认 " + token))
    assert snapshot(configured).config_version == before + 1
    assert len(snapshot(configured).stocks) == 1


def test_expired_async_lease_recovers_and_does_not_apply_twice(configured):
    class Model:
        calls = 0
        def parse(self, text, snap, context):
            self.calls += 1
            return IntentPlan(({"op": "pause", "target": "603986.SH"},
                               {"op": "add_theme", "name": "半导体设备"}), snap.to_dict()["config_version"])
    model = Model()
    service = CallbackService(configured, intent_client=model)
    before = snapshot(configured).config_version
    service.handle(message("nl3", "暂停兆易创新，同时加半导体设备"))
    conn = connect(configured.database_path)
    conn.execute("UPDATE async_tasks SET status='running',locked_until='2000-01-01T00:00:00+00:00'")
    conn.close()
    assert service.run_one()
    assert snapshot(configured).config_version == before + 1
    assert not CallbackService(configured, intent_client=model).run_one()
    assert model.calls == 1


def test_whitelist_revoked_before_worker_prevents_mutation(configured):
    service = CallbackService(configured)
    service.handle(message("nl4", "以后重点看业绩预告"))
    before = snapshot(configured).config_version
    CallbackService(replace(configured, wecom_admin_userids=())).run_one()
    assert snapshot(configured).config_version == before


@pytest.mark.parametrize("raw", [
    "bad json", {"operations": []}, {"operations": [{"op": "sql", "target": "DROP TABLE"}]},
    {"operations": [{"op": "pause", "target": "603986.SH", "code": "exec"}]},
    {"operations": [{"op": "set_push_levels", "levels": ["所有"]}]},
])
def test_invalid_structures_are_rejected(configured, raw):
    with pytest.raises(IntentError):
        validate_intent(raw, snapshot(configured))


def test_pronouns_require_unique_recent_context(configured):
    snap = snapshot(configured)
    with pytest.raises(IntentError, match="无法唯一"):
        validate_intent({"operations": [{"op": "pause", "target": "它"}]}, snap)
    plan = validate_intent({"operations": [{"op": "pause", "target": "它"}]}, snap,
                           {"targets": ["603986.SH"]})
    assert plan.operations[0]["target"] == "603986.SH"


def test_explicit_name_command_stores_stable_context_for_pronoun(configured):
    service = CallbackService(configured)
    service.handle(message("context-name", "设置关注 兆易创新 订单"))
    saved = json.loads(rows(configured, "SELECT * FROM context")[0]["context_json"])
    assert saved["targets"][0].startswith("stk_")
    plan = validate_intent({"operations": [{"op": "pause", "target": "它"}]}, snapshot(configured), saved)
    assert plan.operations[0]["target"] == "603986.SH"
    service.handle(message("context-add", "添加股票 600000.SH 浦发银行"))
    saved = json.loads(rows(configured, "SELECT * FROM context")[0]["context_json"])
    plan = validate_intent({"operations": [{"op": "pause", "target": "它"}]}, snapshot(configured), saved)
    assert plan.operations[0]["target"] == "600000.SH"


def test_model_timeout_and_bad_json_do_not_mutate(configured):
    def timeout(_payload):
        raise TimeoutError()
    client = DeepSeekIntentClient(configured, timeout)
    with pytest.raises(IntentError, match="配置未修改"):
        client.parse("暂停兆易创新", snapshot(configured))
    client = DeepSeekIntentClient(configured, lambda _: {"choices": [{"message": {"content": "not json"}}]})
    with pytest.raises(IntentError):
        client.parse("暂停兆易创新", snapshot(configured))


def test_model_may_not_invent_stock_code(configured):
    response = {"choices": [{"message": {"content": json.dumps({"operations": [
        {"op": "add_stock", "symbol": "600000.SH", "name": "模型猜的"}
    ]})}}]}
    client = DeepSeekIntentClient(configured, lambda _: response)
    with pytest.raises(IntentError, match="明确提供"):
        client.parse("添加一个银行股", snapshot(configured))
