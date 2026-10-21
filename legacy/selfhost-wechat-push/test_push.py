"""Explicit one-message smoke test. Run manually on YOUR server after configuring it."""
import json
import os
import urllib.error
import urllib.request


def load_env(path=".env"):
    result = {}
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                result[key.strip()] = value.strip().strip("\"").strip("'")
    return result


if __name__ == "__main__":
    settings = load_env()
    url = os.environ.get("TEST_PUSH_URL", "http://127.0.0.1:8000/push")
    request = urllib.request.Request(
        url,
        data=json.dumps({"title": "自建微信推送测试", "content": "收到这条消息，说明服务已成功调用企业微信消息接口。"}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + settings["PUSH_TOKEN"]}
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            print(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(exc.read().decode("utf-8", "replace"))
        raise SystemExit(1)
    except urllib.error.URLError:
        print("Cannot reach push service. Check service status, DNS and TLS.")
        raise SystemExit(1)
