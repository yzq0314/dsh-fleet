import asyncio
import contextlib
import hmac
import ipaddress
import os
import platform
import re
import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
import psutil
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictBool

from packages.protocol.adapters import REGISTRY, Decision, TaskRequest
from packages.protocol.network import allowed_peer

from .agents import dsh_command
from .history import History, fingerprint
from .policy import DEFAULT_RULES, TaskPolicy, compile_rules
from .volumes import Volume
from .runner import available as available_runners


class DecisionBody(BaseModel):
    decision: Decision


class RuleBody(BaseModel):
    rules: list[str] | None = None
    mode: Literal["strict_hash", "balanced", "autonomous"] | None = None
    confirm_autonomous: StrictBool = False


class WakeRequest(BaseModel):
    mac: str
    broadcast: str


class VolumeBody(BaseModel):
    path: str
    read_only: bool = True


def send_wake(mac, broadcast):
    if not re.fullmatch(
        r"(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}|[0-9a-fA-F]{12}", mac
    ):
        raise ValueError("Invalid MAC")
    address = ipaddress.IPv4Address(broadcast)
    if address.is_multicast or address.is_unspecified:
        raise ValueError("Invalid broadcast address")
    packet = b"\xff" * 6 + bytes.fromhex(mac.replace("-", "").replace(":", "")) * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (str(address), 9))
    return {"sent": True}


class Job:
    def __init__(self, node, task, source):
        self.task, self.source = task, source
        self.history, self.status = node.history, "queued"
        self.adapter = REGISTRY[task.agent](node.config)
        self.policy = TaskPolicy(node, Path(task.workspace), self.emit, self.adapter)
        self.handle = None

    def emit(self, kind, data):
        self.history.append(self.task.id, kind, data)
        if kind == "status":
            self.status = data["status"]

    async def run(self):
        self.emit("status", {"status": "running"})
        try:
            if not await self.policy.authorize(
                "打开工作目录 " + self.task.workspace, [self.task.workspace]
            ):
                raise PermissionError("Workspace approval denied")
            await self.adapter.run(self.task, self.emit, self.policy.authorize)
        except asyncio.CancelledError:
            await self.adapter.cancel()
            self.emit("status", {"status": "cancelled"})
        except Exception as error:  # noqa: BLE001 - plugin failures must terminate their durable task
            self.emit("log", {"text": str(error)[:500]})
            self.emit("status", {"status": "failed"})
        else:
            self.emit("status", {"status": "completed"})

    async def stop(self):
        if self.handle and not self.handle.done():
            self.handle.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.handle
        if self.status == "queued":
            self.emit("status", {"status": "cancelled"})


