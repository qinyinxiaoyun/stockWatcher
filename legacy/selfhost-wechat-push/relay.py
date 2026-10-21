"""Small authenticated WSGI bridge to the WeCom application-message API."""
import hmac
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path

LOG = logging.getLogger("wechat_push")
MAX_REQUEST = 128 * 1024
MAX_MESSAGE = 64 * 1024
CHUNK_BYTES = 1900
TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
SEND_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/send"


@dataclass(frozen=True)
class Settings:
    push_token: str = field(repr=False)
    corp_id: str
    agent_id: int
    secret: str = field(repr=False)
    to_user: str
    verification_dir: str = "/app/verification"

    @classmethod
    def from_env(cls):
        names = ("PUSH_TOKEN", "WECOM_CORP_ID", "WECOM_AGENT_ID",
                 "WECOM_SECRET", "WECOM_TOUSER")
        values = {name: os.environ.get(name, "").strip() for name in names}
        missing = [name for name, value in values.items()
                   if not value or value.startswith("CHANGE_ME")]
        if missing:
            raise ValueError("Configure environment variables: " + ", ".join(missing))
        if len(values["PUSH_TOKEN"]) < 32:
            raise ValueError("PUSH_TOKEN must contain at least 32 characters")
        try:
            agent = int(values["WECOM_AGENT_ID"])
            if agent <= 0:
                raise ValueError()
        except ValueError:
            raise ValueError("WECOM_AGENT_ID must be a positive integer") from None
        return cls(values["PUSH_TOKEN"], values["WECOM_CORP_ID"], agent,
                   values["WECOM_SECRET"], values["WECOM_TOUSER"],
                   os.environ.get("VERIFICATION_DIR", "/app/verification"))


class WeComFailure(Exception):
    def __init__(self, reason, wecom_code=None, detail=""):
        super().__init__(reason)
        self.reason = reason
        self.wecom_code = wecom_code
        self.detail = detail
        self.accepted_chunks = 0


