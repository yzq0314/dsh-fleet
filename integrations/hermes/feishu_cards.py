"""Mirror Fleet approval events to shared Feishu cards. No inbound approval endpoint."""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


TOKEN_URL = "https://accounts.feishu.cn/oauth/v3/token"
MESSAGES = "https://open.feishu.cn/open-apis/im/v1/messages"


def compact(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UnconfirmedSend(RuntimeError):
    pass


class Feishu:
    def __init__(self, env):
        names = (
            "FLEET_FEISHU_APP_ID",
            "FLEET_FEISHU_APP_SECRET",
            "FLEET_FEISHU_CHAT_ID",
            "FLEET_CONSOLE_URL",
        )
        if any(not env.get(key, "").strip() for key in names):
            raise ValueError("Required private settings: " + ", ".join(names))
        self.app, self.secret, self.chat, self.hub = (env[key] for key in names)
        url = urlsplit(self.hub)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "FLEET_CONSOLE_URL requires a trusted HTTPS URL without credentials"
            )
        self.identity = hashlib.sha256(
            compact([self.app, self.chat, self.hub]).encode()
        ).hexdigest()
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.token, self.expiry = "", 0

    def request(self, method, url, data, token=None):
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = Request(
            url, data=compact(data).encode(), method=method, headers=headers
        )
        try:
            with self.opener.open(request, timeout=12) as response:
                result = json.load(response)
        except HTTPError as error:
            if error.code in {400, 401, 403}:
                self.expiry = 0
            raise
        if result.get("code", 0) != 0:
            self.expiry = 0
            raise RuntimeError("Feishu API returned code " + str(result["code"]))
        return result

    def send(self, approval, card, message_id=None):
        if time.monotonic() >= self.expiry:
            auth = self.request(
                "POST",
                TOKEN_URL,
                {
                    "grant_type": "client_credentials",
                    "client_id": self.app,
                    "client_secret": self.secret,
                    "scope": "im:message:send_as_bot",
                },
            )
            self.token = auth["access_token"]
            self.expiry = time.monotonic() + max(0, int(auth["expires_in"]) - 60)
        data = {"content": compact(card)}
        if message_id:
            self.request(
                "PATCH", MESSAGES + "/" + quote(message_id, safe=""), data, self.token
            )
            return message_id
        key = str(uuid.uuid5(uuid.NAMESPACE_URL, self.identity + ":" + approval))
        data.update(msg_type="interactive", receive_id=self.chat, uuid=key)
        result = self.request(
            "POST", MESSAGES + "?receive_id_type=chat_id", data, self.token
        )
        return result["data"]["message_id"]


def card_for(approval, detail, state, hub):
    title = {
        "pending": "待审批",
        "approve_once": "已允许一次",
        "approve_session_dir": "已信任本次会话目录",
        "deny": "已拒绝",
        "timeout": "已超时拒绝",
        "cancelled": "已取消",
        "closed": "任务已结束，审批已关闭",
        "failed": "任务失败，审批已关闭",
    }.get(state, "已结束")

    def plain(text):
        return {"tag": "plain_text", "content": text}

    lines = [
        f"审批 ID  {approval}",
        "节点  " + detail.get("node", ""),
        "原因  " + detail.get("reason", ""),
        "工作目录  " + detail.get("workspace", ""),
        "目标  " + "、".join(detail.get("paths", [])),
        "操作\n" + detail.get("command", ""),
    ]
    body = {"tag": "div", "text": plain("\n".join(lines))}
    link = {
        "tag": "action",
        "actions": [{"tag": "button", "text": plain("打开 Fleet 控制台"), "url": hub}],
    }
    card = {
        "config": {"update_multi": True},
        "header": {
            "template": "orange" if state == "pending" else "grey",
            "title": plain("Fleet · " + title),
        },
        "elements": [body, link],
    }
    if len(compact(card).encode()) > 28_000:
        body["text"] = plain(
            f"审批 ID  {approval}\n完整命令和路径超出飞书单卡大小限制，请在 Fleet 控制台核对。"
        )
    return card


