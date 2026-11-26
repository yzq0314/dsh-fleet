"""Hardware alerts use the existing private file-relay outbox."""
import asyncio
import hashlib
import logging
import time

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .monitors import hardware_monitors
from .store import all_rows, dump, one


class Rules(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    disk: bool = True
    gpu: bool = True
    failed: bool = False
    offline: bool = False
    threshold: int = Field(default=95, ge=1, le=100)
    cooldown_hours: int = Field(default=6, ge=1, le=168)


async def rules(db):
    row = await one(db, "SELECT doc FROM notification_rules WHERE slot=1")
    return Rules.model_validate_json(row["doc"]) if row else Rules()


class Alerts:
    def __init__(self, store, config):
        self.store = store
        self.config = config

    async def enqueue(self, db, kind, subject, policy, now, **numbers):
        if not getattr(policy, kind) or not self.config.get("file_hook"):
            return False
        last = await one(db, "SELECT last_sent FROM alert_clock WHERE kind=? AND subject=?", kind, subject)
        if last and now - last["last_sent"] < policy.cooldown_hours * 3600:
            return False
        # No monitor labels, task prompts, paths or recipient data enter Hermes.
        key = hashlib.sha256(subject.encode()).hexdigest()[:16]
        data = {"id": key, "kind": kind, **numbers}
        cur = await db.execute("INSERT INTO events(task_id,type,data,created) VALUES ('','monitor_alert',?,?)", (dump(data), now))
        payload = {"id": cur.lastrowid, "type": "monitor_alert", "data": data}
        await db.execute("INSERT INTO hooks VALUES (?,?)", (cur.lastrowid, dump(payload)))
        await db.execute("INSERT OR REPLACE INTO alert_clock VALUES (?,?,?)", (kind, subject, now))
        return True

    async def observe(self, samples, now):
        async with self.store.transaction() as db:
            policy = await rules(db)
            for sample in samples:
                if not sample["fresh"]:
                    continue
                name, stamp = sample["name"], sample["timestamp"]
                for mount, usage in {**sample.get("partitions", {}), "/": sample["disk"]}.items():
                    subject = dump([name, mount])
                    if usage < policy.threshold:
                        await db.execute("DELETE FROM alert_clock WHERE kind='disk' AND subject=?", (subject,))
                    else:
                        await self.enqueue(db, "disk", subject, policy, now, usage=round(usage, 1))
                for watch in await all_rows(db, "SELECT * FROM gpu_watches WHERE monitor=?", name):
                    idx = watch["gpu"]
                    if stamp <= watch["stamp"]:
                        continue
                    gpu = sample["gpu"][idx] if idx < len(sample["gpu"]) else None
                    idle = gpu and gpu["used_mib"] < 1024 and gpu["utilization"] < 10
                    previous = watch["streak"] if stamp-watch["stamp"] <= 25 else 0
                    streak = min(3, previous + 1) if idle and policy.gpu else 0
                    if streak == 3 and await self.enqueue(db, "gpu", dump([name, idx]), policy, now, gpu=idx):
                        await db.execute("DELETE FROM gpu_watches WHERE monitor=? AND gpu=?", (name, idx))
                    else:
                        await db.execute("UPDATE gpu_watches SET stamp=?,streak=? WHERE monitor=? AND gpu=?", (stamp, streak, name, idx))

    async def loop(self):
        while True:
            try:
                samples = await asyncio.to_thread(hardware_monitors, self.config.get("hardware_monitor_dir"))
                await self.observe(samples, time.time())
            except Exception:
                logging.getLogger("fleet.hub").exception("Hardware alert sampling interrupted")
            await asyncio.sleep(5)

    def install(self, app):
        @app.get("/api/notification-rules")
        async def get_rules():
            async with self.store.transaction() as db:
                return (await rules(db)).model_dump()

        @app.put("/api/notification-rules")
        async def put_rules(body: Rules):
            async with self.store.transaction() as db:
                await db.execute("INSERT OR REPLACE INTO notification_rules VALUES (1,?)", (body.model_dump_json(),))
                if not body.gpu:
                    await db.execute("UPDATE gpu_watches SET streak=0")
            return body

        @app.put("/api/gpu-watch/{index}")
        async def watch(index: int, monitor: str, enabled: bool):
            async with self.store.transaction() as db:
                if enabled:
                    samples = await asyncio.to_thread(hardware_monitors, self.config.get("hardware_monitor_dir"))
                    src = next((s for s in samples if s["name"] == monitor and s["fresh"]), None)
                    if not src or not 0 <= index < len(src["gpu"]):
                        raise HTTPException(409, "采样过期或设备已变更")
                    if not (await rules(db)).gpu:
                        raise HTTPException(409, "请先启用 GPU 蹲守通知")
                    cur = await db.execute("INSERT OR IGNORE INTO gpu_watches VALUES (?,?,?,0)", (monitor, index, src["timestamp"]))
                    if cur.rowcount:
                        await db.execute("DELETE FROM alert_clock WHERE kind='gpu' AND subject=?", (dump([monitor, index]),))
                else:
                    await db.execute("DELETE FROM gpu_watches WHERE monitor=? AND gpu=?", (monitor, index))
            return {"enabled": enabled}
