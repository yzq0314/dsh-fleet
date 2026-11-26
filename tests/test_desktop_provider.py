import asyncio
import hashlib
import hmac
import importlib.util
import json
import secrets
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from fleet_node.agents import DshAdapter
from fleet_node.provider import resolve_route, select_live_route
from packages.protocol.adapters import TaskRequest


def test_private_profile_install_roundtrip(tmp_path):
    path = Path(__file__).resolve().parents[1] / "integrations/dsh-app-plugin/manage.py"
    spec = importlib.util.spec_from_file_location("fleet_plugin_manager", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    manifest = tmp_path / "profiles/desktop/package.json"
    manifest.parent.mkdir(parents=True)
    original = {
        "name": "fixture",
        "dsh": {"profile": {"bundles": ["retained"]}},
        "dependencies": {"retained": "1"},
    }
    manifest.write_text(json.dumps(original), encoding="utf8")
    secret = secrets.token_hex(32)
    cfg = tmp_path / "node.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {"secret": secret, "node_id": "test", "hub_url": "http://localhost"}
        )
    )
    module.manage("install", tmp_path, cfg)
    config = json.loads((tmp_path / "fleet-desktop/config.json").read_text())
    assert (
        config["token"]
        == hmac.new(
            secret.encode(), b"dsh-fleet/desktop/v1", hashlib.sha256
        ).hexdigest()
    )
    assert secret not in json.dumps(config)
    module.manage("install", tmp_path, cfg)
    assert (
        json.loads(manifest.read_text())["dsh"]["profile"]["bundles"].count(module.NAME)
        == 1
    )
    module.manage("uninstall", tmp_path)
    assert json.loads(manifest.read_text()) == original
    assert not (tmp_path / "fleet-desktop/config.json").exists()


