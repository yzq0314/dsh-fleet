"""Environment-selected notification sinks. No process-wide environment mutation."""

import base64
import hmac
import os
import time
from urllib.parse import urlsplit

import httpx

KEYS = {
    "FLEET_PASSWORD": "password",
    "FLEET_API_KEY": "api_key",
    "FLEET_WEBHOOK_URL": "webhook",
    "FLEET_WEBHOOK_TOKEN": "webhook_token",
    "FLEET_FEISHU_WEBHOOK_URL": "feishu_webhook",
    "FLEET_FEISHU_SIGNING_SECRET": "feishu_signing_secret",
}


def load_env(config, files):
    values = {}
    for path in files:
        if path.is_file():
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                key, sep, val = line.partition("=")
                if sep and key.strip() in KEYS:
                    val = val.strip()
                    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                        val = val[1:-1]
                    values[key.strip()] = val
    for key, field in KEYS.items():
        value = os.environ.get(key, values.get(key))
        if value:
            config[field] = value
    if url := config.get("feishu_webhook"):
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.hostname not in {"open.feishu.cn", "open.larksuite.com"}
            or parts.username
            or parts.password
            or parts.port not in {None, 443}
            or not parts.path.startswith("/open-apis/bot/v2/hook/")
            or parts.query
            or parts.fragment
        ):
            raise ValueError("请配置官方飞书自定义机器人的 HTTPS Webhook")


async def feishu_notify(client, config, event):
    data = event["data"]
    pending = event["type"] == "approval_required"
    status = "待审批" if pending else "审批已更新"
    lines = [f"节点  {event['node_id']}", f"审批  {data['id']}"]
    if pending:
        lines.extend([str(data.get("reason", "")), str(data.get("command", ""))[:1400]])
    else:
        lines.append(str(data.get("decision", "")) + " · " + str(data.get("cause", "")))
    card = {
        "header": {
            "template": "orange" if pending else "green",
            "title": {"tag": "plain_text", "content": status},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "plain_text", "content": "\n".join(lines)}}
        ],
    }
    if url := config.get("public_url"):
        if urlsplit(url).scheme == "https":
            card["elements"].append(
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "查看审批"},
                            "url": url,
                        }
                    ],
                }
            )
    body = {"msg_type": "interactive", "card": card}
    if secret := config.get("feishu_signing_secret"):
        stamp = str(int(time.time()))
        body.update(
            timestamp=stamp,
            sign=base64.b64encode(
                hmac.digest((stamp + "\n" + secret).encode(), b"", "sha256")
            ).decode(),
        )
    reply = await client.post(config["feishu_webhook"], json=body)
    reply.raise_for_status()
    try:
        if reply.json().get("code", reply.json().get("StatusCode")) != 0:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise httpx.RequestError("Feishu delivery was not acknowledged") from exc
