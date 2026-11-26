"""Single-owner, durable dispatch receipts and ordered events on a node."""

import hashlib
import json
import sqlite3

from packages.protocol.ownership import claim_database


def fingerprint(payload):
    value = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(value.encode()).hexdigest()


class History:
    def __init__(self, path):
        self.lock = claim_database(path, "sidecar")
        self.db = None
        try:
            self.db = sqlite3.connect(path)
            self._initialize()
        except BaseException:
            self.close()
            raise

    def _initialize(self):
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, digest TEXT NOT NULL, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
                id TEXT NOT NULL, seq INTEGER NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(id, seq));
        """)
        with self.db:
            for (identifier,) in self.db.execute(
                "SELECT id FROM tasks WHERE status IN ('queued','running')"
            ).fetchall():
                pending = set()
                for kind, raw in self.db.execute(
                    "SELECT type,data FROM events WHERE id=? ORDER BY seq",
                    (identifier,),
                ):
                    if kind == "approval_required":
                        pending.add(json.loads(raw)["id"])
                    elif kind == "approval_resolved":
                        pending.discard(json.loads(raw)["id"])
                for approval in pending:
                    self._append(
                        identifier,
                        "approval_resolved",
                        {"id": approval, "decision": "deny", "cause": "restart"},
                    )
                self._append(
                    identifier,
                    "log",
                    {"text": "Sidecar restarted; unfinished task interrupted"},
                )
                self._append(identifier, "status", {"status": "failed"})

    def get(self, identifier):
        return self.db.execute(
            "SELECT digest,status FROM tasks WHERE id=?", (identifier,)
        ).fetchone()

    def claim(self, identifier, digest):
        with self.db:
            self.db.execute(
                "INSERT INTO tasks VALUES(?,?,'queued')", (identifier, digest)
            )

    def _append(self, identifier, kind, data):
        self.db.execute(
            """INSERT INTO events(id,seq,type,data)
            SELECT ?,COALESCE(MAX(seq),0)+1,?,? FROM events WHERE id=?""",
            (identifier, kind, json.dumps(data, ensure_ascii=False), identifier),
        )
        if kind == "status":
            self.db.execute(
                "UPDATE tasks SET status=? WHERE id=?", (data["status"], identifier)
            )

    def append(self, identifier, kind, data):
        with self.db:
            self._append(identifier, kind, data)

    def events(self, identifier, after):
        rows = self.db.execute(
            "SELECT seq,type,data FROM events WHERE id=? AND seq>? ORDER BY seq LIMIT 256",
            (identifier, max(after, 0)),
        )
        return [
            {"seq": seq, "type": kind, "data": json.loads(raw)}
            for seq, kind, raw in rows
        ]

    def close(self):
        try:
            if self.db is not None:
                self.db.close()
                self.db = None
        finally:
            if self.lock:
                self.lock.close()
                self.lock = None
