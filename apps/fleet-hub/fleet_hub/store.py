import asyncio
import json
from contextlib import asynccontextmanager

import aiosqlite

from packages.protocol.ownership import claim_database

from .models import NodeEdit, Preferences, Service, dsh_web_entry, uid

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
CREATE TABLE IF NOT EXISTS ui_preferences (id INTEGER PRIMARY KEY CHECK(id=1), doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ui_wallpaper (id INTEGER PRIMARY KEY CHECK(id=1), mime TEXT NOT NULL, image BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS notification_rules(slot INTEGER PRIMARY KEY CHECK(slot=1), doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alert_clock(kind TEXT NOT NULL, subject TEXT NOT NULL, last_sent REAL NOT NULL, PRIMARY KEY(kind,subject));
CREATE TABLE IF NOT EXISTS gpu_watches(monitor TEXT NOT NULL, gpu INTEGER NOT NULL, stamp INTEGER NOT NULL, streak INTEGER NOT NULL, PRIMARY KEY(monitor,gpu));
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS nodes (id TEXT PRIMARY KEY, secret TEXT NOT NULL, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, node_id TEXT NOT NULL, group_id TEXT NOT NULL,
    payload TEXT NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL DEFAULT '', cursor INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL, submitted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
    type TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS events_per_task ON events(task_id,id);
CREATE INDEX IF NOT EXISTS active_tasks ON tasks(status);
CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, node_id TEXT NOT NULL,
    doc TEXT NOT NULL, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (digest TEXT PRIMARY KEY, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tokens (digest TEXT PRIMARY KEY, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS services (id TEXT PRIMARY KEY, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS macros (id TEXT PRIMARY KEY, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hooks (event_id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hook_offsets (sink TEXT PRIMARY KEY, event_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS desktop_peers(node_id TEXT PRIMARY KEY, version TEXT NOT NULL, last_seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS task_filing(task_id TEXT PRIMARY KEY, folder TEXT NOT NULL DEFAULT '', removed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS chat_peers(node_id TEXT PRIMARY KEY, seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS chat_turns(id TEXT PRIMARY KEY, task_id TEXT NOT NULL, node_id TEXT NOT NULL, native_id TEXT NOT NULL, prompt TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS chat_delivery ON chat_turns(node_id,state,created);
CREATE TABLE IF NOT EXISTS desktop_sessions(task_id TEXT PRIMARY KEY, node_id TEXT NOT NULL, native_id TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0, UNIQUE(node_id,native_id));
CREATE TABLE IF NOT EXISTS desktop_receipts(task_id TEXT NOT NULL, event_id TEXT NOT NULL, digest TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(task_id,event_id));
CREATE TABLE IF NOT EXISTS desktop_decisions(id TEXT PRIMARY KEY, expires REAL NOT NULL, choice TEXT, native_id TEXT NOT NULL);
"""

DEFAULT_MACROS = [
    ("仓库概览", "概述 {{repo}} 的目录结构和主要入口。"),
    ("分支审阅", "审阅 {{branch}} 的改动，总结需要人工核对的问题。"),
    ("日志摘要", "阅读 {{file}}，按时间归纳关键事件。"),
    ("测试计划", "为 {{module}} 拟定本地测试计划和验收条件。"),
]


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    def __init__(self, config):
        self.config = config
        self.lock = asyncio.Lock()
        self.owner = None
        self.db = None

    async def open(self):
        path = self.config["database"]
        if self.db is not None or self.owner is not None:
            raise RuntimeError("Hub store is already open")
        self.owner = claim_database(path, "Hub")
        try:
            self.db = await aiosqlite.connect(path, isolation_level=None)
            await self._initialize()
        except BaseException:
            await self.close()
            raise

    async def close(self):
        try:
            if self.db is not None:
                await self.db.close()
                self.db = None
        finally:
            if self.owner:
                self.owner.close()
                self.owner = None

    async def _initialize(self):
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript(SCHEMA)
        async with self.transaction() as db:
            if await one(db, "SELECT 1 FROM metadata WHERE key='settings'"):
                return
            await db.execute(
                "INSERT INTO metadata VALUES ('settings',?)",
                (dump(Preferences().model_dump()),),
            )
            for node in self.config.get("nodes", []):
                public = NodeEdit.model_validate(
                    {k: v for k, v in node.items() if k not in {"id", "secret"}}
                ).model_dump(mode="json", exclude_none=True)
                if (
                    not public.get("alias")
                    or not public.get("url")
                    or not node.get("secret")
                ):
                    raise ValueError("初始节点需要 alias、url 和 secret")
                if public.get("dsh_web_port") and not dsh_web_entry(public):
                    raise ValueError("DSH Web 需要节点的内网地址及独立于 Sidecar 的端口")
                await db.execute(
                    "INSERT INTO nodes VALUES (?,?,?)",
                    (node.get("id") or uid(), node["secret"], dump(public)),
                )
            for item in self.config.get("services", []):
                await db.execute(
                    "INSERT INTO services VALUES (?,?)",
                    (
                        item.get("id") or uid(),
                        dump(
                            Service.model_validate(
                                {k: v for k, v in item.items() if k != "id"}
                            ).model_dump()
                        ),
                    ),
                )
            for position, (name, template) in enumerate(DEFAULT_MACROS):
                await db.execute(
                    "INSERT INTO macros VALUES (?,?)",
                    (
                        uid(),
                        dump(
                            {"name": name, "template": template, "position": position}
                        ),
                    ),
                )

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise

    async def rows(self, sql, *params):
        async with self.lock:
            return await all_rows(self.db, sql, *params)

    async def row(self, sql, *params):
        async with self.lock:
            return await one(self.db, sql, *params)


async def all_rows(db, sql, *params):
    async with db.execute(sql, params) as cursor:
        return [dict(row) for row in await cursor.fetchall()]


async def one(db, sql, *params):
    async with db.execute(sql, params) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row is not None else None
