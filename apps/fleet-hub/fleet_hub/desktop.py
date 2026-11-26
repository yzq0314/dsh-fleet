"""A node-scoped projection of explicitly shared desktop sessions."""

import asyncio
import hashlib
import json
import math
import time
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field

from .engine import TERMINAL
from .conversations import register as register_conversations
from .models import Dispatch, Fields, uid
from .store import all_rows, dump, one


class Heartbeat(Fields):
    version: str = Field(min_length=1, max_length=80)


class DesktopEvent(Fields):
    session_id: str = Field(min_length=1, max_length=256)
    event_id: str = Field(min_length=1, max_length=256)
    type: Literal[
        "status",
        "message",
        "user_message",
        "result",
        "log",
        "tool_call",
        "tool_result",
        "approval_required",
        "approval_resolved",
    ]
    data: dict


class DesktopDispatch(Fields):
    node_id: str
    prompt: str = Field(min_length=1, max_length=32768)
    workspace: str = ""
    agent: str = Field(default="dsh", pattern=r"^[\w.-]{1,48}$")


def scoped_id(*parts):
    return hashlib.sha256(dump(parts).encode()).hexdigest()[:32]


def text_field(data, key, maximum=32768, default=""):
    value = data.get(key, default)
    if not isinstance(value, str) or len(value) > maximum:
        raise HTTPException(422, "桌面事件字段无效")
    return value


