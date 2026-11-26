"""End-to-end coverage over loopback sockets, including real CLI subprocesses."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil
import pytest
import yaml

from deploy.demo import Cluster

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "integrations/hermes/fleetctl.py"


def eventually(read, *, timeout=18):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = read()
        if result:
            return result
        time.sleep(0.15)
    pytest.fail("Timed out waiting for the expected fleet state")


@pytest.fixture(scope="module")
def udp_receiver():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.settimeout(4)
        yield listener


@pytest.fixture(scope="module")
def fleet(tmp_path_factory, udp_receiver):
    cluster = Cluster(
        tmp_path_factory.mktemp("fleet"), port=0, wol_port=udp_receiver.getsockname()[1]
    )
    runtime = cluster.start()
    try:
        with httpx.Client(
            base_url=runtime["hub_url"],
            headers={"Authorization": "Bearer " + runtime["api_key"]},
            timeout=8,
            trust_env=False,
        ) as hub:
            yield cluster, hub
    finally:
        members = [
            child
            for owner in cluster.children
            if owner.poll() is None
            for child in [
                psutil.Process(owner.pid),
                *psutil.Process(owner.pid).children(recursive=True),
            ]
        ]
        ports = {
            cfg["port"]
            for path in cluster.state.glob("*.yaml")
            if "port" in (cfg := yaml.safe_load(path.read_text(encoding="utf8")))
        }
        cluster.close()
        assert all(
            not member.is_running() or member.status() == psutil.STATUS_ZOMBIE
            for member in members
        )
        for port in ports:
            with socket.socket() as probe:
                probe.settimeout(1)
                assert probe.connect_ex(("127.0.0.1", port)) != 0


def cli(cluster, *args):
    env = dict(
        os.environ,
        FLEET_HUB_URL=cluster.runtime["hub_url"],
        FLEET_API_KEY=cluster.runtime["api_key"],
        PYTHONIOENCODING="utf-8",
    )
    process = subprocess.run(
        [sys.executable, str(CLI), *args],
        env=env,
        cwd=ROOT,
        capture_output=True,
        timeout=30,
        encoding="utf-8",
        check=False,
    )
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


def get(hub, path):
    res = hub.get(path)
    res.raise_for_status()
    return res.json()


def test_copy_registered_artifact_to_nas_preserves_existing_files(fleet):
    cluster, hub = fleet
    dispatch = hub.post(
        "/api/dispatch",
        json={
            "mode": "single",
            "node_ids": ["demo-a"],
            "agent": "mock",
            "prompt": "[artifact] create a local sample report",
        },
    )
    dispatch.raise_for_status()
    (task,) = dispatch.json()["tasks"]
    events = f"/api/tasks/{task}/events"
    report = eventually(
        lambda: next(
            (
                event["data"]
                for event in get(hub, events)
                if event["type"] == "artifact"
                and event["data"]["path"].endswith(".png")
            ),
            None,
        )
    )
    source_volume = report["volume"]
    source = f"/api/nodes/demo-a/volumes/{source_volume}"
    nas = "/api/nodes/demo-b/volumes/nas"
    expected = hub.get(source + "/download", params={"path": report["path"]})
    expected.raise_for_status()
    assert expected.content.startswith(b"\x89PNG\r\n\x1a\n")
    copy_url = f"/api/tasks/{task}/artifacts/copy"
    request = {
        "source_volume": source_volume,
        "source_path": report["path"],
        "target_node": "demo-b",
        "target_volume": "nas",
        "target_path": "copied-report.png",
    }
    copied = hub.post(copy_url, json=request)
    copied.raise_for_status()
    assert copied.json()["size"] == len(expected.content)
    assert (
        hub.get(nas + "/download", params={"path": request["target_path"]}).content
        == expected.content
    )

    # A destination edited after the first transfer must survive a retry.
    edited = b"destination-owned revision"
    hub.put(
        nas + "/upload", params={"path": request["target_path"]}, content=edited
    ).raise_for_status()
    assert hub.post(copy_url, json=request).status_code == 409
    assert (
        hub.get(nas + "/download", params={"path": request["target_path"]}).content
        == edited
    )

    # A real, readable file in the same volume is not this task's artifact.
    other_file = "unregistered-report.txt"
    hub.put(
        source + "/upload", params={"path": other_file}, content=b"separate upload"
    ).raise_for_status()
    refused = {**request, "target_path": "refused-copy.txt"}
    assert (
        hub.post(copy_url, json={**refused, "source_path": other_file}).status_code
        == 404
    )
    assert (
        hub.post(
            copy_url, json={**refused, "source_volume": "unknown-volume"}
        ).status_code
        == 404
    )
    assert (
        hub.get(nas + "/download", params={"path": refused["target_path"]}).status_code
        == 404
    )

    invalid_target = {**request, "target_path": "../outside-nas.png"}
    assert hub.post(copy_url, json=invalid_target).status_code == 403
    assert not (
        Path(cluster.runtime["workspaces"]["demo-b"]) / "outside-nas.png"
    ).exists()


def test_doctor_checks_real_hub_and_volume_reads(fleet):
    cluster, _ = fleet
    for name in ("hub", "demo-b"):
        process = subprocess.run(
            [
                sys.executable,
                str(ROOT / "deploy/doctor.py"),
                "--config",
                str(cluster.state / (name + ".yaml")),
            ],
            capture_output=True,
            encoding="utf-8",
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
            timeout=25,
        )
        checks = json.loads(process.stdout)
        assert process.returncode == 0, [c for c in checks if not c["ok"]]
        assert any("nas" in check["check"] for check in checks)


def test_cli_single_approval_result_nas(fleet, tmp_path):
    cluster, hub = fleet
    nodes = cli(cluster, "nodes")
    assert {n["id"] for n in nodes if n["online"]} == {"demo-a", "demo-b", "demo-c"}
    result = cli(
        cluster,
        "dispatch",
        "--mode",
        "single",
        "--node",
        "demo-a",
        "--agent",
        "mock",
        "[approval] review sample",
    )
    (task,) = result["tasks"]
    assert cli(cluster, "status", task)["node_id"] == "demo-a"
    approval = eventually(
        lambda: next(
            (a for a in cli(cluster, "approvals") if a["task_id"] == task), None
        )
    )
    cli(cluster, "approve", approval["id"])
    assert not any(a["id"] == approval["id"] for a in cli(cluster, "approvals"))
    result = cli(cluster, "result", task, "--wait", "16")
    assert result["status"] == "completed" and "review sample" in result["result"]
    events = get(hub, f"/api/tasks/{task}/events")
    assert {"approval_required", "approval_resolved", "result"} <= {
        e["type"] for e in events
    }
    assert eventually(
        lambda: (
            (cluster.state / "events.jsonl").exists()
            and "approval_resolved"
            in (cluster.state / "events.jsonl").read_text(encoding="utf8")
        )
    )
    src = tmp_path / "measurements.csv"
    src.write_text("x,y\n1,2\n", encoding="utf8")
    (nas_task,) = cli(
        cluster,
        "files",
        "demo-b",
        "nas",
        "measurements.csv",
        "--upload",
        str(src),
        "--agent",
        "mock",
        "--dispatch",
        "summarize sample",
    )["tasks"]
    entries = cli(cluster, "files", "demo-b", "nas")["entries"]
    assert "measurements.csv" in {e["name"] for e in entries}
    assert (
        cli(cluster, "files", "demo-b", "nas", "measurements.csv", "--preview")
        == src.read_bytes().decode()
    )
    copied = tmp_path / "download.csv"
    cli(
        cluster, "files", "demo-b", "nas", "measurements.csv", "--download", str(copied)
    )
    assert copied.read_bytes() == src.read_bytes()
    summary = cli(cluster, "result", nas_task, "--wait", "16")
    assert (
        summary["status"] == "completed"
        and summary["context"]["path"] == "measurements.csv"
    )


def test_auto_fanout_and_wake(fleet, udp_receiver):
    cluster, hub = fleet
    (task,) = cli(
        cluster, "dispatch", "--agent", "mock", "--tag", "nas", "inspect storage"
    )["tasks"]
    assert cli(cluster, "result", task, "--wait", "16")["node_id"] == "demo-b"
    (held,) = cli(
        cluster,
        "dispatch",
        "--mode",
        "single",
        "--node",
        "demo-a",
        "--agent",
        "mock",
        "[wait]",
    )["tasks"]
    try:
        eventually(
            lambda: any(
                n["id"] == "demo-a" and n["health"].get("active_tasks", 0) > 0
                for n in get(hub, "/api/nodes")
            )
        )
        (selected,) = cli(
            cluster,
            "dispatch",
            "--mode",
            "auto",
            "--node",
            "demo-a",
            "--node",
            "demo-b",
            "--agent",
            "mock",
            "choose idle node",
        )["tasks"]
        assert cli(cluster, "result", selected, "--wait", "16")["node_id"] == "demo-b"
    finally:
        cli(cluster, "cancel", held)
    jobs = cli(
        cluster,
        "dispatch",
        "--mode",
        "fanout",
        "--tag",
        "dev",
        "--agent",
        "mock",
        "compile summary",
    )
    assert len(jobs["tasks"]) == 3
    results = [cli(cluster, "result", task, "--wait", "16") for task in jobs["tasks"]]
    assert {row["node_id"] for row in results} == {"demo-a", "demo-b", "demo-c"}
    assert all(
        row["group_id"] == jobs["group_id"] and row["status"] == "completed"
        for row in results
    )
    assert cli(cluster, "wake", "demo-b")["sent"]
    packet, _ = udp_receiver.recvfrom(256)
    assert packet == b"\xff" * 6 + bytes.fromhex("020000000002") * 16


def test_cancel_terminates_mock_process_tree(fleet):
    cluster, hub = fleet
    (task,) = cli(
        cluster,
        "dispatch",
        "--mode",
        "single",
        "--node",
        "demo-c",
        "--agent",
        "mock",
        "[wait]",
    )["tasks"]

    def wait_pids():
        events = get(hub, f"/api/tasks/{task}/events")
        return next(
            (
                e["data"]
                for e in events
                if e["type"] == "tool_call" and "child_pid" in e["data"]
            ),
            None,
        )

    proc_info = eventually(wait_pids)
    pids = [proc_info["pid"], proc_info["child_pid"]]
    assert all(psutil.pid_exists(pid) for pid in pids)
    cli(cluster, "cancel", task)
    assert cli(cluster, "status", task)["status"] == "cancelled"
    eventually(
        lambda: all(
            not psutil.pid_exists(pid)
            or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
            for pid in pids
        )
    )


def test_cancel_closes_pending_notification_once(fleet):
    cluster, hub = fleet
    (task,) = cli(
        cluster,
        "dispatch",
        "--mode",
        "single",
        "--node",
        "demo-a",
        "--agent",
        "mock",
        "[approval] cancel before execution",
    )["tasks"]
    pending = eventually(
        lambda: next(
            (v for v in get(hub, "/api/approvals") if v["task_id"] == task), None
        )
    )
    cli(cluster, "cancel", task)
    assert all(item["id"] != pending["id"] for item in get(hub, "/api/approvals"))

    def notification():
        path = cluster.state / "events.jsonl"
        if not path.exists():
            return None
        records = [
            json.loads(line) for line in path.read_text(encoding="utf8").splitlines()
        ]
        return [
            r
            for r in records
            if r["task_id"] == task and r["type"] == "approval_resolved"
        ]

    (closed,) = eventually(notification)
    assert (
        closed["data"]["id"] == pending["id"] and closed["data"]["cause"] == "cancelled"
    )


def test_enroll_edit_revoke_live(fleet, tmp_path):
    cluster, hub = fleet
    res = hub.post("/api/join-tokens")
    res.raise_for_status()
    token = res.json()["token"]
    config = tmp_path / "paired.yaml"
    private = tmp_path / "inbox"
    private.mkdir()
    (private / "existing.txt").write_text("personal", encoding="utf8")
    with socket.socket() as p:
        p.bind(("127.0.0.1", 0))
        port = p.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "deploy/pair.py"),
            "--hub",
            cluster.runtime["hub_url"],
            "--url",
            url,
            "--alias",
            "temporary",
            "--config",
            str(config),
            "--workspace",
            str(tmp_path),
        ],
        env=dict(os.environ, FLEET_JOIN_TOKEN=token),
        cwd=ROOT,
        check=True,
        timeout=15,
        capture_output=True,
    )
    pair = yaml.safe_load(config.read_text(encoding="utf8"))
    assert pair["trusted_workspaces"] == [str(tmp_path)]
    (volume,) = pair["volumes"]
    assert volume["name"] == "inbox" and not volume["read_only"]
    assert Path(volume["path"]).parent == tmp_path and Path(volume["path"]) != private
    ident = pair["node_id"]
    auth = {"Authorization": "Bearer " + pair["secret"]}
    assert (
        hub.post(
            "/api/enroll", json={"token": token, "alias": "duplicate", "url": url}
        ).status_code
        == 403
    )
    assert hub.post("/api/node/verify", headers=auth, json={"id": ident}).json()[
        "valid"
    ]
    cluster.spawn("paired", "fleet_node.main", pair)

    def healthy():
        return any(n["id"] == ident and n["online"] for n in get(hub, "/api/nodes"))

    eventually(healthy)
    assert hub.get(url + "/ping", headers=auth).json() == {"pong": True}
    assert get(hub, f"/api/nodes/{ident}/volumes/inbox/list")["entries"] == []
    target = f"/api/nodes/{ident}/volumes/inbox"
    assert hub.put(
        target + "/upload?path=mobile.txt", content=b"phone attachment"
    ).is_success
    assert hub.get(target + "/preview?path=mobile.txt").text == "phone attachment"
    changed = url.replace("127.0.0.1", "localhost")
    assert (
        hub.patch("/api/nodes/" + ident, json={"url": changed}).json()["url"] == changed
    )
    eventually(healthy)
    assert hub.delete("/api/nodes/" + ident).json()["removed"]
    assert (
        hub.post("/api/node/verify", headers=auth, json={"id": ident}).status_code
        == 403
    )
    assert not any(n["id"] == ident for n in get(hub, "/api/nodes"))
    direct = hub.get(url + "/health", headers=auth)
    assert direct.status_code in {401, 403}
    assert hub.get(url + "/ping", headers=auth).status_code == 403
