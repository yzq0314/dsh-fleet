"""Exercise both transports on three actual loopback listeners."""
import asyncio
from contextlib import asynccontextmanager
import secrets
import socket

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, PlainTextResponse
import httpx
from httpx_ws import aconnect_ws
import pytest
import uvicorn

from fleet_hub.app import create_app as hub
from fleet_node.app import create_app as sidecar
from packages.protocol.pocket import destination
from fastapi import HTTPException
from test_hub import config


@asynccontextmanager
async def serving(app, sock=None):
    sock = sock or listener()
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False, ws="wsproto"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(300):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(.01)
        assert server.started
        yield sock.getsockname()[1]
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 8)
        sock.close()


def listener():
    sock = socket.socket()
    sock.bind(("127.0.0.1",0))
    sock.listen()
    return sock


@pytest.mark.parametrize("path",["//other.test/a","../api","x/../y",r"a\b","%2f%2foutside"])
def test_fixed_destination(path):
    with pytest.raises(HTTPException):
        destination("http://127.0.0.1:13081","/",path,b"")


async def test_root_assets_rpc_ws_auth_and_logout(tmp_path):
    mock = FastAPI()
    hub_socket = listener()
    hp = hub_socket.getsockname()[1]
    alias = "screen.fleet.test:"+str(hp)
    origin = "http://"+alias
    public = "http://console.fleet.test:"+str(hp)
    cfg = config(tmp_path)
    agent_token = secrets.token_urlsafe()
    @mock.get("/")
    async def index():
        return HTMLResponse('<script src="/static/app.js"></script>',headers={"Content-Security-Policy":"frame-ancestors 'self'"})
    @mock.get("/static/app.js")
    async def asset():
        return PlainTextResponse("const endpoint='/rpc/ping'")
    @mock.post("/rpc/ping")
    async def rpc(request: Request):
        assert not request.headers.get("authorization")
        assert not request.headers.get("cookie")
        assert request.headers["host"] == alias
        assert request.headers["origin"] == origin
        return {"echo": (await request.body()).decode()}
    @mock.websocket("/ws/stream")
    async def stream(ws: WebSocket):
        assert ws.headers.get("authorization") is None
        assert ws.headers["host"] == alias and ws.headers["origin"] == origin
        await ws.accept()
        await ws.send_text(await ws.receive_text())
        await ws.send_bytes(await ws.receive_bytes())
        await ws.close()

    async with serving(mock) as mp:
        node_config = {"secret":agent_token,"trusted_workspaces":[str(tmp_path)],"pocket_port":mp,"pocket_public_origin":origin}
        async with serving(sidecar(node_config)) as np:
            cfg.update(public_url=public,pocket_origins={"worker":origin},nodes=[{"id":"worker","secret":agent_token,"url":f"http://127.0.0.1:{np}","alias":"Worker"}])
            async with serving(hub(cfg),hub_socket):
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{hp}",trust_env=False,timeout=10) as c:
                    fleet = {"host":public.removeprefix("http://")}
                    assert (await c.get("/pocket/worker/",headers=fleet)).status_code==401
                    login=await c.post("/api/login",json={"password":cfg["password"]},headers=fleet)
                    assert login.is_success
                    launch=await c.post("/api/pocket/worker/entry",headers=fleet)
                    entry=httpx.URL(launch.json()["entry"])
                    pocket={"host":alias}
                    assert (await c.get(entry.path,headers=pocket)).status_code==303
                    assert (await c.get(entry.path,headers=pocket)).status_code==403
                    page=await c.get("/",headers=pocket)
                    assert 'src="/static/app.js"' in page.text and page.headers["content-security-policy"]=="frame-ancestors 'self'"
                    assert (await c.get("/static/app.js",headers=pocket)).is_success
                    assert (await c.post("/rpc/ping",content=b"ping",headers={**pocket,"origin":origin})).json()=={"echo":"ping"}
                    assert (await c.post("/rpc/ping",headers={**pocket,"origin":public})).status_code==403
                    assert (await c.get("/api/nodes",headers=fleet)).is_success
                    assert (await c.get("/api/nodes",headers=pocket)).status_code==404
                    async with aconnect_ws(f"http://127.0.0.1:{hp}/ws/stream",client=c,headers={**pocket,"origin":origin}) as ws:
                        await ws.send_text("hello")
                        assert await ws.receive_text(timeout=5)=="hello"
                        await ws.send_bytes(b"world")
                        assert await ws.receive_bytes(timeout=5)==b"world"
                    assert (await c.post("/api/logout",headers=fleet)).is_success
                    assert (await c.get("/static/app.js",headers=pocket)).status_code==401
