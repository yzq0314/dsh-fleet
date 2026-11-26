import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from fleet_hub.app import create_app
from fleet_hub.models import Enroll, NodeEdit
from pydantic import TypeAdapter, ValidationError


def config(tmp_path):
    return {
        "database": str(tmp_path / "hub.sqlite"),
        "password": secrets.token_urlsafe(16),
        "api_key": secrets.token_urlsafe(32),
        "allowed_cidrs": ["127.0.0.0/8"],
        "poll_interval": 10,
    }


@contextmanager
def running(cfg, peer="127.0.0.1"):
    with TestClient(create_app(cfg), client=(peer, 4090)) as client:
        yield client


def credentials(cfg):
    return {"Authorization": "Bearer " + cfg["api_key"]}


@pytest.mark.parametrize("model,fields", [(NodeEdit, {}), (Enroll, {"token": "test", "alias": "Demo"})])
def test_node_endpoint_validators_handle_pydantic_context(model, fields):
    adapter = TypeAdapter(model)
    normalized = adapter.validate_python({**fields, "url": "http://node.test:8766/"}, context={"source": "request"})
    assert normalized.url == "http://node.test:8766"
    with pytest.raises(ValidationError):
        adapter.validate_python({**fields, "url": "http://node.test:8766/path"}, context={"source": "request"})


def test_dsh_web_bookmark_follows_paired_node_address_and_can_be_removed(tmp_path):
    cfg = config(tmp_path)
    cfg["nodes"] = [{
        "id": "demo", "alias": "Demo", "url": "http://worker.example.ts.net:8090",
        "secret": secrets.token_urlsafe(32), "enabled": False,
    }]
    auth = credentials(cfg)
    with running(cfg) as client:
        def edit(**fields):
            return client.patch("/api/nodes/demo", json=fields, headers=auth)

        initial = client.get("/api/nodes", headers=auth).json()[0]
        assert initial["dsh_web_url"] is None
        assert edit(dsh_web_port=8443).json()["dsh_web_url"] == "https://worker.example.ts.net:8443/"
        assert edit(url="http://[fd00::10]:8090", dsh_web_scheme="http").json()["dsh_web_url"] == "http://[fd00::10]:8443/"
        assert edit(url="http://127.0.0.1:8090").status_code == 422
        assert edit(dsh_web_port=8090).status_code == 422
        assert edit(dsh_web_port=65536).status_code == 422
        assert edit(dsh_web_url="https://example.invalid/?token=fixture").status_code == 422
        assert edit(dsh_web_port=None).json()["dsh_web_url"] is None
    with running(cfg) as client:
        assert client.get("/api/nodes", headers=auth).json()[0]["dsh_web_url"] is None


def test_dsh_web_bookmark_only_builds_private_roots():
    from fleet_hub.models import dsh_web_entry

    for host in ("10.0.0.2", "100.64.0.2", "worker", "worker.local", "team.home.arpa"):
        assert dsh_web_entry({"url": f"http://{host}:1", "dsh_web_port": 80}) == f"https://{host}:80/"
    for host in ("localhost", "[::1]", "1.1.1.1", "2130706433", "0x08080808",
                 "example.org", "0.0.0.0", "relay.ts.net.attacker.invalid", "example%2f.invalid"):
        assert dsh_web_entry({"url": f"http://{host}:1", "dsh_web_port": 80}) is None


def test_readiness_requires_allowed_peer_credentials_and_readable_store(tmp_path):
    cfg = config(tmp_path)
    with running(cfg) as client:
        assert client.get("/health").status_code == 401
        response = client.get("/health", headers=credentials(cfg))
        assert response.status_code == 200 and response.json() == {"ready": True}
        assert response.headers["cache-control"] == "no-store"
        with sqlite3.connect(cfg["database"]) as db:
            db.execute("DELETE FROM metadata WHERE key='settings'")
        assert client.get("/health", headers=credentials(cfg)).status_code == 503
    with running(cfg, "192.0.2.4") as client:
        assert client.get("/health", headers=credentials(cfg)).status_code == 403


