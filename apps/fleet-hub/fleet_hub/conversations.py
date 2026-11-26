import json
import time
from typing import Literal

from fastapi import HTTPException
from pydantic import Field

from .engine import TERMINAL
from .models import Fields
from .store import all_rows, one


class Reply(Fields):
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    text: str = Field(min_length=1, max_length=16000)


class Receipt(Fields):
    state: Literal["accepted", "rejected"]


class Filing(Fields):
    folder: str | None = Field(default=None, max_length=64)
    removed: bool | None = None


def register(app, peer, store, engine):
    @app.patch("/api/tasks/{task_id}/filing")
    async def file_task(task_id: str, value: Filing):
        async with store.transaction() as db:
            task = await one(
                db, "SELECT id,node_id,status FROM tasks WHERE id=?", task_id
            )
            if not task:
                raise HTTPException(404, "任务不存在")
            if value.removed and (
                task["status"] not in TERMINAL
                or await one(
                    db,
                    "SELECT 1 FROM chat_turns WHERE task_id=? AND state='queued' AND created>?",
                    task_id,
                    time.time() - 120,
                )
            ):
                raise HTTPException(409, "请等待任务或待发送消息结束")
            await db.execute(
                "INSERT OR IGNORE INTO task_filing(task_id) VALUES (?)", (task_id,)
            )
            await db.execute(
                "UPDATE task_filing SET folder=coalesce(?,folder),removed=coalesce(?,removed) WHERE task_id=?",
                (
                    value.folder.strip() if value.folder is not None else None,
                    value.removed,
                    task_id,
                ),
            )
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/replies")
    async def reply(task_id: str, body: Reply):
        prompt = body.text.strip()
        if not prompt:
            raise HTTPException(422, "请输入消息")
        async with store.transaction() as db:
            previous = await one(
                db, "SELECT * FROM chat_turns WHERE id=?", body.request_id
            )
            if previous:
                if previous["task_id"] != task_id or previous["prompt"] != prompt:
                    raise HTTPException(409, "消息编号冲突")
                return {"state": previous["state"]}
            task = await one(db, "SELECT * FROM tasks WHERE id=?", task_id)
            if not task:
                raise HTTPException(404, "任务不存在")
            if task["status"] not in TERMINAL or await one(
                db, "SELECT 1 FROM task_filing WHERE task_id=? AND removed=1", task_id
            ):
                raise HTTPException(409, "请在会话空闲并恢复到队列后发送")
            node_id = task["node_id"]
            node = await one(db, "SELECT doc FROM nodes WHERE id=?", node_id)
            if not node or not json.loads(node["doc"]).get("enabled", True):
                raise HTTPException(409, "节点已停用或移除")
            if not await one(
                db,
                "SELECT 1 FROM chat_peers WHERE node_id=? AND seen>?",
                node_id,
                time.time() - 45,
            ):
                raise HTTPException(
                    409, "桌面续聊未连接，请更新 Fleet 桌面插件并打开 DSH App"
                )
            session = await one(
                db, "SELECT native_id FROM desktop_sessions WHERE task_id=?", task_id
            )
            if not session:
                row = await one(
                    db,
                    "SELECT data FROM events WHERE task_id=? AND type='desktop_handoff' ORDER BY id DESC LIMIT 1",
                    task_id,
                )
                handoff = json.loads(row["data"]) if row else {}
                if handoff.get("status") != "ready" or not handoff.get("session_id"):
                    raise HTTPException(409, "任务尚未交接到桌面会话")
                sid = handoff["session_id"]
                if await one(
                    db,
                    "SELECT 1 FROM desktop_sessions WHERE node_id=? AND native_id=?",
                    node_id,
                    sid,
                ):
                    raise HTTPException(409, "请从已同步的桌面会话中续聊")
                await db.execute(
                    "INSERT INTO desktop_sessions(task_id,node_id,native_id) VALUES (?,?,?)",
                    (task_id, node_id, sid),
                )
            else:
                sid = session["native_id"]
            if await one(
                db,
                "SELECT 1 FROM chat_turns WHERE task_id=? AND state='queued' AND created>?",
                task_id,
                time.time() - 120,
            ):
                raise HTTPException(409, "上一条消息正在等待桌面接收")
            await db.execute(
                "INSERT INTO chat_turns VALUES (?,?,?,?,?,'queued',?)",
                (body.request_id, task_id, node_id, sid, prompt, time.time()),
            )
            await engine.event(
                db,
                task,
                "chat_delivery",
                {"state": "queued", "text": prompt, "request_id": body.request_id},
            )
        return {"state": "queued"}

    @peer.post("/turns")
    async def collect(node_id: str):
        now = time.time()
        async with store.transaction() as db:
            await db.execute(
                "INSERT OR REPLACE INTO chat_peers VALUES (?,?)", (node_id, now)
            )
            for row in await all_rows(
                db,
                "SELECT id,task_id FROM chat_turns WHERE node_id=? AND state='queued' AND created<=?",
                node_id,
                now - 120,
            ):
                await db.execute(
                    "UPDATE chat_turns SET state='expired' WHERE id=?", (row["id"],)
                )
                await engine.event(
                    db,
                    {"id": row["task_id"], "node_id": node_id},
                    "chat_delivery",
                    {"state": "expired"},
                )
            return await all_rows(
                db,
                "SELECT id,task_id,native_id,prompt FROM chat_turns WHERE node_id=? AND state='queued' ORDER BY created LIMIT 8",
                node_id,
            )

    @peer.post("/turns/{turn_id}")
    async def acknowledge(node_id: str, turn_id: str, body: Receipt):
        async with store.transaction() as db:
            row = await one(
                db,
                "SELECT task_id,state FROM chat_turns WHERE id=? AND node_id=?",
                turn_id,
                node_id,
            )
            if not row:
                raise HTTPException(404, "消息不存在")
            if row["state"] == "queued":
                await db.execute(
                    "UPDATE chat_turns SET state=? WHERE id=?", (body.state, turn_id)
                )
                await engine.event(
                    db,
                    {"id": row["task_id"], "node_id": node_id},
                    "chat_delivery",
                    {"state": body.state},
                )
        return {"ok": True}
