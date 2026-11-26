import asyncio
import os
import secrets
import time
from pathlib import Path

import httpx
import psutil
import pytest
import yaml
from fleet_node.agents import DshAdapter
from fleet_node.app import create_app
from fleet_node.policy import TaskPolicy

from packages.protocol.adapters import Decision, TaskRequest
from packages.protocol.network import allowed_peer, resolve_bind

NODE_CREDENTIAL = secrets.token_urlsafe(32)


def test_bind_and_peer():
    for address in ["0.0.0.0", "::", "::ffff:0.0.0.0"]:
        with pytest.raises(ValueError):
            resolve_bind({"bind_ip": address})
    assert resolve_bind({"bind_ip": "::"}, True) == "::"
    assert resolve_bind({"bind_mode": "cidr_match", "cidr": "127.0.0.0/8"}).startswith(
        "127."
    )
    assert allowed_peer("::ffff:127.0.0.1", ["127.0.0.0/8"])
    assert not allowed_peer("192.0.2.5", ["127.0.0.0/8"])


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    nas = tmp_path / "nas"
    nas.mkdir()
    config = {
        "secret": NODE_CREDENTIAL,
        "trusted_workspaces": [str(root)],
        "allow_arbitrary_dirs": True,
        "approval_timeout": 2,
        "volumes": [{"name": "nas", "root": str(nas), "read_only": False}],
    }
    path = tmp_path / "node.yml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    config["_config_file"] = str(path)
    app = create_app(config)
    yield app, root, nas
    app.state.node.history.close()


def client(app, peer="127.0.0.1", secret=NODE_CREDENTIAL):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(peer, 9000)),
        base_url="http://localhost",
        headers={"Authorization": f"Bearer {secret}"},
    )


async def test_advertised_default_follows_configured_workspace_order(tmp_path):
    current, previous = tmp_path / "new-work", tmp_path / "archive"
    current.mkdir()
    previous.mkdir()
    app = create_app({
        "secret": NODE_CREDENTIAL,
        "trusted_workspaces": [str(current), str(previous), str(current)],
        "volumes": [{"name": "archive", "path": str(previous), "read_only": True}],
    })
    node = app.state.node
    node.probe = {"disks": []}
    node.probe_time = time.monotonic()
    try:
        async with client(app) as api:
            response = await api.get("/health")
            assert response.status_code == 200
            assert response.json()["workspaces"] == [str(current), str(previous)]
        request = TaskRequest(id="workspace-order", prompt="Inspect workspace")
        assert node.prepare(request).workspace == str(current)
        explicit = request.model_copy(update={"workspace": str(previous)})
        assert node.prepare(explicit).workspace == str(previous)
        with_context = TaskRequest(
            id="with-context", prompt="Inspect volume", context={"volume": "archive", "path": ""}
        )
        assert node.prepare(with_context).workspace == str(previous)
    finally:
        node.history.close()


async def test_node_auth(setup):
    app, _, _ = setup
    async with client(app) as api:
        assert (await api.get("/ping")).json() == {"pong": True}
    async with client(app, secret="wrong") as api:
        assert (await api.get("/ping")).status_code == 401
        assert (await api.get("/api/volumes")).status_code == 401
    async with client(app, peer="192.0.2.1") as api:
        assert (await api.get("/ping")).status_code == 403
        assert (
            await api.get("/api/volumes", headers={"X-Forwarded-For": "127.0.0.1"})
        ).status_code == 403


async def test_volumes_and_persistent_edits(setup):
    app, root, nas = setup
    async with client(app) as api:
        app.state.node.config["max_upload_bytes"] = 4
        (nas / "keep.txt").write_bytes(b"kept")
        reply = await api.put(
            "/api/volumes/nas/upload", params={"path": "keep.txt"}, content=b"oversized"
        )
        assert reply.status_code == 413 and (nas / "keep.txt").read_bytes() == b"kept"
        assert not list(nas.glob(".upload-*"))
        for escape in (
            "../x",
            "C:/other",
            "/bad",
            "folder:stream",
            "a\\..\\b",
            "aux.txt",
            "x.",
        ):
            assert (
                await api.put(
                    "/api/volumes/nas/upload", params={"path": escape}, content=b"x"
                )
            ).status_code == 403
        (nas / "sub").mkdir()
        response = await api.put(
            "/api/volumes/ro", json={"path": str(nas / "sub"), "read_only": True}
        )
        assert response.is_success, response.text
        assert (
            await api.put(
                "/api/volumes/ro/upload", params={"path": "test"}, content=b"x"
            )
        ).status_code == 403
        assert (
            await api.put(
                "/api/volumes/escape", json={"path": str(root), "read_only": False}
            )
        ).status_code == 403
        assert (
            await api.put(
                "/api/volumes/nas/upload", params={"path": "a.csv"}, content=b"a,b"
            )
        ).is_success
        assert (
            await api.get("/api/volumes/nas/preview", params={"path": "a.csv"})
        ).text == "a,b"
        assert (await api.get("/api/volumes/nas/search", params={"q": "csv"})).json()[
            "entries"
        ][0]["path"] == "a.csv"
        (nas / "sub" / "keep.txt").write_text("keep")
        assert (await api.delete("/api/volumes/ro")).is_success
        assert (nas / "sub" / "keep.txt").exists()
        assert (
            yaml.safe_load(
                Path(app.state.node.config["_config_file"]).read_text("utf-8")
            )["volumes"][0]["name"]
            == "nas"
        )