def test_cookie_origin_ip_and_bearer(tmp_path):
    cfg = config(tmp_path)
    with running(cfg) as client:
        assert client.get("/api/me").json() == {"authenticated": False}
        assert client.get("/api/nodes").status_code == 401
        assert (
            client.get(
                "/api/nodes", headers={"Authorization": "Bearer wrong"}
            ).status_code
            == 401
        )
        assert (
            client.post("/api/login", json={"password": cfg["password"]}).status_code
            == 200
        )
        assert (
            "HttpOnly"
            in client.cookies.jar._cookies["testserver.local"]["/"][
                "fleet_session"
            ]._rest
        )
        assert client.get("/api/nodes").status_code == 200
        assert (
            client.patch(
                "/api/settings",
                json={"theme": "light"},
                headers={"Origin": "https://cross.invalid"},
            ).status_code
            == 403
        )
        assert client.post("/api/logout").status_code == 200
        assert client.get("/api/nodes").status_code == 401
        assert client.get("/api/nodes", headers=credentials(cfg)).status_code == 200
    with running(cfg, "192.0.2.4") as client:
        assert (
            client.get(
                "/api/nodes",
                headers={**credentials(cfg), "X-Forwarded-For": "127.0.0.1"},
            ).status_code
            == 403
        )


def test_enrollment_single_use_readdress_and_revoke(tmp_path):
    cfg = config(tmp_path)
    auth = credentials(cfg)
    with running(cfg) as client:
        token = client.post("/api/join-tokens", headers=auth).json()["token"]
        entry = {
            "url": "http://127.0.0.1:1",
            "alias": "Example",
            "token": token,
            "tags": ["lab"],
        }
        with ThreadPoolExecutor(max_workers=3) as pool:
            replies = list(
                pool.map(lambda _: client.post("/api/enroll", json=entry), range(3))
            )
        assert sorted(r.status_code for r in replies) == [200, 403, 403]
        identity = next(r.json() for r in replies if r.status_code == 200)
        nid = identity["id"]
        node_auth = {"Authorization": "Bearer " + identity["secret"]}
        assert (
            client.post(
                "/api/node/verify", json={"id": nid}, headers=node_auth
            ).status_code
            == 200
        )
        assert (
            client.patch(
                "/api/nodes/" + nid,
                json={"url": "http://agent.example.test:8770", "alias": "Updated"},
                headers=auth,
            ).status_code
            == 200
        )
        public = client.get("/api/nodes", headers=auth).json()
        assert public[0]["url"].endswith(".test:8770") and "secret" not in public[0]
        assert client.delete("/api/nodes/" + nid, headers=auth).status_code == 200
        assert (
            client.post(
                "/api/node/verify", json={"id": nid}, headers=node_auth
            ).status_code
            == 403
        )
    with running(cfg) as client:
        assert not client.get("/api/nodes", headers=auth).json()


def test_expired_pair_code(tmp_path):
    cfg = config(tmp_path)
    with running(cfg) as client:
        token = client.post("/api/join-tokens", headers=credentials(cfg)).json()[
            "token"
        ]
        with sqlite3.connect(cfg["database"]) as db:
            db.execute("UPDATE tokens SET expires=0")
        assert (
            client.post(
                "/api/enroll",
                json={"token": token, "url": "http://127.0.0.1", "alias": "demo"},
            ).status_code
            == 403
        )


def test_tls_terminating_proxy_requires_configured_origin(tmp_path):
    cfg = config(tmp_path)
    cfg["public_url"] = "https://fleet.example.test"
    with running(cfg) as client:
        response = client.post(
            "/api/login",
            json={"password": cfg["password"]},
            headers={"Origin": cfg["public_url"]},
        )
        assert (
            response.status_code == 200 and "Secure" in response.headers["set-cookie"]
        )
        forged = {
            "Origin": "https://untrusted.example.test",
            "X-Forwarded-Host": "untrusted.example.test",
            "X-Forwarded-Proto": "https",
        }
        assert (
            client.post(
                "/api/login", json={"password": cfg["password"]}, headers=forged
            ).status_code
            == 403
        )


