"""Offline tests for application-message delivery."""

from __future__ import annotations

import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from stock_watch.push import (
    MAX_TEXT_BYTES,
    WeComClient,
    WeComFailure,
    http_json,
    split_text,
)


def settings(**changes: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "push_token": "test-auth-token-" + "a" * 40,
        "wecom_corp_id": "ww-test",
        "wecom_agent_id": 1000002,
        "wecom_secret": "test-secret-only",
        "wecom_to_users": ("monitor-one", "monitor-two"),
    }
    values.update(changes)
    return SimpleNamespace(**values)


class FakeTransport:
    def __init__(self, sends=None, token_response=None) -> None:
        self.calls: list[tuple] = []
        self.tokens = 0
        self.sends = list(sends or [])
        self.token_response = token_response

    def __call__(self, method, url, params, payload, timeout):
        self.calls.append((method, url, params, payload, timeout))
        if method == "GET":
            self.tokens += 1
            return self.token_response or {
                "errcode": 0,
                "access_token": f"fake-token-{self.tokens}",
                "expires_in": 7200,
            }
        if self.sends:
            return self.sends.pop(0)
        return {"errcode": 0, "errmsg": "ok", "msgid": "fake-msg"}


def test_unicode_and_emoji_split_on_utf8_boundaries() -> None:
    text = "股票📈汉字🙂e\u0301" * 1500
    chunks = split_text(text)
    assert "".join(chunks) == text
    assert all(len(chunk.encode("utf-8")) <= 1900 for chunk in chunks)
    assert len(chunks) > 1


def test_token_is_cached_and_default_recipients_are_joined() -> None:
    transport = FakeTransport()
    client = WeComClient(settings(), transport)
    client.send("第一条")
    client.send("第二条")
    assert transport.tokens == 1
    sends = [call for call in transport.calls if call[0] == "POST"]
    assert sends[0][3]["touser"] == "monitor-one|monitor-two"
    assert sends[0][3]["text"]["content"] == "第一条"


def test_management_recipient_override_is_not_broadcast() -> None:
    transport = FakeTransport()
    result = WeComClient(settings(), transport).send("已保存", to_user="admin-user")
    sent = [call for call in transport.calls if call[0] == "POST"][-1][3]
    assert result["accepted_chunks"] == 1
    assert sent["touser"] == "admin-user"


def test_expired_token_is_invalidated_and_refreshed_once() -> None:
    transport = FakeTransport([{"errcode": 42001}, {"errcode": 0}])
    result = WeComClient(settings(), transport).send("消息")
    assert result["accepted_chunks"] == 1
    assert transport.tokens == 2


def test_repeated_token_rejection_is_an_error() -> None:
    transport = FakeTransport([{"errcode": 40014}, {"errcode": 40014}])
    with pytest.raises(WeComFailure) as caught:
        WeComClient(settings(), transport).send("消息")
    assert caught.value.reason == "message_rejected"
    assert transport.tokens == 2


def test_partial_delivery_reports_only_accepted_chunks() -> None:
    transport = FakeTransport(
        [{"errcode": 0}, {"errcode": 40058, "errmsg": "bad message"}]
    )
    with pytest.raises(WeComFailure) as caught:
        WeComClient(settings(), transport).send("中文" * 1000)
    assert caught.value.accepted_chunks == 1


@pytest.mark.parametrize(
    "response",
    [
        {"errcode": 0, "invaliduser": "sensitive-member"},
        {"errcode": 0, "unlicenseduser": ["sensitive-member"]},
    ],
)
def test_partial_recipient_rejection_is_not_success(response: dict) -> None:
    with pytest.raises(WeComFailure) as caught:
        WeComClient(settings(), FakeTransport([response])).send("消息")
    assert caught.value.reason == "recipient_rejected"
    assert "sensitive-member" not in str(caught.value)


def test_wire_chunks_include_safe_sequence_marker() -> None:
    transport = FakeTransport()
    WeComClient(settings(), transport).send("汉🙂" * 2000)
    contents = [
        call[3]["text"]["content"] for call in transport.calls if call[0] == "POST"
    ]
    assert len(contents) > 1
    assert contents[0].startswith(f"[1/{len(contents)}]\n")
    assert all(len(content.encode("utf-8")) <= MAX_TEXT_BYTES for content in contents)


def test_send_chunk_supports_persistent_delivery_worker() -> None:
    transport = FakeTransport()
    result = WeComClient(settings(), transport).send_chunk(
        "[1/2]\n第一段", to_user="requesting-admin"
    )
    assert result == {"code": 0, "accepted_chunks": 1, "msgids": ["fake-msg"]}
    assert transport.calls[-1][3]["touser"] == "requesting-admin"
    with pytest.raises(ValueError, match="byte limit"):
        WeComClient(settings(), transport).send_chunk("汉" * 1000)


def test_upstream_errors_are_redacted() -> None:
    config = settings()
    response = {
        "errcode": 40013,
        "errmsg": "corpsecret=test-secret-only authorization="
        + str(config.push_token),
    }
    with pytest.raises(WeComFailure) as caught:
        WeComClient(config, FakeTransport(token_response=response)).send("消息")
    rendered = caught.value.detail
    assert config.wecom_secret not in rendered
    assert config.push_token not in rendered


def test_network_exception_does_not_reflect_secret_url() -> None:
    with patch(
        "urllib.request.urlopen", side_effect=urllib.error.URLError("secret-in-url")
    ):
        with pytest.raises(WeComFailure) as caught:
            http_json(
                "GET",
                "https://example.invalid",
                {"corpsecret": "secret-in-url"},
                None,
                1,
            )
    assert "secret-in-url" not in str(caught.value)


def test_missing_configuration_and_invalid_input_fail_closed() -> None:
    with pytest.raises(WeComFailure, match="push_not_configured"):
        WeComClient(settings(wecom_secret=""), FakeTransport()).send("消息")
    with pytest.raises(WeComFailure, match="recipient_not_configured"):
        WeComClient(settings(wecom_to_users=()), FakeTransport()).send("消息")
    with pytest.raises(ValueError, match="required"):
        WeComClient(settings(), FakeTransport()).send("")
