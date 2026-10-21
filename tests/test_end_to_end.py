"""Encrypted management message -> durable config -> next real pipeline cycle."""
import base64
import io
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from stock_watch.analyzer import FakeAnalyzer, build_analysis_prompt
from stock_watch.api import create_app
from stock_watch.callback_service import CallbackService
from stock_watch.config import ConfigStore
from stock_watch.db import connect, init_db
from stock_watch.delivery import DeliveryDispatcher
from stock_watch.monitor import MonitorService
from stock_watch.news import NewsArticle
from stock_watch.settings import Settings
from stock_watch.wecom_crypto import WeComCrypto


def test_encrypted_add_target_is_used_by_next_monitor_cycle(tmp_path):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "integration.db",
        wecom_corp_id="ww-local", wecom_agent_id=1000002, wecom_admin_userids=("admin",),
        wecom_to_users=("alerts",), wecom_callback_token="local-callback-token",
        wecom_encoding_aes_key=base64.b64encode(b"A" * 32).decode().rstrip("="))
    conn = connect(settings.database_path)
    init_db(conn)
    conn.close()
    class Fetcher:
        active = False
        queries = []
        def fetch_target(self, target, *, now=None):
            self.queries.append(target.query)
            if self.active and "工业机器人" in target.query:
                return [NewsArticle(key="new-robotics", title="工业机器人订单增长", summary="模拟新闻",
                    url="https://example.invalid/robotics", published_at=now - timedelta(seconds=1),
                    target_keys=(target.key,))]
            return []
    fetcher, analyzer = Fetcher(), FakeAnalyzer()
    monitor = MonitorService(settings.database_path, fetcher, analyzer, environ={"MONITOR_ENABLED": "false"})
    monitor.run_once(force=True)
    crypto = WeComCrypto(settings.wecom_callback_token, settings.wecom_encoding_aes_key, settings.wecom_corp_id)
    plaintext = ("<xml><ToUserName>ww-local</ToUserName><FromUserName>admin</FromUserName>"
        "<CreateTime>1792560000</CreateTime><MsgType>text</MsgType><Content>添加方向 工业机器人</Content>"
        "<MsgId>integration-1</MsgId><AgentID>1000002</AgentID></xml>")
    encrypted = crypto.encrypt(plaintext)
    stamp, nonce = "1792560000", "integration-nonce"
    raw = f"<xml><Encrypt>{encrypted}</Encrypt></xml>".encode()
    environ = {"REQUEST_METHOD": "POST", "PATH_INFO": "/wecom/callback",
        "QUERY_STRING": urlencode({"msg_signature": crypto.signature(stamp, nonce, encrypted),
                                    "timestamp": stamp, "nonce": nonce}),
        "CONTENT_TYPE": "text/xml", "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw)}
    response = {}
    def start(status, headers):
        response["status"] = int(status.split()[0])
    b"".join(create_app(settings, callback_service=CallbackService(settings))(environ, start))
    assert response["status"] == 200
    conn = connect(settings.database_path)
    snap = ConfigStore(conn).snapshot().to_dict()
    assert any(row["name"] == "工业机器人" for row in snap["themes"])
    conn.close()
    fetcher.active = True
    scan_time = datetime.now(timezone.utc) + timedelta(seconds=5)
    result = monitor.run_once(now=scan_time, force=True)
    assert any("工业机器人" in query for query in fetcher.queries)
    assert result.analyzed == 1
    assert "工业机器人" in build_analysis_prompt(*analyzer.calls[-1])
    class Sender:
        sent = []
        def send_chunk(self, text, to_user=None):
            self.sent.append((text, to_user))
    sender = Sender()
    dispatcher = DeliveryDispatcher(settings, sender)
    while dispatcher.deliver_one(now=scan_time):
        pass
    assert any(user == "admin" for _, user in sender.sent)
    assert any(user == "alerts" and "工业机器人" in text for text, user in sender.sent)
    conn = connect(settings.database_path)
    assert conn.execute("SELECT count(*) FROM outbox WHERE status='sent'").fetchone()[0] == 2
    conn.close()