class Bridge:
    def __init__(self, path, api):
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA locking_mode=EXCLUSIVE")
        self.api = api
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS cards (approval TEXT PRIMARY KEY, seq INTEGER, message TEXT, state TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS reader (id INTEGER PRIMARY KEY, site TEXT, file TEXT, pos INTEGER);
            CREATE TABLE IF NOT EXISTS attempts (approval TEXT PRIMARY KEY, started REAL);
        """)
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO reader VALUES (1,?,'',0)", (api.identity,)
            )
            if self.db.execute("SELECT site FROM reader").fetchone()[0] != api.identity:
                raise ValueError(
                    "This state database belongs to a different application, chat or Hub"
                )

    def process(self, event):
        kind = event["type"]
        if kind not in {"approval_required", "approval_resolved"}:
            return
        data = event["data"]
        key = data["id"]
        seq = int(event["id"])
        row = self.db.execute(
            "SELECT seq,message,state,detail FROM cards WHERE approval=?", (key,)
        ).fetchone()
        if row and (seq <= row[0] or row[2] != "pending"):
            return
        detail = json.loads(row[3]) if row else {}
        message = row[1] if row else None
        if kind == "approval_required":
            detail = dict(data, node=event["node_id"])
            state = "pending"
        else:
            state = (
                data.get("cause")
                if data.get("cause") in {"timeout", "cancelled", "failed"}
                else data.get("decision", "closed")
            )
        if message or kind == "approval_required":
            if not message:
                # Persist intent before POST. After Feishu's one-hour idempotency
                # window an ambiguous send requires manual reconciliation.
                self.db.execute(
                    "INSERT OR IGNORE INTO attempts VALUES (?,?)", (key, time.time())
                )
                self.db.commit()
                started = self.db.execute(
                    "SELECT started FROM attempts WHERE approval=?", (key,)
                ).fetchone()[0]
                if time.time() - started > 3500:
                    raise UnconfirmedSend(
                        "Unconfirmed card exceeds the Feishu idempotency window; check approval "
                        + repr(key)
                    )
            message = self.api.send(
                key, card_for(key, detail, state, self.api.hub), message
            )
        self.db.execute(
            "INSERT OR REPLACE INTO cards VALUES (?,?,?,?,?)",
            (key, seq, message, state, compact(detail)),
        )
        self.db.execute("DELETE FROM attempts WHERE approval=?", (key,))

    def drain(self, events):
        with Path(events).open("rb") as stream:
            stat = os.fstat(stream.fileno())
            identity = f"{stat.st_dev}:{stat.st_ino}"
            previous, pos = self.db.execute("SELECT file,pos FROM reader").fetchone()
            if identity == previous and pos <= stat.st_size:
                stream.seek(pos)
            while line := stream.readline():
                if not line.endswith(b"\n"):
                    break
                with self.db:
                    self.process(json.loads(line))
                    self.db.execute(
                        "UPDATE reader SET file=?,pos=? WHERE id=1",
                        (identity, stream.tell()),
                    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    try:
        api = Feishu(os.environ)
        bridge = Bridge(args.state, api)
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(2, str(error) + "\n")
    try:
        delay = 1
        while True:
            try:
                bridge.drain(args.events)
                delay = 1
            except UnconfirmedSend as error:
                print(error, file=sys.stderr)
                return 1
            except (
                HTTPError,
                URLError,
                OSError,
                ValueError,
                RuntimeError,
                KeyError,
                sqlite3.Error,
            ) as error:
                # Error bodies may contain private card text. Only log a safe class/status.
                print(
                    "Card delivery pending:",
                    type(error).__name__,
                    getattr(error, "code", ""),
                    file=sys.stderr,
                )
                if args.once:
                    return 1
                delay = min(60, delay * 2)
            if args.once:
                return 0
            time.sleep(delay)
    finally:
        bridge.db.close()


if __name__ == "__main__":
    raise SystemExit(main())
