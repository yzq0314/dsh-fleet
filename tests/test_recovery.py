"""Restart the Hub mid-flight while Sidecars retain their live task sessions."""

import json
import socket
import time

import httpx
import psutil
import yaml

from deploy.demo import Cluster


def listener(port):
    return next(
        (
            conn.pid
            for conn in psutil.net_connections(kind="tcp")
            if conn.status == psutil.CONN_LISTEN and conn.laddr.port == port
        ),
        None,
    )


def until(read, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError("Expected live state did not arrive")


def test_restart_resumes_approval_execution_and_sse(tmp_path):
    cluster = Cluster(tmp_path, port=0)
    runtime = cluster.start()
    try:
        with httpx.Client(
            base_url=runtime["hub_url"],
            headers={"Authorization": "Bearer " + runtime["api_key"]},
            trust_env=False,
            timeout=8,
        ) as hub:

            def get(path):
                response = hub.get(path)
                response.raise_for_status()
                return response.json()

            def post(path, **body):
                response = hub.post(path, json=body)
                response.raise_for_status()
                return response.json()

            def dispatch(node, prompt):
                return post(
                    "/api/dispatch",
                    mode="single",
                    node_ids=[node],
                    agent="mock",
                    prompt=prompt,
                )["tasks"][0]

            running = dispatch("demo-c", "[wait] recovery")
            waiting = dispatch("demo-b", "[approval] recovery")
            pending = until(
                lambda: next(
                    (a for a in get("/api/approvals") if a["task_id"] == waiting), None
                )
            )

            def task_events(task):
                return get(f"/api/tasks/{task}/events")

            def spawned():
                return next(
                    (
                        e
                        for e in task_events(running)
                        if e["type"] == "tool_call" and "pid" in e["data"]
                    ),
                    None,
                )

            live = until(spawned)["data"]
            pids = [live["pid"], live["child_pid"]]
            assert all(psutil.pid_exists(pid) for pid in pids)
            with hub.stream("GET", "/api/events") as stream:
                stream.raise_for_status()
                for line in stream.iter_lines():
                    if line.startswith("data: "):
                        event = json.loads(line[6:])
                        if (
                            event["type"] == "approval_required"
                            and event["task_id"] == waiting
                        ):
                            cursor = event["id"]
                            break
                else:
                    raise AssertionError("Initial SSE approval was absent")

            # kill() abruptly stops only the Hub, leaving the Sidecar process tree intact.
            old_hub = cluster.children[-1]
            config = yaml.safe_load(
                (cluster.state / "hub.yaml").read_text(encoding="utf8")
            )
            port = config["port"]
            old_pid = listener(port)
            assert old_pid
            cluster.stop(old_hub, abrupt=True)
            assert not psutil.pid_exists(old_pid)
            with socket.socket() as probe:
                assert probe.connect_ex(("127.0.0.1", port)) != 0
            assert all(psutil.pid_exists(pid) for pid in pids)
            cluster.spawn("recovered-hub", "fleet_hub.main", config)

            def available():
                try:
                    return get("/api/nodes")
                except (httpx.RequestError, httpx.HTTPStatusError):
                    return None

            until(available)
            assert listener(port) not in {None, old_pid}
            assert [
                a["id"] for a in get("/api/approvals") if a["task_id"] == waiting
            ] == [pending["id"]]
            assert spawned()["data"] == live
            post("/api/approvals/" + pending["id"], decision="approve_once")
            until(lambda: get("/api/tasks/" + waiting)["status"] == "completed")
            post("/api/tasks/" + running + "/cancel")
            until(lambda: get("/api/tasks/" + running)["status"] == "cancelled")
            assert all(
                not psutil.pid_exists(pid)
                or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
                for pid in pids
            )
            persisted = sorted(
                [*task_events(waiting), *task_events(running)], key=lambda e: e["id"]
            )
            for task_id in (waiting, running):
                task_log = [e for e in persisted if e["task_id"] == task_id]
                receipts = [e for e in task_log if e["type"] == "dispatch_ack"]
                assert len(receipts) == 1 and receipts[0]["data"] == {}
                assert task_log[0]["data"]["status"] == "queued"
                assert task_log[0]["id"] < receipts[0]["id"]
            expected = [e["id"] for e in persisted if e["id"] > cursor]
            replay = []
            with hub.stream(
                "GET", "/api/events", headers={"Last-Event-ID": str(cursor)}
            ) as stream:
                stream.raise_for_status()
                for line in stream.iter_lines():
                    if line.startswith("data: "):
                        replay.append(json.loads(line[6:])["id"])
                        if replay[-1] >= expected[-1]:
                            break
            assert replay == expected
            assert (
                sum(
                    e["type"] == "tool_call" and e["task_id"] == running
                    for e in persisted
                )
                == 1
            )
            assert (
                sum(
                    e["type"] == "approval_required" and e["task_id"] == waiting
                    for e in persisted
                )
                == 1
            )
            assert (
                sum(
                    e["type"] == "result" and e["task_id"] == waiting for e in persisted
                )
                == 1
            )
    finally:
        cluster.close()
