"""Command-line client for authenticated Fleet Hub requests, using the standard library."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, ProxyHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="fleetctl", description="Fleet Hub 管理工具")
    parser.add_argument(
        "--hub", default=os.getenv("FLEET_HUB_URL", "http://127.0.0.1:8765")
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("nodes")
    dispatch = sub.add_parser("dispatch")
    dispatch.add_argument(
        "--mode", choices=["single", "auto", "fanout"], default="auto"
    )
    dispatch.add_argument("--node", action="append", default=[])
    dispatch.add_argument("--tag", action="append", default=[])
    dispatch.add_argument("--agent", default="dsh")
    dispatch.add_argument("--workspace", default="")
    dispatch.add_argument("--volume")
    dispatch.add_argument("--path", default="")
    dispatch.add_argument("prompt")
    sub.add_parser("status").add_argument("task")
    result = sub.add_parser("result")
    result.add_argument("task")
    result.add_argument("--wait", type=float, default=0, help="等待任务完成的最大秒数")
    sub.add_parser("cancel").add_argument("task")
    sub.add_parser("approvals")
    approve = sub.add_parser("approve")
    approve.add_argument("approval")
    approve.add_argument("--session-dir", action="store_true")
    sub.add_parser("deny").add_argument("approval")
    sub.add_parser("wake").add_argument("node")
    files = sub.add_parser("files")
    files.add_argument("node")
    files.add_argument("volume", nargs="?")
    files.add_argument("path", nargs="?", default="")
    files.add_argument(
        "--dispatch", metavar="PROMPT", help="派发任务，使用该卷文件或目录作为上下文"
    )
    files.add_argument("--agent", default="dsh")
    op = files.add_mutually_exclusive_group()
    op.add_argument("--search", metavar="QUERY")
    op.add_argument("--upload", metavar="LOCAL_FILE")
    op.add_argument("--download", metavar="NEW_LOCAL_FILE")
    op.add_argument("--preview", action="store_true")
    return parser.parse_args(argv)


def part(value):
    if value in {".", "..", ""}:
        raise ValueError("无效的资源名称")
    return quote(str(value), safe="")


def client(hub, secret):
    url = urlsplit(hub)
    if (
        url.scheme not in {"https", "http"}
        or not url.hostname
        or url.username
        or url.password
        or url.fragment
        or url.query
    ):
        raise ValueError("Hub URL 无效")
    if not secret:
        raise ValueError("请通过 FLEET_API_KEY 提供 CLI 密钥")
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(method, path, body=None, *, data=None, raw=False):
        headers = {"Authorization": f"Bearer {secret}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        elif data is not None:
            headers["Content-Type"] = "application/octet-stream"
        req = Request(hub.rstrip("/") + path, method=method, headers=headers, data=data)
        with opener.open(req, timeout=30) as response:
            content = response.read()
            if raw:
                return content
            if not content:
                return {}
            if "json" in response.headers.get("Content-Type", ""):
                return json.loads(content)
            return content.decode("utf-8", errors="replace")

    return request


def run(args, request):
    cmd = args.command
    if cmd in {"nodes", "approvals"}:
        return request("GET", f"/api/{cmd}")
    if cmd == "dispatch":
        payload = dict(
            mode=args.mode,
            node_ids=args.node,
            tags=args.tag,
            agent=args.agent,
            workspace=args.workspace,
            prompt=args.prompt,
        )
        if args.volume:
            payload["context"] = {"volume": args.volume, "path": args.path}
        return request("POST", "/api/dispatch", payload)
    if cmd in {"status", "result", "cancel"}:
        path = f"/api/tasks/{part(args.task)}"
        if cmd == "cancel":
            return request("POST", path + "/cancel", {})
        deadline = time.monotonic() + (args.wait if cmd == "result" else 0)
        while True:
            task = request("GET", path)
            if (
                cmd == "status"
                or task.get("status")
                in {
                    "completed",
                    "done",
                    "succeeded",
                    "success",
                    "cancelled",
                    "canceled",
                    "failed",
                    "denied",
                    "error",
                }
                or time.monotonic() >= deadline
            ):
                return task
            time.sleep(min(0.4, max(0, deadline - time.monotonic())))
    if cmd in {"approve", "deny"}:
        decision = (
            "deny"
            if cmd == "deny"
            else ("approve_session_dir" if args.session_dir else "approve_once")
        )
        return request(
            "POST", f"/api/approvals/{part(args.approval)}", {"decision": decision}
        )
    if cmd == "wake":
        return request("POST", f"/api/nodes/{part(args.node)}/wake", {})
    path = f"/api/nodes/{part(args.node)}/volumes"
    if not args.volume:
        if (
            args.path
            or args.search
            or args.upload
            or args.download
            or args.preview
            or args.dispatch
        ):
            raise ValueError("此操作需要指定卷名")
        return request("GET", path)
    path += "/" + part(args.volume)
    query = urlencode({"path": args.path})
    if args.dispatch and (args.search or args.download or args.preview):
        raise ValueError("--dispatch 可与 --upload 搭配，或直接指定已存在的上下文路径")
    if args.search:
        return request("GET", f"{path}/search?{query}&{urlencode({'q': args.search})}")
    if args.upload:
        if not args.path:
            raise ValueError("请指定目标卷中的文件路径")
        with Path(args.upload).open("rb") as stream:
            uploaded = request("PUT", f"{path}/upload?{query}", data=stream)
        if not args.dispatch:
            return uploaded
    if args.dispatch:
        return request(
            "POST",
            "/api/dispatch",
            {
                "mode": "single",
                "node_ids": [args.node],
                "agent": args.agent,
                "prompt": args.dispatch,
                "context": {"volume": args.volume, "path": args.path},
            },
        )
    if args.download:
        content = request("GET", f"{path}/download?{query}", raw=True)
        with Path(args.download).open("xb") as dest:
            dest.write(content)
        return {"downloaded": len(content)}
    return request("GET", f"{path}/{'preview' if args.preview else 'list'}?{query}")


def main(argv=None):
    args = parse_args(argv)
    try:
        result = run(args, client(args.hub, os.getenv("FLEET_API_KEY", "")))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except HTTPError as error:
        detail = error.read(8192).decode("utf-8", errors="replace")
        print(f"Fleet HTTP {error.code}: {detail}", file=sys.stderr)
    except (ValueError, OSError, URLError) as error:
        print(f"fleetctl: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
