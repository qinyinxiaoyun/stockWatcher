# -*- coding: utf-8 -*-
from __future__ import unicode_literals

# Copy this function over send_wechat() in your existing index.py.
# It uses the existing os, json, http_request and json_bytes helpers.
# Keep the repaired Python 2.7 http_request that encodes header names/values.
def send_wechat(alerts, article_map):
    if not alerts:
        return

    messages = []
    for alert in alerts:
        article = article_map[alert["id"]]
        messages.append(
            "[%s] %s\n%s\n"
            "影响方向：%s\n影响机制：%s\n待验证：%s\n来源：%s" % (
                alert["level"], article["company"], article["title"],
                alert.get("impact", "不确定"),
                alert.get("reason", "")[:500],
                alert.get("followup", "")[:300], article["url"]
            )
        )
    content = "AI自动新闻预警，未经独立事实核验。\n\n" + "\n\n---\n\n".join(messages)
    response = http_request(
        os.environ["PUSH_URL"],
        data=json_bytes({"title": "股票重大新闻预警", "content": content}),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": "Bearer " + os.environ["PUSH_TOKEN"]
        },
        timeout=90
    )
    result = json.loads(response)
    if result.get("code") != 0:
        raise RuntimeError("Self-hosted WeChat push failed")
