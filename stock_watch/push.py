"""Reliable, dependency-light client for WeCom application text messages."""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from typing import Any


TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
SEND_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/send"
MAX_RESPONSE_BYTES = 128 * 1024
MAX_TEXT_BYTES = 2048
# Reserve space for the ``[n/total]\n`` marker added to split messages.
CHUNK_BYTES = 1900
TOKEN_ERROR_CODES = frozenset({40014, 42001, 42007, 42009})


class WeComFailure(RuntimeError):
    """A sanitized WeCom failure safe for API responses and logs."""

    def __init__(
        self,
        reason: str,
        wecom_code: int | None = None,
        detail: str = "",
        *,
        accepted_chunks: int = 0,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.wecom_code = wecom_code
        self.detail = detail
        self.accepted_chunks = accepted_chunks


def _configured(value: object) -> bool:
    text = str(value).strip()
    return bool(text) and not text.upper().startswith("CHANGE_ME")


def http_json(
    method: str,
    url: str,
    params: dict[str, Any],
    payload: dict[str, Any] | None,
    timeout: float,
) -> dict[str, Any]:
    """Perform one JSON request without leaking credential-bearing URLs."""

    address = url + "?" + urllib.parse.urlencode(params)
    data = (
        None
        if payload is None
        else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )
    request = urllib.request.Request(
        address,
        data=data,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "stock-watch/0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise WeComFailure("upstream_http_error", detail=f"HTTP {status}") from None
    except (urllib.error.URLError, OSError, TimeoutError):
        raise WeComFailure("upstream_unreachable") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise WeComFailure("upstream_response_too_large")
    try:
        result = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        raise WeComFailure("upstream_invalid_json") from None
    if not isinstance(result, dict):
        raise WeComFailure("upstream_invalid_json")
    return result


def split_text(text: str, byte_limit: int = CHUNK_BYTES) -> list[str]:
    """Split without breaking a Unicode code point, measured as UTF-8 bytes."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if byte_limit < 4:
        raise ValueError("byte_limit must be at least four bytes")
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for character in text:
        try:
            width = len(character.encode("utf-8"))
        except UnicodeError as exc:
            raise ValueError("text contains invalid Unicode") from exc
        if width > byte_limit:
            raise ValueError("one character exceeds the byte limit")
        if current and size + width > byte_limit:
            parts.append("".join(current))
            current = []
            size = 0
        current.append(character)
        size += width
    if current:
        parts.append("".join(current))
    return parts


def _setting(settings: object, modern: str, legacy: str, default: Any = None) -> Any:
    value = getattr(settings, modern, None)
    if value is None or value == "" or value == ():
        return getattr(settings, legacy, default)
    return value


class WeComClient:
    """Thread-safe cached-token client.

    ``transport`` is injectable and all tests use a fake implementation, so
    the default test suite never contacts WeCom.
    """

    def __init__(
        self,
        settings: object,
        transport: Callable[..., dict[str, Any]] = http_json,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.clock = clock
        self._token = ""
        self._expires = 0.0
        self._lock = threading.Lock()

    @property
    def corp_id(self) -> str:
        return str(_setting(self.settings, "wecom_corp_id", "corp_id", ""))

    @property
    def agent_id(self) -> int | None:
        return _setting(self.settings, "wecom_agent_id", "agent_id")

    @property
    def secret(self) -> str:
        return str(_setting(self.settings, "wecom_secret", "secret", ""))

    def default_recipients(self) -> tuple[str, ...]:
        recipients = _setting(self.settings, "wecom_to_users", "to_user", ())
        if isinstance(recipients, str):
            return tuple(value for value in recipients.split("|") if value)
        return tuple(str(value) for value in recipients if str(value))

    def _failure(self, reason: str, response: dict[str, Any]) -> WeComFailure:
        detail = str(response.get("errmsg", ""))
        push_token = str(getattr(self.settings, "push_token", ""))
        for value in (self.secret, push_token, self._token):
            if value:
                detail = detail.replace(value, "[redacted]")
        detail = re.sub(
            r"(?i)(access_token|corpsecret|authorization)=?[^&\s]+",
            r"\1=[redacted]",
            detail,
        )
        code = response.get("errcode")
        return WeComFailure(
            reason,
            code if isinstance(code, int) else None,
            detail[:300],
        )

    def token(self, timeout: float = 12.0) -> str:
        with self._lock:
            if self._token and self.clock() < self._expires:
                return self._token
            if not _configured(self.corp_id) or not _configured(self.secret):
                raise WeComFailure("push_not_configured")
            result = self.transport(
                "GET",
                TOKEN_URL,
                {"corpid": self.corp_id, "corpsecret": self.secret},
                None,
                timeout,
            )
            if result.get("errcode", 0) != 0:
                raise self._failure("token_rejected", result)
            token = result.get("access_token")
            ttl = result.get("expires_in", 7200)
            if (
                not isinstance(token, str)
                or not token
                or isinstance(ttl, bool)
                or not isinstance(ttl, (int, float))
                or ttl <= 0
            ):
                raise WeComFailure("invalid_token_response")
            self._token = token
            self._expires = self.clock() + ttl - min(120.0, ttl / 10.0)
            return token

    def invalidate(self, expected: str) -> None:
        with self._lock:
            if self._token == expected:
                self._token = ""
                self._expires = 0.0

    def send(
        self,
        text: str,
        to_user: str | Iterable[str] | None = None,
        *,
        to_users: str | Iterable[str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send all chunks or raise with the number already accepted.

        ``to_users`` lets management receipts target only their initiating
        member, independently of the monitor's broadcast recipients.
        """

        if not isinstance(text, str) or not text.strip():
            raise ValueError("message text is required")
        if to_user is not None and to_users is not None:
            raise ValueError("pass either to_user or to_users, not both")
        selected_users = to_users if to_users is not None else to_user
        if selected_users is None:
            recipients = self.default_recipients()
        elif isinstance(selected_users, str):
            recipients = tuple(value for value in selected_users.split("|") if value)
        else:
            recipients = tuple(str(value) for value in selected_users if str(value))
        if not recipients:
            raise WeComFailure("recipient_not_configured")
        if not isinstance(self.agent_id, int) or self.agent_id <= 0:
            raise WeComFailure("push_not_configured")

        chunks = split_text(text)
        accepted = 0
        message_ids: list[str] = []
        budget = 70.0 if timeout is None else max(0.0, float(timeout))
        deadline = self.clock() + budget
        try:
            for index, chunk in enumerate(chunks, 1):
                content = (
                    f"[{index}/{len(chunks)}]\n{chunk}" if len(chunks) > 1 else chunk
                )
                if len(content.encode("utf-8")) > MAX_TEXT_BYTES:
                    raise WeComFailure("chunk_too_large")
                for attempt in range(2):
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        raise WeComFailure("push_time_budget_exceeded")
                    # Keep the inlined request loop here so the overall method
                    # can retain its single end-to-end time budget.
                    access_token = self.token(timeout=min(12.0, remaining))
                    remaining = deadline - self.clock()
                    result = self._send_chunk_once(
                        content,
                        recipients,
                        access_token,
                        min(12.0, max(0.001, remaining)),
                    )
                    if result.get("errcode") in TOKEN_ERROR_CODES and attempt == 0:
                        self.invalidate(access_token)
                        continue
                    self._check_send_result(result)
                    accepted += 1
                    msgid = result.get("msgid")
                    if isinstance(msgid, str) and msgid:
                        message_ids.append(msgid)
                    break
        except WeComFailure as exc:
            exc.accepted_chunks = accepted
            raise
        return {"code": 0, "accepted_chunks": accepted, "msgids": message_ids}

    def _send_chunk_once(
        self,
        content: str,
        recipients: tuple[str, ...],
        access_token: str,
        timeout: float,
    ) -> dict[str, Any]:
        return self.transport(
            "POST",
            SEND_URL,
            {"access_token": access_token},
            {
                "touser": "|".join(recipients),
                "agentid": self.agent_id,
                "msgtype": "text",
                "text": {"content": content},
            },
            timeout,
        )

    def _check_send_result(self, result: dict[str, Any]) -> None:
        if result.get("errcode") != 0:
            raise self._failure("message_rejected", result)
        if any(
            result.get(key)
            for key in ("invaliduser", "invalidparty", "invalidtag", "unlicenseduser")
        ):
            # Do not reflect the recipient values; they may be sensitive
            # member identifiers.
            raise WeComFailure("recipient_rejected", result.get("errcode"))

    def send_chunk(
        self,
        content: str,
        to_user: str | Iterable[str] | None = None,
        *,
        timeout: float = 12.0,
    ) -> dict[str, Any]:
        """Send one already-formatted chunk for a persistent delivery worker."""

        if not isinstance(content, str) or not content:
            raise ValueError("chunk content is required")
        try:
            size = len(content.encode("utf-8"))
        except UnicodeError as exc:
            raise ValueError("chunk contains invalid Unicode") from exc
        if size > MAX_TEXT_BYTES:
            raise ValueError("chunk exceeds the WeCom text byte limit")
        if to_user is None:
            recipients = self.default_recipients()
        elif isinstance(to_user, str):
            recipients = tuple(value for value in to_user.split("|") if value)
        else:
            recipients = tuple(str(value) for value in to_user if str(value))
        if not recipients:
            raise WeComFailure("recipient_not_configured")
        if not isinstance(self.agent_id, int) or self.agent_id <= 0:
            raise WeComFailure("push_not_configured")
        accepted = 0
        try:
            for attempt in range(2):
                access_token = self.token(timeout=timeout)
                result = self._send_chunk_once(
                    content, recipients, access_token, timeout
                )
                if result.get("errcode") in TOKEN_ERROR_CODES and attempt == 0:
                    self.invalidate(access_token)
                    continue
                self._check_send_result(result)
                accepted = 1
                msgid = result.get("msgid")
                return {
                    "code": 0,
                    "accepted_chunks": 1,
                    "msgids": [msgid] if isinstance(msgid, str) and msgid else [],
                }
        except WeComFailure as exc:
            exc.accepted_chunks = accepted
            raise
        raise WeComFailure("message_rejected")


__all__ = [
    "CHUNK_BYTES",
    "MAX_TEXT_BYTES",
    "SEND_URL",
    "TOKEN_URL",
    "WeComClient",
    "WeComFailure",
    "http_json",
    "split_text",
]