def hardware():
    disks = []
    for mount in psutil.disk_partitions():
        try:
            usage = psutil.disk_usage(mount.mountpoint)
            disks.append(
                {"path": mount.mountpoint, "percent": usage.percent, "free": usage.free}
            )
        except (PermissionError, OSError):
            continue
    gpu = None
    if executable := shutil.which("nvidia-smi"):
        try:
            result = subprocess.run(
                [
                    executable,
                    "--query-gpu=utilization.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if result.returncode == 0:
                gpu = max(float(x) for x in result.stdout.splitlines())
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return {
        "cpu": psutil.cpu_percent(interval=0.08),
        "ram": psutil.virtual_memory().percent,
        "gpu": gpu,
        "disks": disks,
    }


class Node:
    def __init__(self, config):
        self.config = config
        if config.get("_config_file"):
            config["_artifact_spool"] = str(
                Path(config["_config_file"]).with_suffix(".artifacts")
            )
        if any(v.get("name") == "fleet-artifacts" for v in config.get("volumes", [])):
            raise ValueError(
                "The fleet-artifacts volume name is reserved for task outputs"
            )
        self.secret = config["secret"]
        if not isinstance(self.secret, str) or not self.secret:
            raise ValueError("A node secret is required")
        self.cidrs = config.get("allowed_cidrs", ["127.0.0.0/8", "::1/128"])
        for cidr in self.cidrs:
            ipaddress.ip_network(cidr, strict=False)
        # The Hub selects the first advertised workspace for a task without context.
        self.workspaces = list(dict.fromkeys(
            Path(p).resolve() for p in config.get("trusted_workspaces", [])
        ))
        self.roots = set(self.workspaces)
        self.arbitrary = config.get("allow_arbitrary_dirs", False)
        self.timeout = float(config.get("approval_timeout", 120))
        self.export_roots = set()
        for entry in config.get("volume_roots", []) + list(config.get("volumes", [])):
            if isinstance(entry, str):
                self.export_roots.add((Path(entry).resolve(), False))
            else:
                self.export_roots.add(
                    (
                        Path(entry.get("path") or entry["root"]).resolve(),
                        bool(entry.get("read_only", True)),
                    )
                )
        self.write_barriers = {
            path for path, read_only in self.export_roots if read_only
        }
        self.volumes = {
            v["name"]: Volume(v, self.write_barriers) for v in config.get("volumes", [])
        }
        config["_write_barriers"] = self.write_barriers
        self.rules = compile_rules(config.get("rules", DEFAULT_RULES))
        self.mode = config.get("policy_mode", "balanced")
        if self.mode not in {"strict_hash", "balanced", "autonomous"}:
            raise ValueError("Unknown policy_mode")
        self.policy_lock = asyncio.Lock()
        config_file = config.get("_config_file")
        self.history = History(
            config.get("state_path")
            or (
                str(Path(config_file).with_suffix(".state.sqlite3"))
                if config_file
                else ":memory:"
            )
        )
        self.jobs = {}
        self.probe, self.probe_time, self.dsh_version = {}, 0, None
        self.probe_task = None

    def volume(self, name):
        if name == "fleet-artifacts" and self.config.get("_artifact_spool"):
            return Volume(
                {
                    "name": name,
                    "path": self.config["_artifact_spool"],
                    "read_only": True,
                }
            )
        if name not in self.volumes:
            raise HTTPException(404, "No such volume")
        return self.volumes[name]

    def job(self, identifier):
        if identifier not in self.jobs:
            raise HTTPException(404, "No such task")
        return self.jobs[identifier]

    def volume_info(self):
        return [
            {
                "name": v.name,
                "path": str(v.root),
                "read_only": v.read_only,
                "available": v.root.is_dir(),
            }
            for v in self.volumes.values()
        ]

    async def refresh_health(self):
        if self.dsh_version is None:
            try:
                env = os.environ.copy()
                result = await asyncio.to_thread(
                    subprocess.run,
                    [*dsh_command(env, self.config.get("dsh_cli")), "--version"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=4,
                )
                self.dsh_version = (
                    result.stdout.strip()[:80]
                    if result.returncode == 0
                    else "unavailable"
                )
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                self.dsh_version = "unavailable"
        from .pocket import probe_pocket
        usage, volumes, pocket = await asyncio.gather(
            asyncio.to_thread(hardware), asyncio.to_thread(self.volume_info), probe_pocket(self.config)
        )
        self.probe = {**usage, **pocket, "volumes": volumes}
        self.probe_time = time.monotonic()

    def save_config(self, values):
        if filename := self.config.get("_config_file"):
            path = Path(filename)
            source = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
            source.update(values)
            fd, temp = tempfile.mkstemp(dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    yaml.safe_dump(source, handle, allow_unicode=True)
                os.replace(temp, path)
            finally:
                Path(temp).unlink(missing_ok=True)
        self.config.update(values)

    def change_volume(self, name, body):
        if name == "fleet-artifacts":
            raise HTTPException(403, "任务产物卷由 Sidecar 管理")
        if not re.fullmatch(r"[\w-]{1,64}", name):
            raise HTTPException(422, "Invalid volume name")
        updated = self.volumes.copy()
        if body is None:
            self.volume(name)
            del updated[name]
        else:
            from .paths import guarded_target, protected_roots, sensitive_path

            if not Path(body.path).is_absolute():
                raise HTTPException(422, "Select an absolute volume path")
            target = guarded_target(Path(body.path).parent, body.path)
            if target is None or not any(
                target.is_relative_to(p) for p, _ in self.export_roots
            ):
                raise HTTPException(403, "Outside locally configured export roots")
            if sensitive_path(target, protected_roots(self.config)):
                raise HTTPException(403, "Protected control-plane directory")
            if not body.read_only and any(
                ro and (target.is_relative_to(p) or p.is_relative_to(target))
                for p, ro in self.export_roots
            ):
                raise HTTPException(403, "This export is read-only")
            updated[name] = Volume(
                {"name": name, "path": str(target), "read_only": body.read_only},
                self.write_barriers,
            )
        values = [
            {"name": v.name, "path": str(v.root), "read_only": v.read_only}
            for v in updated.values()
        ]
        self.save_config(
            {
                "volumes": values,
                "volume_roots": [
                    {"path": str(p), "read_only": ro}
                    for p, ro in sorted(self.export_roots)
                ],
            }
        )
        self.volumes = updated
        return self.volume_info()

    def prepare(self, incoming):
        task = incoming.model_copy()
        if "agent" not in incoming.model_fields_set:
            task.agent = self.config.get("agent", "mock")
        if task.agent not in REGISTRY:
            raise HTTPException(422, "Unknown agent")
        if (
            self.config.get("enabled_agents") is not None
            and task.agent not in self.config["enabled_agents"]
        ):
            raise HTTPException(403, "Agent disabled on this node")
        context_path = None
        if task.context:
            context_path = self.volume(task.context.volume).target(task.context.path)
            if not context_path.exists():
                raise HTTPException(404, "File context unavailable")
        roots = list(self.config.get("trusted_workspaces", []))
        workspace = task.workspace or (
            str(context_path if context_path.is_dir() else context_path.parent)
            if context_path
            else next(iter(roots), "")
        )
        if (
            not workspace
            or not Path(workspace).is_absolute()
            or not Path(workspace).is_dir()
        ):
            raise HTTPException(422, "Select a valid absolute workspace directory")
        task.workspace = str(Path(workspace).resolve())
        if not self.arbitrary and not any(
            Path(task.workspace).is_relative_to(root) for root in self.roots
        ):
            raise HTTPException(403, "Workspace outside trusted directories")
        if context_path is not None:
            task.prompt += "\n\n上下文路径：" + str(context_path)
        return task

    async def save_policy(self, body):
        async with self.policy_lock:
            await self._save_policy(body)

    async def _save_policy(self, body):
        if body.rules is None and body.mode is None:
            raise HTTPException(422, "Specify rules or mode")
        mode = body.mode or self.mode
        if mode == "autonomous" and mode != self.mode and not body.confirm_autonomous:
            raise HTTPException(
                422, "Explicit confirmation is required for autonomous mode"
            )
        patterns = (
            body.rules
            if body.rules is not None
            else [rule.pattern for rule in self.rules]
        )
        try:
            rules = compile_rules(patterns)
        except (re.error, ValueError) as error:
            raise HTTPException(422, str(error)) from error
        stop = mode != self.mode or patterns != [rule.pattern for rule in self.rules]
        self.save_config({"rules": patterns, "policy_mode": mode})
        self.mode = mode
        self.rules = rules
        if stop:
            await asyncio.gather(*(job.stop() for job in list(self.jobs.values())))


def create_app(config: dict):
    node = Node(config)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await asyncio.gather(*(job.stop() for job in node.jobs.values()))
        node.history.close()

    app = FastAPI(
        title="Fleet Node",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.node = node
    from .pocket_proxy import install as install_pocket
    install_pocket(app, node, config)

    @app.middleware("http")
    async def authenticate(request, next_handler):
        if not request.client or not allowed_peer(request.client.host, node.cidrs):
            return JSONResponse({"detail": "Peer outside allowed CIDRs"}, 403)
        bearer = request.headers.get("authorization", "")
        if not hmac.compare_digest(bearer.encode(), ("Bearer " + node.secret).encode()):
            return JSONResponse({"detail": "Invalid node credential"}, 401)
        if hub := config.get("hub_url"):
            try:
                async with httpx.AsyncClient(
                    timeout=4, trust_env=False, follow_redirects=False
                ) as client:
                    result = await client.post(
                        hub.rstrip("/") + "/api/node/verify",
                        json={"id": config["node_id"]},
                        headers={"Authorization": bearer},
                    )
                if not result.is_success or result.json().get("valid") is not True:
                    return JSONResponse(
                        {"detail": "Node credential revoked or unavailable"}, 403
                    )
            except (httpx.HTTPError, KeyError, ValueError, AttributeError):
                return JSONResponse(
                    {"detail": "Node credential verification unavailable"}, 503
                )
        return await next_handler(request)

    @app.get("/ping")
    async def ping():
        return {"pong": True}

    @app.get("/health")
    async def health():
        if time.monotonic() - node.probe_time > 5 or not node.probe:
            if node.probe_task is None or node.probe_task.done():
                node.probe_task = asyncio.create_task(node.refresh_health())
            try:
                await asyncio.wait_for(asyncio.shield(node.probe_task), 0.7)
            except (TimeoutError, OSError):
                pass
        snapshot = node.probe or {
            "cpu": psutil.cpu_percent(),
            "ram": psutil.virtual_memory().percent,
            "gpu": None,
            "disks": [],
            "volumes": [
                {
                    "name": v.name,
                    "path": str(v.root),
                    "read_only": v.read_only,
                    "available": None,
                }
                for v in list(node.volumes.values())
            ],
        }
        return {
            "hostname": socket.gethostname(),
            "os": platform.system(),
            "agent_version": node.dsh_version or "probing",
            "capabilities": ["upload_create_only", "dsh_model_diagnostics_v1", "desktop_handoff_v1"],
            "policy_mode": node.mode,
            "agent": config.get("agent", "mock"),
            "enabled_agents": config.get("enabled_agents", sorted(REGISTRY)),
            **snapshot,
            "active_tasks": sum(
                job.status in {"queued", "running"} for job in node.jobs.values()
            ),
            "workspaces": [str(path) for path in node.workspaces],
        }

    @app.post("/api/tasks")
    async def start(incoming: TaskRequest):
        source = incoming.model_dump()
        digest = fingerprint(source)
        if stored := node.history.get(incoming.id):
            if stored[0] != digest:
                raise HTTPException(409, "Task ID already has a different request")
            return {"id": incoming.id, "status": stored[1]}
        task = node.prepare(incoming)
        job = Job(node, task, source)
        node.history.claim(task.id, digest)
        node.jobs[task.id] = job
        job.handle = asyncio.create_task(job.run())
        job.handle.add_done_callback(lambda _: node.jobs.pop(task.id, None))
        return {"id": task.id, "status": job.status}

    @app.get("/api/tasks/{identifier}/events")
    async def events(identifier: str, after: int = 0):
        if not node.history.get(identifier):
            raise HTTPException(404, "No such task")
        return node.history.events(identifier, after)

    @app.post("/api/tasks/{identifier}/cancel")
    async def cancel(identifier: str):
        if identifier not in node.jobs and (stored := node.history.get(identifier)):
            return {"status": stored[1]}
        job = node.job(identifier)
        await job.stop()
        return {"status": job.status}

    @app.post("/api/tasks/{identifier}/approvals/{approval}")
    async def decide(identifier: str, approval: str, body: DecisionBody):
        if not node.job(identifier).policy.decide(approval, body.decision):
            raise HTTPException(409, "Approval expired or already resolved")
        return {"decision": body.decision.value}

    @app.get("/api/policy")
    async def policy():
        return {
            "rules": [rule.pattern for rule in node.rules],
            "mode": node.mode,
            "runners": (
                [check["name"] for check in config.get("checks", [])]
                if node.mode == "strict_hash"
                else available_runners(config)
            ),
        }

    @app.put("/api/policy")
    async def set_policy(body: RuleBody):
        await node.save_policy(body)
        return await policy()

    @app.get("/api/volumes")
    async def volumes():
        return await asyncio.to_thread(node.volume_info)

    @app.put("/api/volumes/{name}")
    async def put_volume(name: str, body: VolumeBody):
        return node.change_volume(name, body)

    @app.delete("/api/volumes/{name}")
    async def delete_volume(name: str):
        return node.change_volume(name, None)

    @app.get("/api/volumes/{name}/list")
    async def listing(name: str, path: str = ""):
        return await asyncio.to_thread(node.volume(name).entries, path)

    @app.get("/api/volumes/{name}/search")
    async def search(name: str, q: str, path: str = ""):
        return await asyncio.to_thread(node.volume(name).entries, path, q)

    @app.put("/api/volumes/{name}/upload")
    async def upload(name: str, path: str, request: Request, create_only: bool = False):
        return await node.volume(name).upload(
            path,
            request,
            config.get("max_upload_bytes", 32 * 1024 * 1024),
            create_only=create_only,
        )

    @app.get("/api/volumes/{name}/preview")
    async def preview(name: str, path: str):
        return await asyncio.to_thread(node.volume(name).serve, path, preview=True)

    @app.get("/api/volumes/{name}/download")
    async def download(name: str, path: str):
        return await asyncio.to_thread(node.volume(name).serve, path)

    @app.post("/api/wake")
    async def wake(body: WakeRequest):
        try:
            return await asyncio.to_thread(send_wake, body.mac, body.broadcast)
        except (OSError, ValueError) as error:
            raise HTTPException(422, str(error)) from error

    return app
