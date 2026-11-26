import asyncio
import json
import logging
import time
from pathlib import Path

import httpx
from fastapi import HTTPException

from .store import all_rows, dump, one
from .notifications import feishu_notify
from .alerts import Alerts, rules
from .models import dsh_web_entry

TERMINAL = {"completed", "cancelled", "failed"}
log = logging.getLogger("fleet.hub")


def public_node(row):
    doc = json.loads(row["doc"])
    return {
        "tags": [],
        "enabled": True,
        "icon": "monitor",
        **doc,
        "dsh_web_url": dsh_web_entry(doc),
        "id": row["id"],
    }


def public_task(row):
    return {
        **json.loads(row["payload"]),
        **{
            k: row[k]
            for k in ("id", "node_id", "group_id", "status", "result", "created")
        },
    }


class Engine:
    def __init__(self, config, store):
        self.config = config
        self.store = store
        self.alerts = Alerts(store, config)
        self.health = {}
        self.service_health = {}
        self.limit = asyncio.Semaphore(24)
        self.task_locks = [asyncio.Lock() for _ in range(128)]
        self.work_ready = asyncio.Event()
        self.client = httpx.AsyncClient(
            timeout=8, trust_env=False, follow_redirects=False
        )
        self.interval = max(0.1, float(config.get("poll_interval", 2)))

    async def node(self, node_id):
        row = await self.store.row("SELECT * FROM nodes WHERE id=?", node_id)
        if not row:
            raise HTTPException(404, "节点已移除或不存在")
        return row

    async def request(self, row, method, path, **kwargs):
        node = public_node(row)
        if not node["enabled"]:
            raise HTTPException(409, "节点已停用")
        try:
            async with self.limit:
                response = await self.client.request(
                    method,
                    node["url"] + path,
                    headers={"Authorization": "Bearer " + row["secret"]},
                    **kwargs,
                )
        except httpx.RequestError as exc:
            raise HTTPException(503, "节点暂时不可达") from exc
        if not response.is_success:
            try:
                msg = str(response.json()["detail"])[:500]
            except (ValueError, KeyError, TypeError):
                msg = "节点拒绝请求"
            raise HTTPException(response.status_code, msg)
        return response

    async def probe(self, row):
        node = public_node(row)
        started = time.monotonic()
        info = {"online": False, "rtt": None, "health": {}}
        if node["enabled"]:
            try:
                response = await self.request(row, "GET", "/health", timeout=3)
                health = response.json()
                if isinstance(health, dict):
                    info.update(
                        online=True,
                        rtt=round((time.monotonic() - started) * 1000, 1),
                        health=health,
                    )
            except (HTTPException, ValueError):
                pass
        # An edit or removal can finish while this request is in flight.
        latest = await self.store.row("SELECT doc FROM nodes WHERE id=?", node["id"])
        if latest and latest["doc"] == row["doc"]:
            previous = self.health.get(node["id"], {})
            self.health[node["id"]] = info
            if node["enabled"] and previous.get("online") and not info["online"]:
                async with self.store.transaction() as db:
                    await self.alerts.enqueue(db, "offline", node["id"], await rules(db), time.time())

    async def probe_services(self):
        async def probe(row):
            try:
                async with self.client.stream(
                    "GET", json.loads(row["doc"])["url"], timeout=2
                ) as response:
                    self.service_health[row["id"]] = response.status_code < 500
            except httpx.RequestError:
                self.service_health[row["id"]] = False

        await asyncio.gather(
            *(probe(row) for row in await self.store.rows("SELECT * FROM services"))
        )

    async def event(self, db, task, kind, data):
        cursor = await db.execute(
            "INSERT INTO events(task_id,type,data,created) VALUES (?,?,?,?)",
            (task["id"], kind, dump(data), time.time()),
        )
        if kind == "status" and data.get("status") == "failed":
            await self.alerts.enqueue(db, "failed", task["node_id"], await rules(db), time.time())
        if kind in {"approval_required", "approval_resolved"} and (
            self.config.get("file_hook")
            or self.config.get("webhook")
            or self.config.get("feishu_webhook")
        ):
            payload = {
                "id": cursor.lastrowid,
                "task_id": task["id"],
                "node_id": task["node_id"],
                "type": kind,
                "data": data,
            }
            await db.execute(
                "INSERT INTO hooks VALUES (?,?)", (cursor.lastrowid, dump(payload))
            )

    async def resolve_approval(self, db, task, data):
        changed = await db.execute(
            "UPDATE approvals SET state=? WHERE id=? AND task_id=? AND state='pending'",
            (data["decision"], data["id"], task["id"]),
        )
        if changed.rowcount:
            await self.event(db, task, "approval_resolved", data)

    async def close_pending(self, db, task, cause):
        pending = await all_rows(
            db,
            "SELECT id FROM approvals WHERE task_id=? AND state='pending'",
            task["id"],
        )
        for item in pending:
            await self.resolve_approval(
                db, task, {"id": item["id"], "decision": "closed", "cause": cause}
            )

    async def finish(self, db, task, status, message=""):
        if message:
            await db.execute(
                "UPDATE tasks SET result=? WHERE id=?", (message, task["id"])
            )
        await db.execute("UPDATE tasks SET status=? WHERE id=?", (status, task["id"]))
        await self.close_pending(db, task, status)
        await self.event(db, task, "status", {"status": status, "message": message})

    async def apply_events(self, task, events):
        async with self.store.transaction() as db:
            fresh = await one(db, "SELECT * FROM tasks WHERE id=?", task["id"])
            if not fresh or fresh["status"] in TERMINAL:
                return
            cursor = fresh["cursor"]
            for event in events:
                seq = event["seq"]
                if seq <= cursor:
                    continue
                if seq != cursor + 1:
                    raise ValueError("Sidecar journal sequence gap")
                kind, data = event["type"], event["data"]
                if not isinstance(data, dict):
                    raise TypeError("invalid event payload")
                if kind == "approval_required":
                    await db.execute(
                        "INSERT OR IGNORE INTO approvals VALUES (?,?,?,?,'pending')",
                        (data["id"], task["id"], task["node_id"], dump(data)),
                    )
                elif kind == "approval_resolved":
                    await self.resolve_approval(db, task, data)
                elif kind == "result":
                    await db.execute(
                        "UPDATE tasks SET result=? WHERE id=?",
                        (data.get("text", ""), task["id"]),
                    )
                elif kind == "status":
                    if data["status"] in TERMINAL:
                        await self.close_pending(db, task, data["status"])
                    await db.execute(
                        "UPDATE tasks SET status=? WHERE id=?",
                        (data["status"], task["id"]),
                    )
                if kind != "approval_resolved":
                    await self.event(db, task, kind, data)
                cursor = seq
            await db.execute(
                "UPDATE tasks SET cursor=? WHERE id=?", (cursor, task["id"])
            )

    def task_lock(self, task_id):
        return self.task_locks[hash(task_id) % len(self.task_locks)]

    async def sync_task(self, task):
        async with self.task_lock(task["id"]):
            latest = await self.store.row("SELECT * FROM tasks WHERE id=?", task["id"])
            if latest and latest["status"] not in TERMINAL:
                await self._sync_task(latest)

    async def _sync_task(self, task):
        try:
            row = await self.node(task["node_id"])
            if not task["submitted"]:
                await self.request(
                    row, "POST", "/api/tasks", json=json.loads(task["payload"])
                )
                async with self.store.transaction() as db:
                    changed = await db.execute(
                        "UPDATE tasks SET submitted=1 WHERE id=? AND submitted=0",
                        (task["id"],),
                    )
                    if changed.rowcount:
                        await self.event(db, task, "dispatch_ack", {})
            response = await self.request(
                row,
                "GET",
                f"/api/tasks/{task['id']}/events",
                params={"after": task["cursor"]},
            )
            await self.apply_events(task, response.json())
        except HTTPException as exc:
            if exc.status_code in {400, 401, 403, 404, 409, 422}:
                async with self.store.transaction() as db:
                    fresh = await one(
                        db, "SELECT status FROM tasks WHERE id=?", task["id"]
                    )
                    if fresh and fresh["status"] not in TERMINAL:
                        await self.finish(db, task, "failed", str(exc.detail))

    async def probe_loop(self):
        while True:
            try:
                rows = await self.store.rows("SELECT * FROM nodes")
                await self.batch([self.probe(row) for row in rows])
            except Exception:
                log.exception("Fleet health polling interrupted")
            await asyncio.sleep(self.interval)

    async def batch(self, items):
        for result in await asyncio.gather(*items, return_exceptions=True):
            if isinstance(result, Exception):
                log.error("Fleet worker failure: %s", type(result).__name__)

    async def run(self):
        while True:
            jobs = []
            try:
                self.work_ready.clear()
                jobs = await self.store.rows(
                    "SELECT t.* FROM tasks t LEFT JOIN desktop_sessions d ON t.id=d.task_id "
                    "WHERE d.task_id IS NULL AND t.status NOT IN ('completed','failed','cancelled')"
                )
                await self.batch([self.sync_task(task) for task in jobs])
            except Exception:
                log.exception("Fleet polling interrupted")
            try:
                await asyncio.wait_for(
                    self.work_ready.wait(),
                    timeout=min(0.5, self.interval) if jobs else self.interval,
                )
            except TimeoutError:
                pass

    async def deliver_sink(self, sink):
        cursor = await self.store.row(
            "SELECT event_id FROM hook_offsets WHERE sink=?", sink
        )
        offset = cursor["event_id"] if cursor else 0
        rows = await self.store.rows(
            "SELECT * FROM hooks WHERE event_id>? ORDER BY event_id LIMIT 100", offset
        )
        for row in rows:
            event_id, payload = row["event_id"], row["payload"]
            try:
                if sink == "file_hook":

                    def append():
                        path = Path(self.config[sink])
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with path.open("a", encoding="utf-8") as stream:
                            stream.write(payload + "\n")

                    await asyncio.to_thread(append)
                elif json.loads(payload)["type"] == "monitor_alert":
                    pass  # Operational notices are private to the local owner relay.
                elif sink == "webhook":
                    headers = {
                        "Content-Type": "application/json",
                        "Idempotency-Key": str(event_id),
                    }
                    if secret := self.config.get("webhook_token"):
                        headers["Authorization"] = "Bearer " + secret
                    response = await self.client.post(
                        self.config[sink], content=payload.encode(), headers=headers
                    )
                    response.raise_for_status()
                else:
                    await feishu_notify(self.client, self.config, json.loads(payload))
            except (OSError, httpx.HTTPError):
                log.warning("Notification delivery pending for %s", sink)
                break
            async with self.store.transaction() as db:
                await db.execute(
                    "INSERT OR REPLACE INTO hook_offsets VALUES (?,?)", (sink, event_id)
                )
            offset = event_id
        return offset

    async def flush_notifications(self):
        sinks = [
            key
            for key in ("file_hook", "webhook", "feishu_webhook")
            if self.config.get(key)
        ]
        if sinks:
            offsets = await asyncio.gather(*(self.deliver_sink(sink) for sink in sinks))
            async with self.store.transaction() as db:
                await db.execute("DELETE FROM hooks WHERE event_id<=?", (min(offsets),))

    async def notifications(self):
        while True:
            await asyncio.gather(self.probe_services(), self.flush_notifications())
            await asyncio.sleep(max(1, self.interval))