@pytest.mark.parametrize(
    "decision,accepted",
    [(Decision.once, True), (Decision.directory, True), (Decision.deny, False)],
)
async def test_policy_once_session_and_deny(setup, decision, accepted):
    app, root, outside = setup
    events = []
    node = app.state.node
    policy = TaskPolicy(
        node, root, lambda t, d: events.append((t, d)), DshAdapter(node.config)
    )
    assert await policy.authorize("read notes.txt", ["notes.txt"])
    pending = asyncio.create_task(policy.authorize("read", [str(outside)]))
    await asyncio.sleep(0)
    aid = events[-1][1]["id"]
    assert time.time() < events[-1][1]["expires_at"] <= time.time() + 2
    assert policy.decide(aid, decision)
    assert not policy.decide(aid, decision)
    assert await pending == accepted
    assert bool(policy.session_dirs) == (decision == Decision.directory)
    if decision == Decision.directory:
        assert await policy.authorize("read ok", [str(outside / "file")])
    node.timeout = 0.015
    assert not await policy.authorize("rm -rf output", [str(root)])
    assert events[-1][1]["cause"] == "timeout"


async def test_nested_process_cancellation(setup):
    app, root, _ = setup
    async with client(app) as api:
        body = {
            "id": "cancel",
            "prompt": "[wait]",
            "workspace": str(root),
            "agent": "mock",
        }
        assert (await api.post("/api/tasks", json=body)).is_success
        assert (await api.post("/api/tasks", json=body)).is_success
        assert (
            await api.post("/api/tasks", json={**body, "prompt": "other"})
        ).status_code == 409
        async with asyncio.timeout(12):
            while True:
                events = (await api.get("/api/tasks/cancel/events")).json()
                if event := next((e for e in events if e["type"] == "tool_call"), None):
                    break
                await asyncio.sleep(0.05)
        assert (await api.post("/api/tasks/cancel/cancel")).json()[
            "status"
        ] == "cancelled"
        for key in ["pid", "child_pid"]:
            assert not psutil.pid_exists(event["data"][key])


async def test_dsh_tool_boundary(setup):
    app, root, nas = setup
    adapter = DshAdapter(app.state.node.config)
    task = TaskRequest(id="s", workspace=str(root), prompt="text")
    audit = []

    async def permit(command, paths, risk):
        audit.append(paths)
        return True

    async def check(name, path):
        return await adapter.tool_allowed(
            task, {"name": name, "args": {"file_path": path}}, permit
        )

    assert await check("read", "document.txt")
    assert audit == [[str(root / "document.txt")]]
    for path in [
        "aux.txt",
        "name:payload",
        str(Path(app.state.node.config["_config_file"])),
        ".ssh/id_rsa",
    ]:
        assert not await check("write", path)
    assert not await check("pwsh", "file")
    adapter.read_only.append(nas)
    assert not await check("edit", str(nas / "file"))
    assert await check("read", str(nas / "file"))


async def test_restart_never_replays_an_accepted_request(tmp_path):
    from fleet_node.history import History, fingerprint

    from packages.protocol.adapters import TaskRequest

    state = str(tmp_path / "node.sqlite3")
    body = TaskRequest(
        id="once", agent="mock", workspace=str(tmp_path), prompt="[artifact]"
    ).model_dump()
    old = History(state)
    old.claim("once", fingerprint(body))
    old.append("once", "status", {"status": "running"})
    old.append("once", "approval_required", {"id": "old", "command": "example"})
    with pytest.raises(RuntimeError):
        History(state)
    old.close()
    app = create_app(
        {
            "secret": NODE_CREDENTIAL,
            "trusted_workspaces": [str(tmp_path)],
            "state_path": state,
        }
    )
    try:
        async with client(app) as api:
            result = await api.post("/api/tasks", json=body)
            assert result.json()["status"] == "failed"
            assert (
                await api.post("/api/tasks", json={**body, "prompt": "changed"})
            ).status_code == 409
            events = (await api.get("/api/tasks/once/events")).json()
            assert any(e["data"].get("cause") == "restart" for e in events)
            assert not app.state.node.jobs
    finally:
        app.state.node.history.close()


async def test_link_boundaries_and_overlapping_read_only_volumes(tmp_path):
    workspace, share = tmp_path / "workspace", tmp_path / "share"
    workspace.mkdir()
    share.mkdir()
    read_only = share / "read-only"
    read_only.mkdir()
    private_file = workspace / "private.txt"
    private_file.write_text("outside")
    os.link(private_file, share / "hard.txt")
    junction = share / "link"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(workspace), str(junction))
    else:
        junction.symlink_to(workspace, target_is_directory=True)
    app = create_app(
        {
            "secret": NODE_CREDENTIAL,
            "trusted_workspaces": [str(workspace)],
            "agent": "dsh",
            "enabled_agents": ["dsh"],
            "volumes": [
                {"name": "all", "path": str(share), "read_only": False},
                {"name": "ro", "path": str(read_only), "read_only": True},
            ],
        }
    )
    try:
        async with client(app) as api:
            for filename in ("hard.txt", "link/private.txt"):
                for action in ("preview", "download"):
                    assert (
                        await api.get(
                            "/api/volumes/all/" + action, params={"path": filename}
                        )
                    ).status_code == 403
                assert (
                    await api.put(
                        "/api/volumes/all/upload",
                        params={"path": filename},
                        content=b"changed",
                    )
                ).status_code == 403
            assert private_file.read_text() == "outside"
            assert (
                await api.put(
                    "/api/volumes/all/upload",
                    params={"path": "read-only/blocked.txt"},
                    content=b"changed",
                )
            ).status_code == 403
            assert (await api.delete("/api/volumes/ro")).is_success
            assert (
                await api.put(
                    "/api/volumes/new",
                    json={"path": str(read_only), "read_only": False},
                )
            ).status_code == 403
            health = (await api.get("/health")).json()
            assert (health["agent"], health["enabled_agents"]) == ("dsh", ["dsh"])
    finally:
        app.state.node.history.close()
