"""Offline WSGI tests for health, push, and encrypted callbacks."""

from __future__ import annotations

import io
import json
import urllib.parse
from pathlib import Path

import pytest

from stock_watch.api import create_app
from stock_watch.settings import Settings
from stock_watch.wecom_crypto import WeComCrypto


TOKEN = "QDG6eK"
AES_KEY = "jWmYm7qr5nMoAUwZRjGtBxmz3KA1tkAj3ykkR6q2B2C"
CORP_ID = "wx5823bf96d3bd56c7"
AGENT_ID = 218


def settings(tmp_path: Path, **changes) -> Settings:
    values = {
        "data_dir": tmp_path,
        "database_path": tmp_path / "stock-watch.db",
        "build_sha": "abc123",
        "push_token": "push-token-" + "x" * 32,
        "wecom_corp_id": CORP_ID,
        "wecom_agent_id": AGENT_ID,
        "wecom_secret": "fake-secret",
        "wecom_to_users": ("broadcast-user",),
        "wecom_callback_token": TOKEN,
        "wecom_encoding_aes_key": AES_KEY,
        "wecom_admin_userids": ("admin-user",),
    }
    values.update(changes)
    return Settings(**values)


def invoke(
    app,
    *,
    method: str = "GET",
    path: str = "/health",
    query: str = "",
    body: bytes = b"",
    content_type: str = "application/json",
    authorization: str = "",
    content_length: str | None = None,
):
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "CONTENT_TYPE": content_type,
        "CONTENT_LENGTH": str(len(body)) if content_length is None else content_length,
        "HTTP_AUTHORIZATION": authorization,
        "wsgi.input": io.BytesIO(body),
    }
    response = {}

    def start_response(status, headers):
        response["status"] = int(status.split()[0])
        response["headers"] = dict(headers)

    response["body"] = b"".join(app(environ, start_response))
    try:
        response["json"] = json.loads(response["body"])
    except ValueError:
        pass
    return response


class FakePushClient:
    def __init__(self) -> None:
        self.messages = []

    def send(self, text):
        self.messages.append(text)
        return {"code": 0, "accepted_chunks": 1, "msgids": ["fake"]}


class RecordingCallbackService:
    def __init__(self) -> None:
        self.messages = []

    def handle(self, message):
        self.messages.append(message)


def encrypted_callback(
    plaintext: str,
    *,
    outer_agent: int = AGENT_ID,
    timestamp: str = "1700000000",
    nonce: str = "123456",
):
    crypto = WeComCrypto(TOKEN, AES_KEY, CORP_ID)
    encrypted = crypto.encrypt(plaintext, random_bytes=b"0123456789abcdef")
    body = (
        f"<xml><ToUserName><![CDATA[{CORP_ID}]]></ToUserName>"
        f"<Encrypt><![CDATA[{encrypted}]]></Encrypt>"
        f"<AgentID>{outer_agent}</AgentID></xml>"
    ).encode()
    query = urllib.parse.urlencode(
        {
            "msg_signature": crypto.signature(timestamp, nonce, encrypted),
            "timestamp": timestamp,
            "nonce": nonce,
        }
    )
    return body, query


def text_xml(
    *,
    sender: str = "admin-user",
    agent_id: int = AGENT_ID,
    msg_id: str | None = "4561255354251345929",
    content: str = "添加方向 半导体设备",
) -> str:
    id_element = "" if msg_id is None else f"<MsgId>{msg_id}</MsgId>"
    return (
        f"<xml><ToUserName><![CDATA[{CORP_ID}]]></ToUserName>"
        f"<FromUserName><![CDATA[{sender}]]></FromUserName>"
        "<CreateTime>1700000000</CreateTime><MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>{id_element}"
        f"<AgentID>{agent_id}</AgentID></xml>"
    )


def test_health_and_ready_include_build_version(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), readiness=lambda: (True, "database_ok"))
    health = invoke(app)
    assert health["status"] == 200
    assert health["json"]["status"] == "ok"
    assert health["json"]["build_sha"] == "abc123"
    assert health["json"]["version"]

    ready = invoke(app, path="/ready")
    assert ready["status"] == 200
    assert ready["json"]["status"] == "ready"
    assert ready["json"]["reason"] == "database_ok"


def test_not_ready_is_503_and_never_calls_external_service(tmp_path: Path) -> None:
    push = FakePushClient()
    app = create_app(
        settings(tmp_path), push_client=push, readiness=lambda: (False, "schema_mismatch")
    )
    result = invoke(app, path="/ready")
    assert result["status"] == 503
    assert result["json"]["reason"] == "schema_mismatch"
    assert push.messages == []


def test_push_preserves_bearer_contract_and_unicode(tmp_path: Path) -> None:
    config = settings(tmp_path)
    push = FakePushClient()
    app = create_app(config, push_client=push)
    payload = json.dumps(
        {"title": "预警", "content": "兆易创新📈"}, ensure_ascii=False
    ).encode()
    denied = invoke(app, method="POST", path="/push", body=payload)
    assert denied["status"] == 401
    assert push.messages == []

    accepted = invoke(
        app,
        method="POST",
        path="/push",
        body=payload,
        authorization="Bearer " + config.push_token,
    )
    assert accepted["status"] == 200
    assert push.messages == ["预警\n\n兆易创新📈"]