class Desktop:
    def __init__(self, store, engine):
        self.store, self.engine = store, engine

    async def request_cancel(self, task_id):
        async with self.store.transaction() as db:
            changed = await db.execute(
                "UPDATE desktop_sessions SET cancel_requested=1 WHERE task_id=?",
                (task_id,),
            )
        return bool(changed.rowcount)

    async def choose(self, approval_id, decision):
        async with self.store.transaction() as db:
            record = await one(
                db,
                """SELECT d.*,a.state,n.doc AS peer FROM desktop_decisions d
                   JOIN approvals a ON a.id=d.id LEFT JOIN nodes n ON n.id=a.node_id WHERE d.id=?""",
                approval_id,
            )
            if not record:
                return False
            if not record["peer"] or not json.loads(record["peer"]).get(
                "enabled", True
            ):
                raise HTTPException(409, "节点授权已撤销")
            if decision not in {"approve_once", "deny"}:
                raise HTTPException(422, "桌面原生审批仅支持本次调用")
            if (
                record["choice"]
                or record["state"] != "pending"
                or record["expires"] <= time.time()
            ):
                raise HTTPException(409, "审批已处理或过期")
            await db.execute(
                "UPDATE desktop_decisions SET choice=? WHERE id=?",
                (decision, approval_id),
            )
        return True

    async def expire(self):
        async with self.store.transaction() as db:
            for row in await all_rows(
                db,
                "SELECT a.* FROM approvals a JOIN desktop_decisions d ON d.id=a.id WHERE a.state='pending' AND d.expires<=?",
                time.time(),
            ):
                await self.engine.resolve_approval(
                    db,
                    {"id": row["task_id"], "node_id": row["node_id"]},
                    {"id": row["id"], "decision": "deny", "cause": "timeout"},
                )

    async def expire_loop(self):
        while True:
            await self.expire()
            await asyncio.sleep(1)

    async def project(self, node_id, incoming):
        data = incoming.data
        body = json.dumps(incoming.model_dump(), sort_keys=True, ensure_ascii=False)
        if len(body.encode()) > 65536:
            raise HTTPException(413, "桌面事件超过大小限制")
        fingerprint = hashlib.sha256(body.encode()).hexdigest()
        task_id = scoped_id("desktop", node_id, incoming.session_id)
        task = {"id": task_id, "node_id": node_id}
        result = {"task_id": task_id}
        async with self.store.transaction() as db:
            peer = await one(db, "SELECT doc FROM nodes WHERE id=?", node_id)
            if not peer or not json.loads(peer["doc"]).get("enabled", True):
                raise HTTPException(403, "设备授权已撤销")
            linked = await one(db, "SELECT task_id FROM desktop_sessions WHERE node_id=? AND native_id=?", node_id, incoming.session_id)
            if linked:
                task_id = linked["task_id"]
                task["id"] = result["task_id"] = task_id
            receipt = await one(
                db,
                "SELECT * FROM desktop_receipts WHERE task_id=? AND event_id=?",
                task_id,
                incoming.event_id,
            )
            if receipt:
                if receipt["digest"] != fingerprint:
                    raise HTTPException(409, "事件 ID 已被其他内容使用")
                return json.loads(receipt["result"])
            if not await one(
                db, "SELECT 1 FROM desktop_sessions WHERE task_id=?", task_id
            ):
                payload = {
                    "id": task_id,
                    "agent": "dsh-desktop",
                    "origin": "desktop",
                    "prompt": text_field(data, "title", 512) or "DSH 桌面会话",
                    "workspace": text_field(data, "workspace", 4096),
                }
                await db.execute(
                    "INSERT INTO tasks(id,node_id,group_id,payload,status,created,submitted) VALUES (?,?,?,?,'running',?,1)",
                    (task_id, node_id, task_id, dump(payload), time.time()),
                )
                await db.execute(
                    "INSERT INTO desktop_sessions(task_id,node_id,native_id) VALUES (?,?,?)",
                    (task_id, node_id, incoming.session_id),
                )
            kind = incoming.type
            if kind.startswith("approval_"):
                native_id = text_field(data, "id", 256)
                if not native_id:
                    raise HTTPException(422, "缺少原生审批 ID")
                aid = scoped_id("approval", task_id, native_id)
                result["approval_id"] = aid
                if kind == "approval_required":
                    if await one(db, "SELECT id FROM approvals WHERE id=?", aid):
                        raise HTTPException(409, "该原生审批已登记")
                    now = time.time()
                    expiry = data.get("expires_at", now + 120)
                    if not isinstance(expiry, (int, float)) or not math.isfinite(
                        expiry
                    ):
                        raise HTTPException(422, "审批截止时间无效")
                    expiry = min(expiry, now + 600)
                    detail = {
                        key: text_field(data, key)
                        for key in ("command", "workspace", "reason")
                    }
                    detail.update(
                        id=aid, expires_at=expiry, decisions=["approve_once", "deny"]
                    )
                    expired = expiry <= now
                    await db.execute(
                        "INSERT INTO approvals VALUES (?,?,?,?,?)",
                        (
                            aid,
                            task_id,
                            node_id,
                            dump(detail),
                            "deny" if expired else "pending",
                        ),
                    )
                    await db.execute(
                        "INSERT INTO desktop_decisions(id,expires,native_id) VALUES (?,?,?)",
                        (aid, expiry, native_id),
                    )
                    if expired:
                        # A delayed sender must be able to advance its journal. Never re-open an
                        # expired native prompt. The later native outcome can still be audited.
                        result["expired"] = True
                        await db.execute(
                            "UPDATE desktop_decisions SET choice='expired-on-arrival' WHERE id=?",
                            (aid,),
                        )
                        await self.engine.event(
                            db,
                            task,
                            "log",
                            {
                                "text": "收到已过期的桌面审批，Hub 已关闭该请求",
                                "approval_id": aid,
                            },
                        )
                    else:
                        await self.engine.event(db, task, kind, detail)
                else:
                    decision = data.get("decision")
                    if decision not in {"approve_once", "deny", "closed"}:
                        raise HTTPException(422, "桌面审批结果无效")
                    choice = await one(
                        db, "SELECT choice FROM desktop_decisions WHERE id=?", aid
                    )
                    if not choice:
                        raise HTTPException(404, "原生审批尚未登记")
                    outcome = {
                        "id": aid,
                        "decision": decision,
                        "cause": text_field(data, "cause", 100, "desktop"),
                    }
                    if choice["choice"] == "expired-on-arrival":
                        await db.execute(
                            "UPDATE desktop_decisions SET choice=? WHERE id=?",
                            (decision, aid),
                        )
                        await db.execute(
                            "UPDATE approvals SET state=? WHERE id=?", (decision, aid)
                        )
                        await self.engine.event(db, task, kind, outcome)
                    else:
                        await self.engine.resolve_approval(db, task, outcome)
            elif kind == "status":
                status = data.get("status")
                if status not in TERMINAL | {"running"}:
                    raise HTTPException(422, "会话状态无效")
                if status in TERMINAL:
                    await db.execute(
                        "UPDATE desktop_sessions SET cancel_requested=0 WHERE task_id=?",
                        (task_id,),
                    )
                    await self.engine.finish(db, task, status)
                else:
                    await db.execute(
                        "UPDATE tasks SET status='running' WHERE id=?", (task_id,)
                    )
                    await self.engine.event(db, task, kind, {"status": status})
            else:
                detail = {
                    key: data[key]
                    for key in ("text", "name", "command", "call_id", "success", "request_id")
                    if key in data and isinstance(data[key], (str, bool))
                }
                if kind == "result":
                    await db.execute(
                        "UPDATE tasks SET result=? WHERE id=?",
                        (text_field(data, "text"), task_id),
                    )
                await self.engine.event(db, task, kind, detail)
            await db.execute(
                "INSERT INTO desktop_receipts VALUES (?,?,?,?)",
                (task_id, incoming.event_id, fingerprint, dump(result)),
            )
        return result

    def install(self, app, list_nodes, dispatch):
        async def scoped_peer(request: Request, node_id: str):
            if getattr(request.state, "desktop_node", None) != node_id:
                raise HTTPException(403, "需要本节点专用的桌面插件凭据")

        router = APIRouter(
            prefix="/api/desktop/{node_id}", dependencies=[Depends(scoped_peer)]
        )

        def rights(node_id):
            return self.engine.config.get("desktop_clients", {}).get(node_id, {})

        @app.get("/api/nodes/{node_id}/desktop")
        async def info(node_id: str):
            await self.engine.node(node_id)
            row = await self.store.row(
                "SELECT * FROM desktop_peers WHERE node_id=?", node_id
            )
            count = await self.store.row(
                "SELECT COUNT(*) AS n FROM desktop_sessions d JOIN tasks t ON d.task_id=t.id WHERE d.node_id=? AND t.status='running'",
                node_id,
            )
            return {
                "online": bool(row and time.time() - row["last_seen"] < 60),
                "last_seen": row["last_seen"] if row else None,
                "version": row["version"] if row else None,
                "sessions": count["n"],
                "install_hint": "uv run python integrations/dsh-app-plugin/manage.py install --node-config local/node.local.yaml",
            }

        @router.post("/heartbeat")
        async def heartbeat(node_id: str, data: Heartbeat):
            async with self.store.transaction() as db:
                await db.execute(
                    "INSERT INTO desktop_peers VALUES (?,?,?) ON CONFLICT(node_id) DO UPDATE SET version=excluded.version,last_seen=excluded.last_seen",
                    (node_id, data.version, time.time()),
                )
                pending = await all_rows(
                    db,
                    "SELECT native_id FROM desktop_sessions WHERE node_id=? AND cancel_requested=1",
                    node_id,
                )
            return {"ok": True, "conversation_protocol": 1, "cancel_sessions": [r["native_id"] for r in pending]}

        @router.post("/events")
        async def event(node_id: str, data: DesktopEvent):
            return await self.project(node_id, data)

        @router.get("/decisions/{aid}")
        async def poll(node_id: str, aid: str):
            row = await self.store.row(
                "SELECT d.*,a.state FROM desktop_decisions d JOIN approvals a ON a.id=d.id WHERE d.id=? AND a.node_id=?",
                aid,
                node_id,
            )
            if not row:
                raise HTTPException(404, "审批不存在")
            if row["expires"] <= time.time():
                return {"state": "expired", "decision": "deny"}
            if row["state"] != "pending":
                return {"state": "resolved", "decision": "deny"}
            return {
                "state": "decided" if row["choice"] else "pending",
                "decision": row["choice"],
            }

        @router.get("/nodes")
        async def targets(node_id: str):
            allowed = set(rights(node_id).get("targets", [])) | set(
                rights(node_id).get("volumes", {})
            )
            return [
                {k: n[k] for k in ("id", "alias", "tags", "online", "rtt")}
                for n in await list_nodes()
                if n["id"] in allowed
            ]

        @router.post("/dispatch")
        async def send(node_id: str, data: DesktopDispatch):
            if data.node_id not in rights(node_id).get("targets", []):
                raise HTTPException(403, "该节点不在插件可派单范围内")
            return await dispatch(
                Dispatch(
                    mode="single",
                    node_ids=[data.node_id],
                    prompt=data.prompt,
                    workspace=data.workspace,
                    agent=data.agent,
                )
            )

        @router.get("/files/{target}/{volume}")
        async def files(
            node_id: str,
            target: str,
            volume: str,
            path: str = "",
            action: Literal["list", "preview"] = "list",
        ):
            if volume not in rights(node_id).get("volumes", {}).get(target, []):
                raise HTTPException(403, "插件未获该卷的读取授权")
            node = await self.engine.node(target)
            endpoint = "/api/volumes/" + quote(volume, safe="") + "/" + action
            if action == "preview":
                # Restrict this text-only bridge before fetching image or binary content.
                if not path.lower().endswith(
                    (".txt", ".csv", ".tsv", ".md", ".log", ".json", ".yaml", ".yml")
                ):
                    raise HTTPException(415, "该工具只预览文本")
            resp = await self.engine.request(
                node, "GET", endpoint, params={"path": path}
            )
            return resp.json() if action == "list" else {"text": resp.text[:16384]}

        register_conversations(app, router, self.store, self.engine)
        app.include_router(router)
