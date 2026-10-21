"""Offline checks. All WeCom requests are simulated; no messages are sent."""
import importlib.util
import io
import json
import logging
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from relay import Settings, WeComClient, WeComFailure, create_app, http_json, split_text, MAX_REQUEST


def settings(**changes):
    values = dict(push_token="test-auth-token-" + "a" * 40, corp_id="ww-test", agent_id=1000002,
                  secret="test-secret-only", to_user="test-self")
    values.update(changes)
    return Settings(**values)


class FakeTransport:
    def __init__(self, sends=None, token_response=None):
        self.calls, self.tokens = [], 0
        self.sends = list(sends or [])
        self.token_response = token_response

    def __call__(self, method, url, params, payload, timeout):
        self.calls.append((method, url, params, payload, timeout))
        if method == "GET":
            self.tokens += 1
            return self.token_response or {"access_token": "fake-token-%d" % self.tokens, "expires_in": 7200}
        if self.sends:
            return self.sends.pop(0)
        return {"errcode": 0, "errmsg": "ok", "msgid": "fake-msg"}


def invoke(app, payload=None, token=None, method="POST", path="/push", raw=None, content_type="application/json", length=None):
    if raw is None:
        raw = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    environment = {"REQUEST_METHOD": method, "PATH_INFO": path,
                   "CONTENT_TYPE": content_type, "CONTENT_LENGTH": str(len(raw)) if length is None else str(length),
                   "HTTP_AUTHORIZATION": "Bearer " + (token or ""), "wsgi.input": io.BytesIO(raw)}
    result = {}
    def start(status, headers):
        result["status"] = int(status.split()[0])
        result["headers"] = dict(headers)
    result["body"] = b"".join(app(environment, start))
    try:
        result["json"] = json.loads(result["body"])
    except ValueError:
        pass
    return result


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.settings = settings()
        self.fake = FakeTransport()
        self.client = WeComClient(self.settings, self.fake)
        self.app = create_app(self.settings, self.client)

    def call(self, payload=None, **kwargs):
        kwargs.setdefault("token", self.settings.push_token)
        return invoke(self.app, payload, **kwargs)

    def test_unicode_and_emoji_survive_splitting(self):
        text = "股票📈汉字🙂e\u0301" * 1500
        chunks = split_text(text)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk.encode("utf-8")) <= 1900 for chunk in chunks))
        self.assertGreater(len(chunks), 1)

    def test_wrong_auth_never_calls_wecom(self):
        result = self.call({"content": "消息"}, token="incorrect")
        self.assertEqual(result["status"], 401)
        self.assertEqual(self.fake.calls, [])

    def test_non_ascii_auth_is_rejected_without_exception(self):
        self.assertEqual(self.call({"content": "消息"}, token="错误令牌")["status"], 401)

    def test_health_never_calls_wecom(self):
        self.assertEqual(invoke(self.app, method="GET", path="/health")["status"], 200)
        self.assertEqual(self.fake.calls, [])

    def test_plain_chinese_delivery(self):
        result = self.call({"title": "预警", "content": "兆易创新📈"})
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"]["code"], 0)
        sent = self.fake.calls[-1][3]
        self.assertEqual(sent["text"]["content"], "预警\n\n兆易创新📈")
        self.assertEqual(sent["touser"], "test-self")

    def test_tokens_are_cached(self):
        self.call({"content": "一"})
        self.call({"content": "二"})
        self.assertEqual(self.fake.tokens, 1)

    def test_expired_token_is_refreshed_once(self):
        fake = FakeTransport([{"errcode": 42001}, {"errcode": 0}])
        result = WeComClient(self.settings, fake).send("消息")
        self.assertEqual(result["accepted_chunks"], 1)
        self.assertEqual(fake.tokens, 2)

    def test_repeated_bad_token_is_reported(self):
        fake = FakeTransport([{"errcode": 42001}, {"errcode": 42001}])
        with self.assertRaises(WeComFailure):
            WeComClient(self.settings, fake).send("消息")
        self.assertEqual(fake.tokens, 2)

    def test_invalid_recipient_is_not_success(self):
        fake = FakeTransport([{"errcode": 0, "invaliduser": "test-self"}])
        result = invoke(create_app(self.settings, WeComClient(self.settings, fake)),
                        {"content": "消息"}, token=self.settings.push_token)
        self.assertEqual(result["status"], 502)
        self.assertEqual(result["json"]["error"], "recipient_rejected")

    def test_partial_delivery_is_not_success(self):
        fake = FakeTransport([{"errcode": 0}, {"errcode": 40058, "errmsg": "bad message"}])
        with self.assertRaises(WeComFailure) as caught:
            WeComClient(self.settings, fake).send("中文" * 1000)
        self.assertEqual(caught.exception.accepted_chunks, 1)

    def test_wire_messages_stay_below_text_limit(self):
        self.call({"content": "汉🙂" * 2000})
        sent = [row[3]["text"]["content"] for row in self.fake.calls if row[0] == "POST"]
        self.assertTrue(all(len(text.encode("utf-8")) <= 2048 for text in sent))
        self.assertGreater(len(sent), 1)

    def test_api_errors_are_redacted_in_reply_and_logs(self):
        fake = FakeTransport(token_response={"errcode": 40013,
              "errmsg": "corpsecret=" + self.settings.secret + " auth=" + self.settings.push_token})
        app = create_app(self.settings, WeComClient(self.settings, fake))
        with self.assertLogs("wechat_push", logging.WARNING) as logs:
            result = invoke(app, {"content": "消息"}, token=self.settings.push_token)
        output = result["body"].decode() + " ".join(logs.output)
        self.assertNotIn(self.settings.secret, output)
        self.assertNotIn(self.settings.push_token, output)
        self.assertEqual(result["status"], 502)

    def test_network_exception_cannot_leak_secret_url(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("secret-in-url")):
            with self.assertRaises(WeComFailure) as caught:
                http_json("GET", "https://example.invalid", {"corpsecret": "secret-in-url"}, None, 1)
        self.assertNotIn("secret-in-url", str(caught.exception))

    def test_invalid_inputs_never_call_wecom(self):
        for payload, raw, content_type, status in [
            (None, b"not-json", "application/json", 400),
            ([], b"[]", "application/json", 400),
            ({"content": ""}, None, "application/json", 400),
            ({"content": 123}, None, "application/json", 400),
            ({"title": "a" * 257, "content": "x"}, None, "application/json", 400),
            ({"content": "x"}, None, "text/plain", 415),
            ({"content": "汉" * 25000}, None, "application/json", 413),
            (None, b'{"content":"\\ud800"}', "application/json", 400),
        ]:
            with self.subTest(status=status, raw=raw):
                result = self.call(payload, raw=raw, content_type=content_type)
                self.assertEqual(result["status"], status)
        self.assertEqual(self.fake.calls, [])

    def test_size_and_method_guards(self):
        self.assertEqual(self.call(length=MAX_REQUEST + 1)["status"], 413)
        self.assertEqual(self.call(length="")["status"], 411)
        self.assertEqual(self.call(method="GET")["status"], 405)

    def test_only_explicit_verification_file_is_public(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "WW_verify_example.txt").write_text("verification-only")
            app = create_app(settings(verification_dir=directory), self.client)
            result = invoke(app, method="GET", path="/WW_verify_example.txt")
            self.assertEqual(result["body"], b"verification-only")
            self.assertEqual(invoke(app, method="GET", path="/.env")["status"], 404)

    def test_scf_patch_builds_utf8_payload_and_checks_success(self):
        specification = importlib.util.spec_from_file_location("scf_patch", Path(__file__).with_name("scf_send_wechat.py"))
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        module.os, module.json = type("Env", (), {"environ": {"PUSH_URL": "https://push.example.com/push", "PUSH_TOKEN": "dummy"}}), json
        module.json_bytes = lambda value: json.dumps(value, ensure_ascii=False).encode("utf-8")
        requests = []
        module.http_request = lambda url, **kwargs: requests.append((url, kwargs)) or b'{"code":0}'
        module.send_wechat([{"id": "x", "level": "重要", "reason": "中文📈"}],
                           {"x": {"company": "兆易创新", "title": "消息", "url": "https://example.com/news"}})
        payload = json.loads(requests[0][1]["data"].decode("utf-8"))
        self.assertIn("中文📈", payload["content"])
        module.http_request = lambda *args, **kwargs: b'{"code":1}'
        with self.assertRaises(RuntimeError):
            module.send_wechat([{"id": "x", "level": "重要"}],
                               {"x": {"company": "中芯国际", "title": "消息", "url": "https://example.com"}})


if __name__ == "__main__":
    unittest.main()