def http_json(method, url, params, payload, timeout):
    """Never expose urllib exceptions: their URLs can contain credentials."""
    address = url + "?" + urllib.parse.urlencode(params)
    data = None if payload is None else json.dumps(
        payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(address, data=data, method=method, headers={
        "Accept": "application/json", "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "selfhost-wechat-push/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read(128 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise WeComFailure("upstream_http_error", detail="HTTP " + str(status)) from None
    except (urllib.error.URLError, OSError, TimeoutError):
        raise WeComFailure("upstream_unreachable") from None
    if len(raw) > 128 * 1024:
        raise WeComFailure("upstream_response_too_large")
    try:
        result = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        raise WeComFailure("upstream_invalid_json") from None
    if not isinstance(result, dict):
        raise WeComFailure("upstream_invalid_json")
    return result


def split_text(text, byte_limit=CHUNK_BYTES):
    parts, current, size = [], [], 0
    for character in text:
        width = len(character.encode("utf-8"))
        if current and size + width > byte_limit:
            parts.append("".join(current))
            current, size = [], 0
        current.append(character)
        size += width
    if current:
        parts.append("".join(current))
    return parts


class WeComClient:
    def __init__(self, settings, transport=http_json, clock=time.monotonic):
        self.settings, self.transport, self.clock = settings, transport, clock
        self._token, self._expires = "", 0.0
        self._lock = threading.Lock()

    def _failure(self, reason, response):
        detail = str(response.get("errmsg", ""))
        for value in (self.settings.secret, self.settings.push_token, self._token):
            if value:
                detail = detail.replace(value, "[redacted]")
        detail = re.sub(r"(?i)(access_token|corpsecret)=[^&\s]+", r"\1=[redacted]", detail)
        return WeComFailure(reason, response.get("errcode"), detail[:300])

    def token(self, timeout=12):
        with self._lock:
            if self._token and self.clock() < self._expires:
                return self._token
            result = self.transport("GET", TOKEN_URL, {
                "corpid": self.settings.corp_id, "corpsecret": self.settings.secret
            }, None, timeout)
            if result.get("errcode", 0) != 0:
                raise self._failure("token_rejected", result)
            token, ttl = result.get("access_token"), result.get("expires_in", 7200)
            if not isinstance(token, str) or not token or not isinstance(ttl, (int, float)) or ttl <= 0:
                raise WeComFailure("invalid_token_response")
            self._token = token
            self._expires = self.clock() + ttl - min(120, ttl / 10)
            return token

    def invalidate(self, expected):
        with self._lock:
            if self._token == expected:
                self._token, self._expires = "", 0.0

    def send(self, text):
        chunks = split_text(text)
        accepted, message_ids = 0, []
        deadline = self.clock() + 70
        try:
            for index, chunk in enumerate(chunks, 1):
                content = "[%d/%d]\n%s" % (index, len(chunks), chunk) if len(chunks) > 1 else chunk
                for attempt in range(2):
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        raise WeComFailure("push_time_budget_exceeded")
                    token = self.token(timeout=min(12, remaining))
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        raise WeComFailure("push_time_budget_exceeded")
                    result = self.transport("POST", SEND_URL, {"access_token": token}, {
                        "touser": self.settings.to_user,
                        "agentid": self.settings.agent_id,
                        "msgtype": "text", "text": {"content": content}
                    }, min(12, remaining))
                    if result.get("errcode") in (40014, 42001) and attempt == 0:
                        self.invalidate(token)
                        continue
                    if result.get("errcode") != 0:
                        raise self._failure("message_rejected", result)
                    if any(result.get(key) for key in ("invaliduser", "invalidparty", "invalidtag", "unlicenseduser")):
                        raise WeComFailure("recipient_rejected", result.get("errcode"))
                    accepted += 1
                    if result.get("msgid"):
                        message_ids.append(result["msgid"])
                    break
        except WeComFailure as exc:
            exc.accepted_chunks = accepted
            raise
        return {"code": 0, "accepted_chunks": accepted, "msgids": message_ids}


class PushApp:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client or WeComClient(settings)

    def __call__(self, environ, start_response):
        def reply(status, data):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            start_response("%d %s" % (status, HTTPStatus(status).phrase), [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(body))), ("Cache-Control", "no-store")])
            return [body]

        method, path = environ.get("REQUEST_METHOD"), environ.get("PATH_INFO", "")
        if method == "GET" and path == "/health":
            return reply(200, {"code": 0, "status": "ok"})
        # Optional public file verification for a domain owned by the operator.
        if method == "GET" and re.fullmatch(r"/WW_verify_[A-Za-z0-9_-]+\.txt", path):
            file = Path(self.settings.verification_dir) / path[1:]
            try:
                with file.open("rb") as stream:
                    body = stream.read(10241)
            except OSError:
                return reply(404, {"code": 1, "error": "not_found"})
            if len(body) > 10240:
                return reply(404, {"code": 1, "error": "not_found"})
            start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))])
            return [body]
        if path != "/push":
            return reply(404, {"code": 1, "error": "not_found"})
        if method != "POST":
            return reply(405, {"code": 1, "error": "use_post"})
        actual = environ.get("HTTP_AUTHORIZATION", "").encode("utf-8")
        expected = ("Bearer " + self.settings.push_token).encode("utf-8")
        if not hmac.compare_digest(actual, expected):
            return reply(401, {"code": 1, "error": "unauthorized"})
        if environ.get("CONTENT_TYPE", "").split(";")[0].lower().strip() != "application/json":
            return reply(415, {"code": 1, "error": "use_application_json"})
        try:
            length = int(environ.get("CONTENT_LENGTH", ""))
        except ValueError:
            return reply(411, {"code": 1, "error": "content_length_required"})
        if length > MAX_REQUEST:
            return reply(413, {"code": 1, "error": "request_too_large"})
        if length <= 0:
            return reply(400, {"code": 1, "error": "empty_request"})
        try:
            raw = environ["wsgi.input"].read(length)
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, OSError):
            return reply(400, {"code": 1, "error": "invalid_json"})
        if not isinstance(payload, dict):
            return reply(400, {"code": 1, "error": "expected_json_object"})
        title, content = payload.get("title", ""), payload.get("content")
        if not isinstance(title, str) or len(title) > 256 or not isinstance(content, str) or not content.strip():
            return reply(400, {"code": 1, "error": "invalid_title_or_content"})
        text = title + "\n\n" + content if title else content
        try:
            size = len(text.encode("utf-8"))
        except UnicodeError:
            return reply(400, {"code": 1, "error": "invalid_unicode"})
        if size > MAX_MESSAGE:
            return reply(413, {"code": 1, "error": "message_too_large"})
        request_id = uuid.uuid4().hex
        try:
            result = self.client.send(text)
            result["request_id"] = request_id
            return reply(200, result)
        except WeComFailure as exc:
            LOG.warning("push_failed request_id=%s error=%s wecom_code=%s accepted_chunks=%d",
                        request_id, exc.reason, exc.wecom_code, exc.accepted_chunks)
            return reply(502, {"code": 1, "error": exc.reason, "wecom_errcode": exc.wecom_code,
                               "detail": exc.detail, "accepted_chunks": exc.accepted_chunks,
                               "request_id": request_id})
        except Exception as exc:
            LOG.error("push_failed request_id=%s exception_type=%s", request_id, type(exc).__name__)
            return reply(500, {"code": 1, "error": "internal_error", "request_id": request_id})


def create_app(settings=None, client=None):
    return PushApp(settings or Settings.from_env(), client)
