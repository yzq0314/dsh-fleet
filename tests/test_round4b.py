import json
import time

import httpx

from fleet_hub.alerts import Alerts
from fleet_hub.store import Store
from fleet_node.pocket import probe_pocket
from test_hub import config, credentials, running


async def test_pocket_fixed_loopback_and_explicit_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    settings = tmp_path / "dsh-pocket" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"proxyPort": 3011}))
    visited = []

    def reply(req):
        visited.append((req.url.host, req.url.port))
        return httpx.Response(401)

    transport = httpx.MockTransport(reply)
    assert (await probe_pocket({}, transport))["pocket_port"] == 3011
    assert (await probe_pocket({"pocket_port": 3022}, transport))["pocket_status"] == "ready"
    await probe_pocket({"pocket_port": "http://other.invalid/path"}, transport)
    assert visited == [("127.0.0.1", 3011), ("127.0.0.1", 3022)]


async def test_alert_cooldown_recovery_and_distinct_samples(tmp_path):
    cfg = {**config(tmp_path), "file_hook": str(tmp_path / "out")}
    store = Store(cfg)
    await store.open()
    try:
        alerts = Alerts(store, cfg)
        async with store.transaction() as db:
            await db.execute("INSERT INTO gpu_watches VALUES ('lab',0,0,0)")
        stamp = int(time.time())
        def sample(delta, disk, usage):
            return dict(fresh=True, name="lab", timestamp=stamp+delta, disk=disk, gpu=[dict(utilization=usage, used_mib=12)])
        for delta, disk, util in [(0,96,1),(0,98,1),(15,98,32),(30,90,1),(45,96,1),(60,98,1)]:
            await alerts.observe([sample(delta,disk,util)],stamp+delta)
        kinds = [json.loads(r["payload"])["data"]["kind"] for r in await store.rows("SELECT * FROM hooks ORDER BY event_id")]
        assert kinds == ["disk","disk","gpu"]
        assert not await store.rows("SELECT * FROM gpu_watches")
        await alerts.observe([sample(22000,99,1)],stamp+22000)
        assert len(await store.rows("SELECT * FROM hooks")) == 4
    finally:
        await store.close()


def test_rules_and_macro_tags_survive_restart(tmp_path):
    cfg = config(tmp_path)
    auth = credentials(cfg)
    with running(cfg) as c:
        assert c.get("/api/notification-rules").status_code == 401
        prefs = c.get("/api/notification-rules", headers=auth).json()
        assert c.put("/api/notification-rules", headers=auth, json={**prefs, "approvals":False}).status_code == 422
        assert c.put("/api/notification-rules", headers=auth, json={**prefs, "offline":True}).is_success
        m = c.post("/api/macros",headers=auth,json={"name":"Summary","template":"{{target}}","target_tags":["gpu","gpu"]}).json()
        assert m["target_tags"] == ["gpu"]
        assert c.patch("/api/macros/"+m["id"],headers=auth,json={"position":7}).json()["target_tags"] == ["gpu"]
    with running(cfg) as c:
        assert c.get("/api/notification-rules", headers=auth).json()["offline"]
