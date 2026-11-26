"""Each optional Pocket virtual host shares the Hub socket and has its own cookie jar."""
import hmac
import json
import secrets
import time
from ipaddress import ip_address
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.routing import Router, Route, WebSocketRoute

from packages.protocol.network import allowed_peer
from packages.protocol.pocket import destination, http_tunnel, ws_tunnel
from .auth import digest


def install(app, config, engine, security):
    origins = config.get("pocket_origins") or {}
    hosts = {}
    for node_id, origin in origins.items():
        parts = urlsplit(origin)
        fleet = urlsplit(security.public_origin)
        if (not fleet.hostname or parts.scheme != fleet.scheme or parts.port != fleet.port or
            not parts.hostname or parts.hostname == fleet.hostname or "." not in parts.hostname or
            parts.hostname.endswith(".localhost") or parts.path or parts.query or parts.fragment or
            parts.username or parts.password or parts.netloc in hosts or origin != origin.lower()):
            raise ValueError("Pocket requires a distinct DNS name on the same Hub listener")
        try:
            ip_address(parts.hostname)
        except ValueError:
            pass
        else:
            raise ValueError("Pocket virtual host must be a DNS name")
        hosts[parts.netloc] = node_id
    tickets = {}
    signing_key = secrets.token_bytes(32)

    def sign(node, sid):
        return hmac.digest(signing_key, (node + "/" + sid).encode(), "sha256").hex()

    @app.get("/pocket/{node_id}/{path:path}")
    @app.post("/api/pocket/{node_id}/entry")
    @app.get("/pocket/{node_id}/")
    async def launch(node_id: str, request: Request, path: str = ""):
        if not await security.valid(request) or not request.cookies.get("fleet_session"):
            raise HTTPException(401, "请登录 Fleet")
        if node_id not in origins:
            raise HTTPException(409, "此节点尚未配置独立同屏地址")
        row = await engine.node(node_id)
        if not json.loads(row["doc"]).get("enabled", True):
            raise HTTPException(409, "节点已停用")
        now = time.monotonic()
        for k in list(tickets):
            if tickets[k][0] <= now:
                del tickets[k]
        if len(tickets) >= 256:
            raise HTTPException(429, "请稍后重试")
        ticket = secrets.token_urlsafe(24)
        target = destination(origins[node_id], "/", path, request.scope["query_string"] if path else b"")
        landing = target.raw_path.decode("ascii")
        tickets[ticket] = (now + 20, node_id, digest(request.cookies["fleet_session"]), landing)
        entry_url = origins[node_id] + "/.fleet/enter/" + ticket
        if request.method == "POST":
            return {"entry":entry_url}
        return RedirectResponse(entry_url, status_code=303,
                                headers={"cache-control":"no-store","referrer-policy":"no-referrer"})

    async def authorized(scope):
        node = hosts[scope.headers["host"].lower()]
        token = scope.cookies.get("fleet_pocket", "")
        sid, _, mac = token.partition(".")
        if len(sid) != 64 or not sid.isascii() or not hmac.compare_digest(mac.encode(), sign(node, sid).encode()):
            return None
        if not await engine.store.row("SELECT 1 FROM sessions WHERE digest=? AND expires>?", sid, time.time()):
            return None
        row = await engine.store.row("SELECT * FROM nodes WHERE id=?", node)
        return row if row and json.loads(row["doc"]).get("enabled", True) else None

    def proxy(scope,row):
        base = json.loads(row["doc"])["url"]
        url = destination(base, "/pocket/proxy/", scope.path_params["path"], scope.scope["query_string"])
        return url, {"authorization": "Bearer " + row["secret"], "x-fleet-pocket-origin":origins[row["id"]]}

    async def serve(request):
        node = hosts[request.headers["host"].lower()]
        path = request.path_params["path"]
        if path.startswith(".fleet/enter/") and request.method == "GET":
            entry = tickets.pop(path.removeprefix(".fleet/enter/"), None)
            if not entry or entry[0] < time.monotonic() or entry[1] != node:
                return JSONResponse({"detail":"同屏链接已过期"},403)
            sid = entry[2]
            res = RedirectResponse(entry[3],303,headers={"cache-control":"no-store","referrer-policy":"no-referrer"})
            res.set_cookie("fleet_pocket", sid+"."+sign(node,sid), httponly=True,
                           secure=origins[node].startswith("https://"),samesite="lax")
            return res
        row = await authorized(request)
        if not row:
            return JSONResponse({"detail":"请从 Fleet 重新进入同屏"},401)
        origin = request.headers.get("origin")
        if (origin and origin != origins[node]) or (request.method not in ("GET","HEAD","OPTIONS") and origin != origins[node]):
            return JSONResponse({"detail":"请求来源不匹配"},403)
        try:
            url, extra = proxy(request,row)
            return await http_tunnel(request,url,extra)
        except HTTPException as exc:
            return JSONResponse({"detail":exc.detail},exc.status_code)

    async def socket(ws):
        row = await authorized(ws)
        if not row or ws.headers.get("origin") != origins[row["id"]]:
            await ws.close(code=1008); return
        try:
            url, extra = proxy(ws,row)
        except HTTPException:
            await ws.close(code=1008); return
        await ws_tunnel(ws,url,extra,lambda: authorized(ws))

    router = Router(routes=[Route("/{path:path}",serve,methods=["GET","HEAD","OPTIONS","POST","PUT","PATCH","DELETE"]),
                            WebSocketRoute("/{path:path}",socket)])

    class VirtualHosts:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] in ("http","websocket"):
                host = dict(scope["headers"]).get(b"host", b"").decode("latin1").lower()
                if host in hosts:
                    peer = scope.get("client")
                    if not peer or not allowed_peer(peer[0], config["allowed_cidrs"]):
                        if scope["type"] == "websocket":
                            await send({"type":"websocket.close","code":1008})
                        else:
                            await JSONResponse({"detail":"来源不在允许的网段"},403)(scope,receive,send)
                        return
                    await router(scope,receive,send)
                    return
            await self.app(scope,receive,send)
    app.add_middleware(VirtualHosts)
