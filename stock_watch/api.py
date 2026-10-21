"""Pure WSGI HTTP surface for health, push, and encrypted WeCom callbacks."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from http import HTTPStatus
from pathlib import Path
from typing import Any

from . import __version__
from .push import WeComClient, WeComFailure
from .settings import Settings
from .wecom_crypto import (
    MessageFormatError,
    WeComCrypto,
    WeComCryptoError,
    extract_encrypted,
)


LOG = logging.getLogger("stock_watch.api")
MAX_PUSH_REQUEST = 128 * 1024
MAX_PUSH_MESSAGE = 64 * 1024
MAX_CALLBACK_REQUEST = 256 * 1024
_VERIFY_PATH = re.compile(r"/WW_verify_[A-Za-z0-9_-]+\.txt\Z")


def _text(element: ET.Element, name: str) -> str:
    matches = element.findall(name)
    if len(matches) > 1:
        raise MessageFormatError()
    child = matches[0] if matches else None
    return "" if child is None or child.text is None else child.text.strip()


def _configured(value: object, *, minimum: int = 1) -> bool:
    text = str(value).strip()
    return len(text) >= minimum and not text.upper().startswith("CHANGE_ME")


def _xml_root(xml: str | bytes) -> ET.Element:
    try:
        raw = xml.encode("utf-8") if isinstance(xml, str) else bytes(xml)
    except (TypeError, UnicodeError) as exc:
        raise MessageFormatError() from exc
    if not raw or len(raw) > MAX_CALLBACK_REQUEST:
        raise MessageFormatError()
    lowered = raw.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise MessageFormatError()
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, UnicodeError, ValueError) as exc:
        raise MessageFormatError() from exc
    if root.tag != "xml":
        raise MessageFormatError()
    # Bound XML complexity in addition to byte size.  ElementTree does not
    # resolve external entities, and DTDs are rejected above; these limits also
    # avoid pathological nesting or huge numbers of empty elements.
    stack = [(root, 1)]
    nodes = 0
    while stack:
        element, depth = stack.pop()
        nodes += 1
        if nodes > 512 or depth > 32:
            raise MessageFormatError()
        stack.extend((child, depth + 1) for child in element)
    return root


def _all_fields(root: ET.Element) -> dict[str, str | list[str]]:
    result: dict[str, str | list[str]] = {}
    for child in root:
        value = (child.text or "").strip()
        previous = result.get(child.tag)
        if previous is None:
            result[child.tag] = value
        elif isinstance(previous, list):
            previous.append(value)
        else:
            result[child.tag] = [previous, value]
    return result


def _canonical_xml(element: ET.Element) -> list[Any]:
    return [
        element.tag,
        (element.text or "").strip(),
        sorted(element.attrib.items()),
        [_canonical_xml(child) for child in element],
    ]


def _stable_message_id(root: ET.Element, corp_id: str, agent_id: int) -> str:
    raw = json.dumps(
        [corp_id, agent_id, _canonical_xml(root)],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "event:" + hashlib.sha256(raw).hexdigest()


def parse_callback_message(
    plaintext: str,
    *,
    corp_id: str,
    agent_id: int,
    outer_xml: str | bytes | None = None,
) -> dict[str, Any]:
    """Validate application identity and normalize one decrypted callback."""

    root = _xml_root(plaintext)
    outer = _xml_root(outer_xml) if outer_xml is not None else None

    corp_values = [_text(root, "ToUserName")]
    agent_values = [_text(root, "AgentID")]
    if outer is not None:
        corp_values.append(_text(outer, "ToUserName"))
        agent_values.append(_text(outer, "AgentID"))
    if any(value and value != corp_id for value in corp_values):
        raise PermissionError("corp_id_mismatch")
    present_agents = [value for value in agent_values if value]
    if not present_agents:
        raise PermissionError("agent_id_missing")
    try:
        if any(int(value) != agent_id for value in present_agents):
            raise PermissionError("agent_id_mismatch")
    except ValueError as exc:
        raise PermissionError("agent_id_mismatch") from exc

    from_user = _text(root, "FromUserName")
    msg_type = _text(root, "MsgType").lower()
    if (
        not from_user
        or len(from_user) > 256
        or not msg_type
        or len(msg_type) > 32
    ):
        raise MessageFormatError()
    raw_msg_id = _text(root, "MsgId")
    if len(raw_msg_id) > 128:
        raise MessageFormatError()
    message_id = raw_msg_id or _stable_message_id(root, corp_id, agent_id)
    raw_fields = _all_fields(root)
    return {
        "message_id": message_id,
        "msg_id": raw_msg_id or None,
        "from_user": from_user,
        "to_user": _text(root, "ToUserName") or corp_id,
        "agent_id": agent_id,
        "msg_type": msg_type,
        "content": _text(root, "Content") if msg_type == "text" else "",
        "create_time": _text(root, "CreateTime"),
        "event": _text(root, "Event").lower(),
        "event_key": _text(root, "EventKey"),
        "change_type": _text(root, "ChangeType").lower(),
        "raw_fields": raw_fields,
    }


class Application:
    def __init__(
        self,
        settings: Settings,
        *,
        push_client: Any | None = None,
        callback_service: Any | None = None,
        readiness: Callable[[], Any] | None = None,
    ) -> None:
        self.settings = settings
        self.push_client = push_client or WeComClient(settings)
        self._callback_service = callback_service
        self._callback_lock = threading.Lock()
        self.readiness = readiness or self._default_readiness

    @staticmethod
    def _response(
        start_response: Callable[..., Any],
        status: int,
        body: bytes,
        content_type: str,
    ) -> list[bytes]:
        start_response(
            f"{status} {HTTPStatus(status).phrase}",
            [
                ("Content-Type", content_type),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
            ],
        )
        return [body]

    @classmethod
    def _json(
        cls,
        start_response: Callable[..., Any],
        status: int,
        payload: Mapping[str, Any],
    ) -> list[bytes]:
        body = json.dumps(
            dict(payload), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return cls._response(
            start_response, status, body, "application/json; charset=utf-8"
        )

    def _default_readiness(self) -> tuple[bool, str]:
        from .db import SCHEMA_VERSION, connect

        path = self.settings.database_path
        if str(path) != ":memory:" and not Path(path).is_file():
            return False, "database_not_initialized"
        connection = None
        try:
            connection = connect(path)
            actual = int(connection.execute("PRAGMA user_version").fetchone()[0])
            connection.execute("SELECT config_version FROM config_state WHERE singleton=1").fetchone()
            return actual == SCHEMA_VERSION, (
                "ready" if actual == SCHEMA_VERSION else "schema_mismatch"
            )
        except Exception:
            return False, "database_unavailable"
        finally:
            if connection is not None:
                connection.close()

    def _readiness_payload(self) -> tuple[int, dict[str, Any]]:
        try:
            result = self.readiness()
        except Exception as exc:
            LOG.warning("readiness_failed exception_type=%s", type(exc).__name__)
            return 503, {"code": 1, "status": "not_ready", "reason": "check_failed"}
        details: dict[str, Any] = {}
        if isinstance(result, tuple) and len(result) == 2:
            ready = bool(result[0])
            details["reason"] = str(result[1])
        elif isinstance(result, Mapping):
            details = dict(result)
            ready = bool(details.pop("ready", details.get("status") == "ready"))
        else:
            ready = bool(result)
        payload = {
            "code": 0 if ready else 1,
            "status": "ready" if ready else "not_ready",
            "build_sha": self.settings.build_sha,
            **details,
        }
        return (200 if ready else 503), payload

    def _query(self, environ: Mapping[str, Any]) -> dict[str, str]:
        query_string = str(environ.get("QUERY_STRING", ""))
        if len(query_string) > MAX_CALLBACK_REQUEST:
            raise MessageFormatError("invalid_query")
        try:
            values = urllib.parse.parse_qs(
                query_string,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=16,
            )
        except ValueError as exc:
            raise MessageFormatError("invalid_query") from exc
        if any(len(items) != 1 for items in values.values()):
            raise MessageFormatError("invalid_query")
        result = {key: items[0] for key, items in values.items()}
        for key in ("msg_signature", "timestamp", "nonce"):
            if key in result and (not result[key] or len(result[key]) > 256):
                raise MessageFormatError("invalid_query")
        if "echostr" in result and len(result["echostr"]) > MAX_CALLBACK_REQUEST:
            raise MessageFormatError("invalid_query")
        return result

    @staticmethod
    def _read_body(environ: Mapping[str, Any], maximum: int) -> bytes:
        length_text = str(environ.get("CONTENT_LENGTH", ""))
        try:
            length = int(length_text)
        except ValueError as exc:
            raise MessageFormatError("content_length_required") from exc
        if length <= 0:
            raise MessageFormatError("empty_request")
        if length > maximum:
            raise OverflowError
        try:
            body = environ["wsgi.input"].read(length)
        except (KeyError, OSError) as exc:
            raise MessageFormatError("body_read_failed") from exc
        if not isinstance(body, bytes) or len(body) != length:
            raise MessageFormatError("incomplete_request")
        return body

    def _crypto(self) -> WeComCrypto:
        try:
            self.settings.validate_callback()
            if not _configured(self.settings.wecom_corp_id) or not _configured(
                self.settings.wecom_callback_token
            ):
                raise ValueError("callback settings contain placeholders")
            return WeComCrypto(
                self.settings.wecom_callback_token,
                self.settings.wecom_encoding_aes_key,
                self.settings.wecom_corp_id,
            )
        except (ValueError, WeComCryptoError) as exc:
            raise RuntimeError("callback_not_configured") from exc

    def _service(self) -> Any:
        if self._callback_service is not None:
            return self._callback_service
        with self._callback_lock:
            if self._callback_service is None:
                try:
                    from .callback_service import create_callback_service
                except (ImportError, AttributeError) as exc:
                    raise RuntimeError("callback_service_unavailable") from exc
                self._callback_service = create_callback_service(self.settings)
        return self._callback_service

    def _dispatch_callback(self, message: Mapping[str, Any]) -> None:
        service = self._service()
        handler = getattr(service, "handle", None)
        if handler is None:
            handler = getattr(service, "handle_message", None)
        if handler is None and callable(service):
            handler = service
        if handler is None:
            raise RuntimeError("callback_service_invalid")
        handler(dict(message))

    def _verification_file(
        self, path: str, start_response: Callable[..., Any]
    ) -> list[bytes]:
        directory = getattr(
            self.settings,
            "verification_dir",
            os.environ.get("VERIFICATION_DIR", "/app/verification"),
        )
        file = Path(directory) / path[1:]
        try:
            with file.open("rb") as stream:
                body = stream.read(10_241)
        except OSError:
            return self._json(start_response, 404, {"code": 1, "error": "not_found"})
        if len(body) > 10_240:
            return self._json(start_response, 404, {"code": 1, "error": "not_found"})
        return self._response(start_response, 200, body, "text/plain")

    def _push(
        self, environ: Mapping[str, Any], start_response: Callable[..., Any]
    ) -> list[bytes]:
        if environ.get("REQUEST_METHOD") != "POST":
            return self._json(start_response, 405, {"code": 1, "error": "use_post"})
        if not _configured(self.settings.push_token, minimum=32):
            return self._json(
                start_response, 503, {"code": 1, "error": "push_not_configured"}
            )
        try:
            actual = str(environ.get("HTTP_AUTHORIZATION", "")).encode("utf-8")
        except UnicodeError:
            actual = b""
        expected = ("Bearer " + self.settings.push_token).encode("utf-8")
        if not hmac.compare_digest(actual, expected):
            return self._json(
                start_response, 401, {"code": 1, "error": "unauthorized"}
            )
        content_type = str(environ.get("CONTENT_TYPE", "")).split(";", 1)[0]
        if content_type.strip().lower() != "application/json":
            return self._json(
                start_response, 415, {"code": 1, "error": "use_application_json"}
            )
        length_text = str(environ.get("CONTENT_LENGTH", ""))
        try:
            length = int(length_text)
        except ValueError:
            return self._json(
                start_response,
                411,
                {"code": 1, "error": "content_length_required"},
            )
        if length <= 0:
            return self._json(
                start_response, 400, {"code": 1, "error": "empty_request"}
            )
        if length > MAX_PUSH_REQUEST:
            return self._json(
                start_response, 413, {"code": 1, "error": "request_too_large"}
            )
        try:
            raw = self._read_body(environ, MAX_PUSH_REQUEST)
            payload = json.loads(raw.decode("utf-8"))
        except OverflowError:
            return self._json(
                start_response, 413, {"code": 1, "error": "request_too_large"}
            )
        except (MessageFormatError, ValueError, UnicodeError):
            return self._json(
                start_response, 400, {"code": 1, "error": "invalid_json"}
            )
        if not isinstance(payload, dict):
            return self._json(
                start_response, 400, {"code": 1, "error": "expected_json_object"}
            )
        title = payload.get("title", "")
        content = payload.get("content")
        if (
            not isinstance(title, str)
            or len(title) > 256
            or not isinstance(content, str)
            or not content.strip()
        ):
            return self._json(
                start_response,
                400,
                {"code": 1, "error": "invalid_title_or_content"},
            )
        text = f"{title}\n\n{content}" if title else content
        try:
            size = len(text.encode("utf-8"))
        except UnicodeError:
            return self._json(
                start_response, 400, {"code": 1, "error": "invalid_unicode"}
            )
        if size > MAX_PUSH_MESSAGE:
            return self._json(
                start_response, 413, {"code": 1, "error": "message_too_large"}
            )
        request_id = uuid.uuid4().hex
        try:
            result = self.push_client.send(text)
            return self._json(
                start_response, 200, {**result, "request_id": request_id}
            )
        except WeComFailure as exc:
            LOG.warning(
                "push_failed request_id=%s error=%s wecom_code=%s accepted_chunks=%d",
                request_id,
                exc.reason,
                exc.wecom_code,
                exc.accepted_chunks,
            )
            return self._json(
                start_response,
                502,
                {
                    "code": 1,
                    "error": exc.reason,
                    "wecom_errcode": exc.wecom_code,
                    "detail": exc.detail,
                    "accepted_chunks": exc.accepted_chunks,
                    "request_id": request_id,
                },
            )
        except Exception as exc:
            LOG.error(
                "push_failed request_id=%s exception_type=%s",
                request_id,
                type(exc).__name__,
            )
            return self._json(
                start_response,
                500,
                {"code": 1, "error": "internal_error", "request_id": request_id},
            )

    def _callback(
        self, environ: Mapping[str, Any], start_response: Callable[..., Any]
    ) -> list[bytes]:
        method = str(environ.get("REQUEST_METHOD", ""))
        if method not in {"GET", "POST"}:
            return self._json(
                start_response, 405, {"code": 1, "error": "method_not_allowed"}
            )
        try:
            query = self._query(environ)
            signature = query["msg_signature"]
            timestamp = query["timestamp"]
            nonce = query["nonce"]
            crypto = self._crypto()
            if method == "GET":
                echo = query["echostr"]
                plaintext = crypto.verify_url(signature, timestamp, nonce, echo)
                return self._response(
                    start_response,
                    200,
                    plaintext.encode("utf-8"),
                    "text/plain; charset=utf-8",
                )

            body = self._read_body(environ, MAX_CALLBACK_REQUEST)
            encrypted = extract_encrypted(body)
            plaintext = crypto.verify_and_decrypt(
                encrypted, signature, timestamp, nonce
            )
            message = parse_callback_message(
                plaintext,
                corp_id=self.settings.wecom_corp_id,
                agent_id=int(self.settings.wecom_agent_id),
                outer_xml=body,
            )
            allowed = {
                user.casefold() for user in self.settings.wecom_admin_userids if user
            }
            if message["from_user"].casefold() not in allowed:
                raise PermissionError("unauthorized_member")
            self._dispatch_callback(message)
            # Empty HTTP 200 asks WeCom not to retry.  Receipts are durable
            # active messages from the outbox, never broadcast passive replies.
            return self._response(start_response, 200, b"", "text/plain")
        except KeyError:
            return self._json(
                start_response, 400, {"code": 1, "error": "missing_parameter"}
            )
        except OverflowError:
            return self._json(
                start_response, 413, {"code": 1, "error": "request_too_large"}
            )
        except PermissionError:
            return self._json(
                start_response, 403, {"code": 1, "error": "forbidden"}
            )
        except WeComCryptoError as exc:
            status = 403 if exc.reason in {"invalid_signature", "receive_id_mismatch"} else 400
            return self._json(
                start_response, status, {"code": 1, "error": exc.reason}
            )
        except RuntimeError as exc:
            LOG.warning("callback_unavailable reason=%s", str(exc))
            return self._json(
                start_response, 503, {"code": 1, "error": "callback_unavailable"}
            )
        except Exception as exc:
            LOG.error("callback_failed exception_type=%s", type(exc).__name__)
            return self._json(
                start_response, 500, {"code": 1, "error": "internal_error"}
            )

    def __call__(
        self, environ: Mapping[str, Any], start_response: Callable[..., Any]
    ) -> list[bytes]:
        method = str(environ.get("REQUEST_METHOD", ""))
        path = str(environ.get("PATH_INFO", ""))
        if method == "GET" and path == "/health":
            return self._json(
                start_response,
                200,
                {
                    "code": 0,
                    "status": "ok",
                    "version": __version__,
                    "build_sha": self.settings.build_sha,
                },
            )
        if method == "GET" and path == "/ready":
            status, payload = self._readiness_payload()
            return self._json(start_response, status, payload)
        if method == "GET" and _VERIFY_PATH.fullmatch(path):
            return self._verification_file(path, start_response)
        if path == "/push":
            return self._push(environ, start_response)
        if path == "/wecom/callback":
            return self._callback(environ, start_response)
        return self._json(start_response, 404, {"code": 1, "error": "not_found"})


def create_app(
    settings: Settings | None = None,
    client: Any | None = None,
    *,
    push_client: Any | None = None,
    callback_service: Any | None = None,
    readiness: Callable[[], Any] | None = None,
) -> Application:
    """Application factory used by Gunicorn and offline tests.

    ``client`` remains as a compatibility alias for the legacy relay tests.
    """

    if client is not None and push_client is not None:
        raise ValueError("pass either client or push_client, not both")
    return Application(
        settings or Settings.from_env(),
        push_client=push_client if push_client is not None else client,
        callback_service=callback_service,
        readiness=readiness,
    )


__all__ = ["Application", "create_app", "parse_callback_message"]
