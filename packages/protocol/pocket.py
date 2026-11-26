"""Fixed-destination HTTP and WebSocket forwarding for the two Fleet hops."""
import asyncio
from http.cookies import SimpleCookie, CookieError
from urllib.parse import quote, urlsplit

import httpx
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from httpx_ws import aconnect_ws, WebSocketDisconnect, WebSocketUpgradeError
from starlette.background import BackgroundTask
from wsproto.events import TextMessage, BytesMessage

REQUEST = {"accept", "accept-encoding", "accept-language", "content-type", "range",
           "if-none-match", "if-modified-since", "origin"}
RESPONSE = {"content-type", "content-encoding", "content-language", "content-length",
            "content-range", "accept-ranges", "etag", "last-modified", "vary",
            "content-disposition", "x-frame-options", "content-security-policy",
            "content-security-policy-report-only", "permissions-policy"}


def destination(base, prefix, path, query):
    # Construct the path component independently of the authority. Reject ambiguous
    # separators, traversal and encoded paths that would be decoded again downstream.
    if (path.startswith("/") or any(c in path for c in ("\\", "%", "\x00"))
            or any(seg in (".", "..") for seg in path.split("/"))):
        raise HTTPException(400, "无效的 Pocket 路径")
    return httpx.URL(base).copy_with(path=prefix + quote(path, safe="/:@!$&'()*+,;=-._~"), query=query)


def headers_for(scope, extra):
    headers = {k: v for k,v in scope.headers.items() if k.lower() in REQUEST}
    cookie = "; ".join(k+"="+v for k,v in scope.cookies.items()
                       if not k.lower().startswith(("fleet_", "__host-fleet")) and not any(c in v for c in "\r\n;"))
    if cookie:
        headers["cookie"] = cookie
    return {**headers, **extra}


def response_headers(upstream, response):
    for name, value in upstream.headers.multi_items():
        if name in RESPONSE:
            response.raw_headers.append((name.encode("ascii"), value.encode("latin1")))
        elif name == "location":
            parts = urlsplit(value)
            if not parts.scheme and not parts.netloc and not value.startswith("//") and "\\" not in value:
                response.headers["location"] = value
        elif name == "set-cookie":
            jar = SimpleCookie()
            try:
                jar.load(value)
            except CookieError:
                continue
            for cookie in jar.values():
                if not cookie.key.lower().startswith(("fleet_", "__host-fleet")):
                    cookie["domain"] = ""
                    response.raw_headers.append((b"set-cookie", cookie.OutputString().encode("latin1")))
    response.headers["cache-control"] = "no-store"
    response.headers["referrer-policy"] = "no-referrer"
    response.headers["x-content-type-options"] = "nosniff"


async def http_tunnel(request, url, extra):
    client = httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=httpx.Timeout(45, connect=4))
    upstream = None
    async def close():
        if upstream is not None:
            await upstream.aclose()
        await client.aclose()
    try:
        data = request.stream() if request.method not in ("HEAD","GET") else None
        req = client.build_request(request.method, url, headers=headers_for(request, extra), content=data)
        upstream = await client.send(req, stream=True)
        response = StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code, background=BackgroundTask(close))
        response_headers(upstream, response)
        return response
    except httpx.HTTPError as err:
        await close()
        raise HTTPException(502, "Pocket 上游未响应") from err
    except BaseException:
        await close()
        raise


async def ws_tunnel(ws, url, extra, valid):
    protocols = ws.scope.get("subprotocols", [])
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=8) as client:
            async with aconnect_ws(str(url), client=client, headers=headers_for(ws, extra),
                                   subprotocols=protocols, max_message_size_bytes=4*1024*1024, queue_size=16) as conn:
                await ws.accept(subprotocol=conn.subprotocol)
                async def outgoing():
                    while True:
                        msg = await ws.receive()
                        if msg["type"] == "websocket.disconnect":
                            return
                        if msg.get("text") is not None:
                            await conn.send_text(msg["text"])
                        else:
                            await conn.send_bytes(msg["bytes"])
                async def incoming():
                    while True:
                        event = await conn.receive()
                        if isinstance(event, TextMessage):
                            await ws.send_text(event.data)
                        elif isinstance(event, BytesMessage):
                            await ws.send_bytes(event.data)
                async def check():
                    while await valid():
                        await asyncio.sleep(5)
                tasks = [asyncio.create_task(f()) for f in (incoming, outgoing, check)]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
    except (WebSocketDisconnect, WebSocketUpgradeError, httpx.HTTPError, OSError):
        pass
    finally:
        if ws.application_state.name != "DISCONNECTED":
            try:
                await ws.close()
            except (RuntimeError, OSError):
                pass
