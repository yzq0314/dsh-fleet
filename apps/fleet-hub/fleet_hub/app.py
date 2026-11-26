import asyncio
import hmac
import json
import secrets
import socket
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from .auth import Security, bearer, digest
from .artifacts import register_artifacts
from .monitors import hardware_monitors
from .desktop import Desktop
from .engine import TERMINAL, Engine, public_node, public_task
from .models import (
    Answer,
    Dispatch,
    Enroll,
    Fields,
    NodeEdit,
    dsh_web_entry,
    uid,
)
from .store import Store, all_rows, dump, one


def must(row, label):
    if row is None:
        raise HTTPException(404, label + "不存在")
    return row


class Verify(Fields):
    id: str


def create_app(config):
    store = Store(config)
    engine = Engine(config, store)
    security = Security(config, store)
    desktop = Desktop(store, engine)
    locks = [asyncio.Lock() for _ in range(64)]

    @asynccontextmanager
    async def lifespan(_app):
        await store.open()
        workers = []
        try:
            for worker in (
                engine.run,
                engine.probe_loop,
                engine.notifications,
                engine.alerts.loop,
                desktop.expire_loop,
            ):
                workers.append(asyncio.create_task(worker()))
            yield
        finally:
            for worker in workers:
                worker.cancel()
            try:
                await asyncio.gather(*workers, return_exceptions=True)
                await engine.client.aclose()
            finally:
                await store.close()

    app = FastAPI(
        title="DSH Fleet",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.engine = engine
    security.install(app)
    from .pocket import install as install_pocket
    install_pocket(app, config, engine, security)
    engine.alerts.install(app)

    @app.get("/health")
    async def health():
        try:
            ready = store.db is not None and await store.row(
                "SELECT 1 FROM metadata WHERE key='settings'"
            )
        except sqlite3.Error:
            ready = False
        if not ready:
            raise HTTPException(503, "Hub 数据库尚未就绪")
        return {"ready": True}

    @app.get("/api/nodes")
    async def nodes():
        return [
            {
                **public_node(row),
                "pocket_embed": row["id"] in (config.get("pocket_origins") or {}),
                **engine.health.get(
                    row["id"], {"online": False, "rtt": None, "health": {}}
                ),
            }
            for row in await store.rows("SELECT id,doc FROM nodes ORDER BY rowid")
        ]

    @app.patch("/api/nodes/{node_id}")
    async def edit_node(node_id: str, data: NodeEdit):
        edits = data.model_dump(mode="json", exclude_unset=True)
        if any(
            edits.get(field, "") is None
            for field in ("url", "alias", "tags", "enabled")
        ):
            raise HTTPException(422, "必填字段不能为空")
        async with store.transaction() as db:
            row = must(
                await one(db, "SELECT doc FROM nodes WHERE id=?", node_id), "节点"
            )
            value = {**json.loads(row["doc"]), **edits}
            if value.get("dsh_web_port") and not dsh_web_entry(value):
                raise HTTPException(422, "DSH Web 需要节点的内网地址及独立于 Sidecar 的端口")
            await db.execute(
                "UPDATE nodes SET doc=? WHERE id=?", (dump(value), node_id)
            )
        engine.health.pop(node_id, None)
        return {**value, "id": node_id, "dsh_web_url": dsh_web_entry(value)}

    @app.delete("/api/nodes/{node_id}")
    async def remove_node(node_id: str):
        async with store.transaction() as db:
            must(await one(db, "SELECT id FROM nodes WHERE id=?", node_id), "节点")
            await db.execute("DELETE FROM nodes WHERE id=?", (node_id,))
            tasks = await all_rows(
                db,
                "SELECT * FROM tasks WHERE node_id=? AND status NOT IN ('completed','cancelled','failed')",
                node_id,
            )
            for task in tasks:
                await engine.finish(db, task, "failed", "节点已移除，失去控制连接")
        engine.health.pop(node_id, None)
        return {"removed": True}

    @app.post("/api/join-tokens")
    async def join_token(request: Request):
        token = secrets.token_urlsafe(24)
        now = time.time()
        expiry = now + max(1, min(3600, config.get("join_ttl", 600)))
        async with store.transaction() as db:
            await db.execute("DELETE FROM tokens WHERE expires<=?", (now,))
            await db.execute("INSERT INTO tokens VALUES (?,?)", (digest(token), expiry))
        hub = config.get("public_url", str(request.base_url).rstrip("/"))
        # Values supplied by an untrusted Host header must never enter a shell command.
        import re
        from urllib.parse import urlsplit

        parts = urlsplit(hub)
        if parts.scheme not in ("http", "https") or not re.fullmatch(
            r"[a-zA-Z0-9.:\[\]-]+", parts.netloc
        ):
            raise HTTPException(400, "请配置有效的 public_url")
        command = f"uv run python deploy/pair.py --hub {parts.scheme}://{parts.netloc} --token {token}"
        return {"token": token, "expires_at": expiry, "command": command}

    @app.post("/api/enroll")
    async def enroll(data: Enroll):
        node_id, secret = uid(), secrets.token_urlsafe(40)
        async with store.transaction() as db:
            cursor = await db.execute(
                "DELETE FROM tokens WHERE digest=? AND expires>?",
                (digest(data.token), time.time()),
            )
            if cursor.rowcount != 1:
                raise HTTPException(403, "配对码无效、已使用或已过期")
            await db.execute(
                "INSERT INTO nodes VALUES (?,?,?)",
                (node_id, secret, dump(data.model_dump(exclude={"token"}))),
            )
        return {"id": node_id, "secret": secret}

    @app.post("/api/node/verify")
    async def verify(data: Verify, request: Request):
        row = await store.row("SELECT * FROM nodes WHERE id=?", data.id)
        if (
            not row
            or not public_node(row)["enabled"]
            or not hmac.compare_digest(digest(bearer(request)), digest(row["secret"]))
        ):
            raise HTTPException(403, "节点凭据无效")
        return {"valid": True}

    @app.post("/api/nodes/{node_id}/wake")
    async def wake(node_id: str):
        node = public_node(await engine.node(node_id))
        if not node.get("mac") or not node.get("broadcast"):
            raise HTTPException(409, "请先配置 MAC 和广播地址")
        if node.get("wake_proxy"):
            proxy = await engine.node(node["wake_proxy"])
            await engine.request(
                proxy,
                "POST",
                "/api/wake",
                json={"mac": node["mac"], "broadcast": node["broadcast"]},
            )
        else:
            addr = bytes.fromhex(node["mac"].replace(":", "").replace("-", ""))
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.sendto(
                    b"\xff" * 6 + addr * 16,
                    (node["broadcast"], int(config.get("wol_port", 9))),
                )
        return {"sent": True}

    @app.post("/api/dispatch")
    async def dispatch(data: Dispatch):
        candidates = [node for node in await nodes() if node["enabled"]]
        if data.node_ids:
            by_id = {n["id"]: n for n in candidates}
            if any(nid not in by_id for nid in data.node_ids):
                raise HTTPException(404, "所选节点已移除或停用")
            candidates = [by_id[nid] for nid in dict.fromkeys(data.node_ids)]
        if data.mode == "single" and len(data.node_ids) != 1:
            raise HTTPException(422, "单机派单需要指定一个节点")
        matches = [
            n for n in candidates if set(data.tags).issubset(n["tags"]) and n["online"]
        ]
        if (
            data.node_ids
            and data.mode in {"single", "fanout"}
            and len(matches) != len(candidates)
        ):
            raise HTTPException(409, "所选节点有离线设备或标签不匹配")
        if not matches:
            raise HTTPException(409, "没有符合条件的在线节点")
        if data.mode == "auto":
            matches = [
                min(
                    matches,
                    key=lambda n: (
                        n["health"].get("active_tasks") or 0,
                        n["health"].get("cpu") or 0,
                        n["id"],
                    ),
                )
            ]
        group_id = uid()
        task_ids = []
        async with store.transaction() as db:
            for node in matches:
                must(
                    await one(db, "SELECT id FROM nodes WHERE id=?", node["id"]), "节点"
                )
                task_id = uid()
                payload = data.model_dump(
                    include={"prompt", "workspace", "agent", "context", "desktop_handoff"},
                    exclude_none=True,
                )
                if not payload.get("workspace") and not payload.get("context"):
                    payload["workspace"] = next(
                        iter(node["health"].get("workspaces", [])), ""
                    )
                payload["id"] = task_id
                await db.execute(
                    "INSERT INTO tasks(id,node_id,group_id,payload,status,created) VALUES (?,?,?,?,'queued',?)",
                    (task_id, node["id"], group_id, dump(payload), time.time()),
                )
                await engine.event(
                    db,
                    {"id": task_id, "node_id": node["id"]},
                    "status",
                    {"status": "queued"},
                )
                task_ids.append(task_id)
        engine.work_ready.set()
        return {"group_id": group_id, "tasks": task_ids}

    @app.get("/api/tasks")
    async def tasks(removed: bool = False, offset: int = 0):
        return [
            {**public_task(row), "folder": row["folder"], "removed": removed,
             "chat_online": bool(row["chat_online"])}
            for row in await store.rows(
                "SELECT t.*, coalesce(f.folder,'') AS folder, c.seen>? AS chat_online FROM tasks t "
                "LEFT JOIN task_filing f ON f.task_id=t.id LEFT JOIN chat_peers c ON c.node_id=t.node_id "
                "WHERE coalesce(f.removed,0)=? ORDER BY t.created DESC,t.id LIMIT 500 OFFSET ?",
                time.time()-45, removed, max(0,offset)
            )
        ]

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str):
        return public_task(
            must(await store.row("SELECT * FROM tasks WHERE id=?", task_id), "任务")
        )

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel(task_id: str):
        async with engine.task_lock(task_id):
            task = must(
                await store.row("SELECT * FROM tasks WHERE id=?", task_id), "任务"
            )
            if task["status"] in TERMINAL:
                return {"status": task["status"]}
            if await desktop.request_cancel(task_id):
                return {"status": "cancelling"}
            # A prior POST can reach the sidecar even if its acknowledgement is lost.
            row = await engine.node(task["node_id"])
            try:
                await engine.request(row, "POST", f"/api/tasks/{task_id}/cancel")
            except HTTPException as exc:
                if exc.status_code != 404 or task["submitted"]:
                    raise
            async with store.transaction() as db:
                await engine.finish(db, task, "cancelled")
        return {"status": "cancelled"}

    def show_event(row):
        return {**row, "data": json.loads(row["data"])}

    @app.get("/api/tasks/{task_id}/events")
    async def history(task_id: str, after: int = 0):
        await get_task(task_id)
        return [
            show_event(row)
            for row in await store.rows(
                "SELECT * FROM events WHERE task_id=? AND id>? ORDER BY id LIMIT 2000",
                task_id,
                after,
            )
        ]

    @app.get("/api/events")
    async def events(request: Request, after: int = 0, live: bool = False):
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(400, "事件游标无效")
        if live and not cursor:
            cursor = (await store.row("SELECT coalesce(max(id),0) AS id FROM events"))["id"]

        async def stream():
            pos = cursor
            while not await request.is_disconnected() and await security.valid(request):
                batch = await store.rows(
                    "SELECT * FROM events WHERE id>? ORDER BY id LIMIT 250", pos
                )
                for row in batch:
                    pos = row["id"]
                    yield f"id: {pos}\ndata: {dump(show_event(row))}\n\n"
                if not batch:
                    yield ": heartbeat\n\n"
                    await asyncio.sleep(0.8)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    @app.get("/api/approvals")
    async def approvals():
        await desktop.expire()
        return [
            {
                **json.loads(row["doc"]),
                "id": row["id"],
                "node_id": row["node_id"],
                "task_id": row["task_id"],
            }
            for row in await store.rows(
                "SELECT a.* FROM approvals a LEFT JOIN desktop_decisions d ON a.id=d.id "
                "WHERE a.state='pending' AND d.choice IS NULL ORDER BY a.rowid DESC"
            )
        ]

    @app.post("/api/approvals/{approval_id}")
    async def decide(approval_id: str, data: Answer):
        async with locks[hash(approval_id) % len(locks)]:
            row = must(
                await store.row("SELECT * FROM approvals WHERE id=?", approval_id),
                "审批",
            )
            if row["state"] != "pending":
                raise HTTPException(409, "该审批已处理")
            if await desktop.choose(approval_id, data.decision):
                return {"decision": data.decision, "state": "decided"}
            node = await engine.node(row["node_id"])
            path = (
                f"/api/tasks/{row['task_id']}/approvals/{quote(approval_id, safe='')}"
            )
            await engine.request(node, "POST", path, json=data.model_dump())
            async with store.transaction() as db:
                await engine.resolve_approval(
                    db,
                    {"id": row["task_id"], "node_id": row["node_id"]},
                    {"id": approval_id, "decision": data.decision, "cause": "operator"},
                )
        engine.work_ready.set()
        return {"decision": data.decision}

    from .resources import register_resources

    services, macros, settings = register_resources(app, store, engine)

    @app.get("/api/overview")
    async def overview():
        monitors = await asyncio.to_thread(hardware_monitors, config.get("hardware_monitor_dir"))
        watches = await store.rows("SELECT monitor,gpu FROM gpu_watches")
        for monitor in monitors:
            monitor["watching"] = [row["gpu"] for row in watches if row["monitor"] == monitor["name"]]
        return {"monitors": monitors, "nodes": await nodes(), "tasks": await tasks(), "approvals": await approvals(),
                "services": await services(), "macros": await macros(), "settings": await settings()}
    from .appearance import register_appearance
    register_appearance(app, store)
    desktop.install(app, nodes, dispatch)
    register_artifacts(app, store, engine)
    root = Path(
        config.get(
            "web_root", Path(__file__).resolve().parents[3] / "apps/web-pwa/dist"
        )
    )
    if root.is_dir():
        app.mount("/", StaticFiles(directory=root, html=True), name="console")
    else:

        @app.get("/")
        async def placeholder():
            return {
                "service": "dsh-fleet",
                "console": "请先构建 apps/web-pwa，再启动 Hub",
            }

    return app
