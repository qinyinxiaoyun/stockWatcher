"""Member commands -> shared analysis -> current rules -> isolated delivery retry."""
from datetime import datetime, timedelta, timezone
import json

from stock_watch.analyzer import FakeAnalyzer
from stock_watch.callback_service import CallbackService
from stock_watch.cli import FakeFetcher
from stock_watch.config import ConfigStore
from stock_watch.db import connect, init_db
from stock_watch.delivery import DeliveryDispatcher
from stock_watch.monitor_router import MonitorRouter
from stock_watch.settings import Settings
from stock_watch.subscriptions import MemberRegistry


def test_member_pipeline_reuses_analysis_and_retries_only_failed_recipient(tmp_path):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "members.db",
                        wecom_corp_id="ww-offline", wecom_to_users=("must-never-broadcast",))
    conn = connect(settings.database_path)
    init_db(conn)
    ConfigStore(conn).apply_operation({"op": "clear"}, actor="offline-operator")
    registry = MemberRegistry(conn, settings.wecom_corp_id)
    for user in ("alice", "bob", "carol"):
        registry.add(user)
    registry.assign_legacy_owner("alice")
    callback = CallbackService(settings)
    for user in ("alice", "bob", "carol"):
        callback.handle({"message_id": "same-upstream-id", "from_user": user,
                         "msg_type": "text", "content": "添加方向 工业机器人"})
    scan_time = datetime.now(timezone.utc) + timedelta(seconds=5)
    fetcher, analyzer = FakeFetcher(), FakeAnalyzer()
    monitor = MonitorRouter(settings.database_path, fetcher, analyzer,
                            corp_id=settings.wecom_corp_id, environ={"MONITOR_ENABLED": "false"})
    result = monitor.run_once(force=True, now=scan_time)
    assert result.analyzed == 1 and result.queued == 3
    assert len(fetcher.queries) == 1 and len(analyzer.calls) == 1
    # A rule tightened after analysis must be checked again at actual delivery.
    callback.handle({"message_id": "tighten-bob", "from_user": "bob",
                     "msg_type": "text", "content": "设置推送等级 重大"})

    class Sender:
        def __init__(self):
            self.failed_carol = False
            self.news_recipients = []

        def send_chunk(self, text, to_user=None):
            assert to_user in {"alice", "bob", "carol"}
            if "AI 自动新闻预警" in text:
                if to_user == "carol" and not self.failed_carol:
                    self.failed_carol = True
                    raise OSError("offline simulated transient failure")
                self.news_recipients.append(to_user)

    sender = Sender()
    dispatcher = DeliveryDispatcher(settings, sender)
    delivery_time = scan_time + timedelta(minutes=1)
    for _ in range(20):
        if not dispatcher.deliver_one(now=delivery_time):
            break
    assert sender.news_recipients == ["alice"]
    for _ in range(20):
        if not dispatcher.deliver_one(now=delivery_time + timedelta(minutes=1)):
            break
    assert sender.news_recipients == ["alice", "carol"]
    news = {json.loads(row["payload_json"])["touser"]: row["status"]
            for row in conn.execute("SELECT * FROM outbox WHERE event_key LIKE 'member:%:article:%'")}
    assert news == {"alice": "sent", "bob": "cancelled", "carol": "sent"}
    conn.close()