def test_settings_and_seed_once(tmp_path):
    cfg = config(tmp_path)
    cfg["nodes"] = [
        {
            "id": "seed",
            "url": "http://127.0.0.1:1",
            "secret": secrets.token_hex(24),
            "alias": "Initial",
        }
    ]
    auth = credentials(cfg)
    with running(cfg) as client:
        assert len(client.get("/api/macros", headers=auth).json()) == 4
        assert client.delete("/api/nodes/seed", headers=auth).status_code == 200
        assert (
            client.patch("/api/settings", json={"theme": "light"}, headers=auth).json()[
                "theme"
            ]
            == "light"
        )
        assert (
            client.post(
                "/api/services",
                json={"url": "javascript:alert(1)", "name": "bad"},
                headers=auth,
            ).status_code
            == 422
        )
    with running(cfg) as client:
        assert client.get("/api/settings", headers=auth).json()["theme"] == "light"
        assert not client.get("/api/nodes", headers=auth).json()


def test_appearance_upgrade_and_sync(tmp_path):
    import json
    cfg = config(tmp_path)
    auth = credentials(cfg)
    url = "/api/ui-preferences"
    with running(cfg) as client:
        assert client.get(url).status_code == 401
        assert client.put(url, json={}).status_code == 401
    with sqlite3.connect(cfg["database"]) as db:
        db.execute("DROP TABLE ui_preferences")
        db.execute("DROP TABLE ui_wallpaper")
        settings = json.loads(db.execute("SELECT value FROM metadata WHERE key='settings'").fetchone()[0])
        settings.update(theme="midnight", accent="amber")
        db.execute("UPDATE metadata SET value=? WHERE key='settings'",(json.dumps(settings),))
        existing = db.execute("SELECT COUNT(*) FROM macros").fetchone()
    with running(cfg) as client:
        prefs = client.get(url, headers=auth).json()
        assert prefs["theme"] == "midnight" and prefs["accent"] == "#f4c382"
        for invalid in ({"layout":{"mobile":["devices","devices"]}}, {"theme":"unsupported"},
                        {"css":"body{display:none}"},{"shortcuts":{"demo":["external:bad"]}},
                        {"accent":"red"},{"blur":33},{"url":"https://example.invalid/"}):
            assert client.put(url,headers=auth,json=invalid).status_code == 422
        prefs.update(theme="paper",layout={"mobile":["devices"],"desktop":["topology","devices"]})
        assert client.put(url,headers=auth,json=prefs).json()==prefs
    with running(cfg) as client:
        assert client.get(url,headers=auth).json()==prefs
    with sqlite3.connect(cfg["database"]) as db:
        assert db.execute("SELECT COUNT(*) FROM macros").fetchone()==existing


def test_wallpaper_auth_size_type_and_roundtrip(tmp_path):
    from pathlib import Path
    cfg = config(tmp_path)
    path = "/api/ui-preferences/wallpaper"
    with running(cfg) as client:
        auth=credentials(cfg)
        assert client.get(path).status_code==401
        assert client.get(path,headers=auth).status_code==404
        image=(Path(__file__).resolve().parents[1]/"apps/web-pwa/public/icon-192.png").read_bytes()
        for mime,content,status in (("text/html",b"<svg/>",415),("image/jpeg",image,415),
                                    ("image/png",bytes(2*1024**2+1),413),("image/png",image,200)):
            assert client.put(path,headers={**auth,"Content-Type":mime},content=content).status_code==status
        result=client.get(path,headers=auth)
        assert result.content==image and result.headers["content-type"]=="image/png"