def test_resolver_origin_precedence_and_redaction(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    key = secrets.token_hex(32)
    monkeypatch.setenv("FIXTURE_MODEL_KEY", key)
    cfg = {
        "type": "openai-compatible",
        "api_key_env": "FIXTURE_MODEL_KEY",
        "model": "demo",
        "base_url": "https://model.example/v1",
        "credential_origin": "https://model.example",
    }
    assert (
        resolve_route({"dsh_provider": cfg}).environment["FLEET_SELECTED_API_KEY"]
        == key
    )
    assert key not in repr(resolve_route({"dsh_provider": cfg}))
    with pytest.raises(ValueError, match="origin"):
        resolve_route(
            {"dsh_provider": {**cfg, "base_url": "https://different.example/v1"}}
        )
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "DEEPSEEK_API_KEY=" + key + "\nDSH_FLEET_MODEL=fixture\n", encoding="utf8"
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", secrets.token_hex(32))
    assert resolve_route({}).source == "dsh-home-env"
    marker = tmp_path / "fleet-desktop/bridge.json"
    marker.parent.mkdir()
    marker.write_text(
        json.dumps(
            {
                "url": "http://127.0.0.1:10001",
                "model": "selected",
                "provider": "deepseek-account",
                "token": secrets.token_urlsafe(36),
            }
        )
    )
    assert resolve_route({}).provider == "fleet-desktop"
    assert resolve_route({"dsh_provider": cfg}).source == "sidecar"


async def test_offline_desktop_falls_back_only_in_automatic_mode(tmp_path):
    import socket

    token = secrets.token_urlsafe(36)
    private = tmp_path / "fleet-desktop"
    private.mkdir()
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=" + secrets.token_hex(32))
    with socket.socket() as idle:
        idle.bind(("127.0.0.1", 0))
        marker = {
            "url": f"http://127.0.0.1:{idle.getsockname()[1]}",
            "token": token,
            "model": "test",
            "provider": "test",
        }
        (private / "bridge.json").write_text(json.dumps(marker))
        assert (
            await select_live_route({"dsh_home": str(tmp_path)})
        ).source == "dsh-home-env"
        with pytest.raises(RuntimeError, match="offline"):
            await select_live_route(
                {"dsh_home": str(tmp_path), "dsh_provider": {"type": "desktop"}}
            )


@pytest.mark.skipif(not shutil.which("dsh"), reason="DSH runtime unavailable")
@pytest.mark.parametrize(
    "credential_source", ["explicit", "official-profile", "local-env"]
)
async def test_actual_openai_compatible_route(tmp_path, monkeypatch, credential_source):
    seen = []
    key = secrets.token_hex(32)

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(
                (self.path, self.headers.get("Authorization") == f"Bearer {key}", data)
            )
            frame = {
                "id": "fixture",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "local-test",
            }
            lines = []
            for delta, reason in [
                ({"role": "assistant", "content": "local routed answer"}, None),
                ({}, "stop"),
            ]:
                lines.append(
                    "data: "
                    + json.dumps(
                        {
                            **frame,
                            "choices": [
                                {"index": 0, "delta": delta, "finish_reason": reason}
                            ],
                        }
                    )
                )
            payload = ("\n\n".join([*lines, "data: [DONE]"]) + "\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("FIXTURE_MODEL_KEY", key)
    config = {
        "dsh_home": str(tmp_path),
        "dsh_provider": {
            "type": "openai-compatible",
            "model": "local-test",
            "base_url": endpoint + "/v1",
            "credential_origin": endpoint,
            "api_key_env": "FIXTURE_MODEL_KEY",
        },
    }
    if credential_source == "official-profile":
        profile = tmp_path / "profiles/sdk"
        profile.mkdir(parents=True)
        (profile / "cordis.patch.yml").write_text(
            yaml.safe_dump(
                [
                    {
                        "id": "agent-default-model",
                        "config": {"provider": "fixture", "model": "local-test"},
                    },
                    {
                        "id": "llm-pi-ai",
                        "config": {
                            "providers": {
                                "fixture": {
                                    "baseURL": endpoint + "/v1",
                                    "api": "openai-completions",
                                    "apiKeyEnv": "FIXTURE_MODEL_KEY",
                                    "models": [{"id": "local-test"}],
                                }
                            }
                        },
                    },
                ]
            )
        )
        vault = tmp_path / ".credentials.yaml"
        vault.write_text(
            yaml.safe_dump({"version": 1, "refs": {"FIXTURE_MODEL_KEY": key}})
        )
        vault.chmod(0o600)
        initial_vault = vault.read_bytes()
        monkeypatch.setenv("FIXTURE_MODEL_KEY", secrets.token_hex(32))
        config = {
            "dsh_home": str(tmp_path),
            "dsh_profile": "sdk",
        }
        route = resolve_route(config)
        assert route.source == "dsh-profile" and not route.environment
    if credential_source == "local-env":
        private = tmp_path / ".env"
        private.write_text(
            f"OPENAI_BASE_URL={endpoint}/v1\nOPENAI_API_KEY={key}\nOPENAI_MODEL=local-test\n"
        )
        monkeypatch.setenv("OPENAI_BASE_URL", "https://different.example/v1")
        monkeypatch.setenv("OPENAI_API_KEY", secrets.token_hex(32))
        config = {"dsh_home": str(tmp_path)}
        assert resolve_route(config).source == "dsh-home-env"
        private.write_text(
            private.read_text()
            + "DSH_FLEET_CREDENTIAL_ORIGIN=https://another.example\n"
        )
        with pytest.raises(ValueError, match="origin"):
            resolve_route(config)
        private.write_text(private.read_text().split("DSH_FLEET_CREDENTIAL_ORIGIN=")[0])
    try:
        output = []

        async def no_tools(*_):
            return False

        async with asyncio.timeout(35):
            await DshAdapter(config).run(
                TaskRequest(
                    id="oai", agent="dsh", workspace=str(tmp_path), prompt="fixture"
                ),
                lambda t, d: output.append((t, d)),
                no_tools,
            )
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
    assert seen and seen[0][0:2] == ("/v1/chat/completions", True)
    assert seen[0][2]["model"] == "local-test"
    assert ("result", {"text": "local routed answer"}) in output
    if credential_source == "official-profile":
        assert vault.read_bytes() == initial_vault