def test_push_rejects_missing_length_and_placeholder_token(tmp_path: Path) -> None:
    payload = b'{"content":"test"}'
    config = settings(tmp_path)
    app = create_app(config, push_client=FakePushClient())
    missing = invoke(
        app,
        method="POST",
        path="/push",
        body=payload,
        authorization="Bearer " + config.push_token,
        content_length="",
    )
    assert missing["status"] == 411
    placeholder = settings(tmp_path, push_token="CHANGE_ME_" + "x" * 40)
    rejected = invoke(
        create_app(placeholder, push_client=FakePushClient()),
        method="POST",
        path="/push",
        body=payload,
        authorization="Bearer " + placeholder.push_token,
    )
    assert rejected["status"] == 503


def test_get_callback_verifies_signature_decrypts_and_returns_plain_echo(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    crypto = WeComCrypto(TOKEN, AES_KEY, CORP_ID)
    encrypted = crypto.encrypt("callback-ok", random_bytes=b"0123456789abcdef")
    timestamp, nonce = "1700000000", "42"
    query = urllib.parse.urlencode(
        {
            "msg_signature": crypto.signature(timestamp, nonce, encrypted),
            "timestamp": timestamp,
            "nonce": nonce,
            "echostr": encrypted,
        }
    )
    result = invoke(
        create_app(config, callback_service=RecordingCallbackService()),
        path="/wecom/callback",
        query=query,
    )
    assert result["status"] == 200
    assert result["body"] == b"callback-ok"
    assert not result["body"].endswith(b"\n")


def test_post_callback_validates_and_dispatches_normalized_message(
    tmp_path: Path,
) -> None:
    callback = RecordingCallbackService()
    body, query = encrypted_callback(text_xml())
    result = invoke(
        create_app(settings(tmp_path), callback_service=callback),
        method="POST",
        path="/wecom/callback",
        query=query,
        body=body,
        content_type="text/xml",
    )
    assert result["status"] == 200
    assert result["body"] == b""
    assert len(callback.messages) == 1
    message = callback.messages[0]
    assert message["message_id"] == "4561255354251345929"
    assert message["from_user"] == "admin-user"
    assert message["content"] == "添加方向 半导体设备"
    assert message["agent_id"] == AGENT_ID


def test_invalid_signature_identity_and_admin_never_dispatch(
    tmp_path: Path,
) -> None:
    callback = RecordingCallbackService()
    app = create_app(settings(tmp_path), callback_service=callback)

    body, query = encrypted_callback(text_xml())
    bad_query = urllib.parse.parse_qs(query)
    bad_query["msg_signature"] = ["0" * 40]
    result = invoke(
        app,
        method="POST",
        path="/wecom/callback",
        query=urllib.parse.urlencode(bad_query, doseq=True),
        body=body,
        content_type="text/xml",
    )
    assert result["status"] == 403

    body, query = encrypted_callback(text_xml(agent_id=999))
    result = invoke(
        app,
        method="POST",
        path="/wecom/callback",
        query=query,
        body=body,
        content_type="text/xml",
    )
    assert result["status"] == 403

    body, query = encrypted_callback(text_xml(sender="not-an-admin"))
    result = invoke(
        app,
        method="POST",
        path="/wecom/callback",
        query=query,
        body=body,
        content_type="text/xml",
    )
    assert result["status"] == 403
    assert callback.messages == []


def test_event_without_msgid_gets_stable_content_based_identity(tmp_path: Path) -> None:
    callback = RecordingCallbackService()
    app = create_app(settings(tmp_path), callback_service=callback)
    event = (
        f"<xml><ToUserName>{CORP_ID}</ToUserName><FromUserName>admin-user</FromUserName>"
        "<CreateTime>1700000001</CreateTime><MsgType>event</MsgType>"
        "<Event>enter_agent</Event>"
        f"<AgentID>{AGENT_ID}</AgentID></xml>"
    )
    for _ in range(2):
        body, query = encrypted_callback(event)
        result = invoke(
            app,
            method="POST",
            path="/wecom/callback",
            query=query,
            body=body,
            content_type="application/xml",
        )
        assert result["status"] == 200
    assert callback.messages[0]["message_id"].startswith("event:")
    assert callback.messages[0]["message_id"] == callback.messages[1]["message_id"]


def test_duplicate_query_parameter_and_malformed_xml_are_rejected(
    tmp_path: Path,
) -> None:
    callback = RecordingCallbackService()
    app = create_app(settings(tmp_path), callback_service=callback)
    result = invoke(
        app,
        path="/wecom/callback",
        query="timestamp=1&timestamp=2&nonce=3&msg_signature=x&echostr=y",
    )
    assert result["status"] == 400

    result = invoke(
        app,
        method="POST",
        path="/wecom/callback",
        query="timestamp=1&nonce=2&msg_signature=x",
        body=b"<!DOCTYPE xml><xml/>",
        content_type="text/xml",
    )
    assert result["status"] in {400, 403}
    assert callback.messages == []


def test_legacy_domain_verification_file_route_is_narrow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "WW_verify_example.txt").write_text("verification-only")
    monkeypatch.setenv("VERIFICATION_DIR", str(tmp_path))
    app = create_app(settings(tmp_path))
    result = invoke(app, path="/WW_verify_example.txt")
    assert result["status"] == 200
    assert result["body"] == b"verification-only"
    assert invoke(app, path="/.env")["status"] == 404
