from urllib.parse import urlsplit
import hmac
import time

import httpx
from fastapi import HTTPException, Request, WebSocket

from packages.protocol.network import allowed_peer
from packages.protocol.pocket import destination, http_tunnel, ws_tunnel
from .pocket import probe_pocket


def install(app, node, config):
    async def upstream(scope, path):
        origin = config.get("pocket_public_origin", "").rstrip("/")
        parts = urlsplit(origin)
        if not parts.hostname or parts.scheme not in ("http","https") or parts.username or parts.password or parts.path or parts.query or parts.fragment:
            raise HTTPException(409, "请在节点设置 Pocket 专用访问来源")
        if (parts.hostname == "localhost" or parts.hostname.endswith(".localhost")
                or "." not in parts.hostname or parts.hostname.replace(".", "").isdigit()
                or ":" in parts.hostname):
            raise HTTPException(409, "Pocket 需要独立的内网主机名")
        if scope.headers.get("x-fleet-pocket-origin") != origin:
            raise HTTPException(403, "Pocket 访问地址尚未配对")
        if scope.headers.get("origin") not in (None, origin):
            raise HTTPException(403, "Pocket 来源不匹配")
        # Reuse the most recent successful health scan during asset bursts.
        info = node.probe or {}
        if "pocket_status" not in info or time.monotonic() - node.probe_time > 5:
            info = await probe_pocket(config)
        if info["pocket_status"] != "ready":
            raise HTTPException(503, "Pocket 尚未就绪")
        target = destination("http://127.0.0.1:" + str(info["pocket_port"]), "/", path, scope.scope["query_string"])
        return target, {"host": parts.netloc}

    @app.api_route("/pocket/proxy/{path:path}", methods=["GET","HEAD","POST","PUT","PATCH","DELETE","OPTIONS"])
    async def forward(path: str, request: Request):
        url, extra = await upstream(request, path)
        return await http_tunnel(request, url, extra)

    @app.websocket("/pocket/proxy/{path:path}")
    async def socket(ws: WebSocket, path: str):
        async def valid():
            if not ws.client or not allowed_peer(ws.client.host, node.cidrs):
                return False
            auth = ws.headers.get("authorization", "")
            if not hmac.compare_digest(auth.encode(), ("Bearer " + node.secret).encode()):
                return False
            if not config.get("hub_url"):
                return True
            try:
                async with httpx.AsyncClient(trust_env=False, timeout=4) as client:
                    r = await client.post(config["hub_url"].rstrip("/") + "/api/node/verify",
                        headers={"authorization": auth}, json={"id": config["node_id"]})
                    return r.is_success and r.json().get("valid") is True
            except (httpx.HTTPError, ValueError):
                return False
        if not await valid():
            await ws.close(code=1008); return
        try:
            url, extra = await upstream(ws, path)
        except HTTPException:
            await ws.close(code=1008); return
        await ws_tunnel(ws, url, extra, valid)
