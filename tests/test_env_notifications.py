import hashlib
import hmac
import json
from base64 import b64decode

import httpx
import pytest

from fleet_hub.notifications import KEYS, feishu_notify, load_env


async def test_env_file_and_custom_bot_receipts(tmp_path, monkeypatch):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    private = tmp_path / ".env"
    private.write_text(
        "FLEET_FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/example\nFLEET_FEISHU_SIGNING_SECRET=fixture\n"
    )
    cfg = {}
    load_env(cfg, [private])
    sent = []

    async def remote(req):
        value = json.loads(req.content)
        sent.append(value)
        expected = hmac.new(
            (value["timestamp"] + "\nfixture").encode(), b"", hashlib.sha256
        ).digest()
        assert hmac.compare_digest(b64decode(value["sign"]), expected)
        return httpx.Response(200, json={"code": 0 if len(sent) == 1 else 1})

    event = {
        "type": "approval_required",
        "node_id": "a",
        "data": {"id": "1", "command": "check"},
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        await feishu_notify(client, cfg, event)
        with pytest.raises(httpx.RequestError):
            await feishu_notify(client, cfg, event)
    assert sent[0]["msg_type"] == "interactive"
    monkeypatch.setenv("FLEET_FEISHU_WEBHOOK_URL", "http://example.test/")
    with pytest.raises(ValueError):
        load_env(cfg, [private])


async def test_sinks_resume_independently_after_restart(tmp_path):
    from fleet_hub.engine import Engine
    from fleet_hub.store import Store

    file = tmp_path / "notify.jsonl"
    config = {
        "database": str(tmp_path / "hub.db"),
        "file_hook": str(file),
        "webhook": "http://example.test/generic",
        "feishu_webhook": "https://example.test/card",
    }
    events = [
        {"id": n, "node_id": "n", "type": kind, "data": {"id": "approval"}}
        for n, kind in enumerate(("approval_required", "approval_resolved"), start=1)
    ]
    pending = [True]
    cards = []
    delivered = []

    def receiver(req):
        if req.url.path == "/generic":
            if pending[0]:
                return httpx.Response(503)
            delivered.append(req.headers["Idempotency-Key"])
        else:
            cards.append(json.loads(req.content)["card"]["header"]["title"]["content"])
        return httpx.Response(200, json={"code": 0})

    for first in (True, False):
        store = Store(config)
        await store.open()
        engine = Engine(config, store)
        await engine.client.aclose()
        engine.client = httpx.AsyncClient(transport=httpx.MockTransport(receiver))
        try:
            if first:
                async with store.transaction() as db:
                    await db.executemany(
                        "INSERT INTO hooks VALUES (?,?)",
                        [(e["id"], json.dumps(e)) for e in events],
                    )
            else:
                pending[0] = False
            await engine.flush_notifications()
            assert len(await store.rows("SELECT * FROM hooks")) == (2 if first else 0)
            assert [
                json.loads(s) for s in file.read_text(encoding="utf8").splitlines()
            ] == events
            assert cards == ["待审批", "审批已更新"]
        finally:
            await engine.client.aclose()
            await store.close()
    assert delivered == ["1", "2"]
