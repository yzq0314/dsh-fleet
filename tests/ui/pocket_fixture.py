"""Browser acceptance with two paired Sidecars on distinct Pocket virtual hosts."""
import asyncio
from contextlib import AsyncExitStack
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse, PlainTextResponse

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT/"tests"), str(ROOT/"apps/fleet-hub"), str(ROOT/"apps/node-sidecar")]
from test_pocket_tunnel import serving, listener
from test_hub import config
from fleet_hub.app import create_app as hub
from fleet_node.app import create_app as sidecar


async def main():
    with tempfile.TemporaryDirectory() as temp:
        async with AsyncExitStack() as stack:
            sock = listener()
            port = sock.getsockname()[1]
            cfg = config(Path(temp))
            cfg.update(public_url=f"http://console.fleet.test:{port}",pocket_origins={},nodes=[],poll_interval=.3,
                       web_root=os.environ["FLEET_TEST_DIST"])
            for name in ("Alpha","Beta"):
                mock = FastAPI()
                @mock.get("/")
                async def page():
                    return HTMLResponse('<h1>DSH fixture</h1><output></output><script src="/static/app.js"></script>')
                @mock.get("/static/app.js")
                async def script():
                    return PlainTextResponse("fetch('/rpc/ping').then(r=>r.text()).then(v=>{const ws=new WebSocket('ws://'+location.host+'/ws/stream');ws.onopen=()=>ws.send(v);ws.onmessage=e=>document.querySelector('output').textContent=e.data;});",media_type="text/javascript")
                @mock.get("/rpc/ping")
                async def ping(text=name):
                    return PlainTextResponse(text)
                @mock.websocket("/ws/stream")
                async def stream(ws:WebSocket):
                    await ws.accept(); await ws.send_text(await ws.receive_text()); await ws.close()
                mp=await stack.enter_async_context(serving(mock))
                key=name.lower()
                dedicated=f"http://{key}.fleet.test:{port}"
                nc={"secret":cfg["api_key"]+name,"pocket_port":mp,"pocket_public_origin":dedicated,"trusted_workspaces":[temp]}
                np=await stack.enter_async_context(serving(sidecar(nc)))
                cfg["pocket_origins"][key]=dedicated
                cfg["nodes"].append(dict(id=key,alias=name,url=f"http://127.0.0.1:{np}",secret=nc["secret"]))
            await stack.enter_async_context(serving(hub(cfg),sock))
            await asyncio.to_thread(subprocess.run,["node",str(ROOT/"tests/ui/pocket.mjs")],
                env={**os.environ,"FLEET_FIXTURE_PORT":str(port),"FLEET_FIXTURE_PASSWORD":cfg["password"]},check=True)


if __name__=="__main__":
    asyncio.run(main())
