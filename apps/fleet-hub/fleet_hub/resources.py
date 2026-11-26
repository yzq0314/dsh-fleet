import json
from urllib.parse import quote

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from starlette.background import BackgroundTask

from .engine import public_node
from .models import Macro, Preferences, Service, uid
from .store import dump, one


def validated(model, data):
    try:
        return model.model_validate(data).model_dump(mode="json")
    except ValidationError as exc:
        raise HTTPException(
            422, str(exc.errors(include_input=False, include_url=False)[0]["msg"])
        ) from exc


def register_resources(app, store, engine):
    def collection(table, model):
        # Table names are the fixed literals passed below.
        base = "/api/" + table

        @app.get(base, name="list_" + table)
        async def list_items():
            values = [
                {"id": row["id"], **json.loads(row["doc"])}
                for row in await store.rows(f"SELECT * FROM {table} ORDER BY rowid")
            ]
            if table == "macros":
                values.sort(key=lambda x: x["position"])
            else:
                for item in values:
                    item["online"] = engine.service_health.get(item["id"])
            return values

        @app.post(base, name="create_" + table)
        async def create(data: dict):
            item = validated(model, data)
            key = uid()
            async with store.transaction() as db:
                await db.execute(f"INSERT INTO {table} VALUES (?,?)", (key, dump(item)))
            return {"id": key, **item}

        @app.patch(base + "/{key}", name="edit_" + table)
        async def edit(key: str, data: dict):
            async with store.transaction() as db:
                row = await one(db, f"SELECT doc FROM {table} WHERE id=?", key)
                if not row:
                    raise HTTPException(404, "记录不存在")
                item = validated(model, {**json.loads(row["doc"]), **data})
                await db.execute(
                    f"UPDATE {table} SET doc=? WHERE id=?", (dump(item), key)
                )
            return {"id": key, **item}

        @app.delete(base + "/{key}", name="remove_" + table)
        async def remove(key: str):
            async with store.transaction() as db:
                cursor = await db.execute(f"DELETE FROM {table} WHERE id=?", (key,))
                if not cursor.rowcount:
                    raise HTTPException(404, "记录不存在")
            return {"removed": True}

        return list_items

    macros = collection("macros", Macro)
    services = collection("services", Service)

    @app.get("/api/settings")
    async def get_settings():
        return json.loads(
            (await store.row("SELECT value FROM metadata WHERE key='settings'"))[
                "value"
            ]
        )

    @app.patch("/api/settings")
    async def settings(data: dict):
        async with store.transaction() as db:
            row = await one(db, "SELECT value FROM metadata WHERE key='settings'")
            value = validated(Preferences, {**json.loads(row["value"]), **data})
            await db.execute(
                "UPDATE metadata SET value=? WHERE key='settings'", (dump(value),)
            )
        return value

    @app.get("/api/nodes/{node_id}/policy")
    async def policy(node_id: str):
        return (
            await engine.request(await engine.node(node_id), "GET", "/api/policy")
        ).json()

    @app.put("/api/nodes/{node_id}/policy")
    async def set_policy(node_id: str, data: dict):
        return (
            await engine.request(
                await engine.node(node_id), "PUT", "/api/policy", json=data
            )
        ).json()

    @app.get("/api/nodes/{node_id}/volumes")
    async def volumes(node_id: str):
        return (
            await engine.request(await engine.node(node_id), "GET", "/api/volumes")
        ).json()

    @app.put("/api/nodes/{node_id}/volumes/{name}")
    async def register_volume(node_id: str, name: str, data: dict):
        return (
            await engine.request(
                await engine.node(node_id),
                "PUT",
                "/api/volumes/" + quote(name, safe=""),
                json=data,
            )
        ).json()

    @app.delete("/api/nodes/{node_id}/volumes/{name}")
    async def detach_volume(node_id: str, name: str):
        return (
            await engine.request(
                await engine.node(node_id),
                "DELETE",
                "/api/volumes/" + quote(name, safe=""),
            )
        ).json()

    @app.api_route(
        "/api/nodes/{node_id}/volumes/{name}/{action}", methods=["GET", "PUT"]
    )
    async def files(node_id: str, name: str, action: str, request: Request):
        if action not in {"search", "list", "download", "preview", "upload"} or (
            request.method == "PUT"
        ) != (action == "upload"):
            raise HTTPException(405, "卷操作无效")
        row = await engine.node(node_id)
        path = "/api/volumes/" + quote(name, safe="") + "/" + action
        params = dict(request.query_params)
        if action == "upload":

            async def body():
                total = 0
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > int(
                        engine.config.get("max_upload_bytes", 64 * 1024 * 1024)
                    ):
                        raise HTTPException(413, "文件超过上传限制")
                    yield chunk

            return (
                await engine.request(
                    row, "PUT", path, params=params, content=body(), timeout=120
                )
            ).json()
        if action in {"search", "list"}:
            return (await engine.request(row, "GET", path, params=params)).json()
        node = public_node(row)
        if not node["enabled"]:
            raise HTTPException(409, "节点已停用")
        req = engine.client.build_request(
            "GET",
            node["url"] + path,
            params=params,
            headers={"Authorization": "Bearer " + row["secret"]},
            timeout=60,
        )
        try:
            upstream = await engine.client.send(req, stream=True)
        except httpx.RequestError as exc:
            raise HTTPException(503, "节点暂时不可达") from exc
        if not upstream.is_success:
            await upstream.aclose()
            raise HTTPException(upstream.status_code, "无法读取卷文件")
        mime = (
            upstream.headers.get("content-type", "application/octet-stream")
            .split(";")[0]
            .lower()
        )
        safe = {
            "text/plain",
            "text/csv",
            "application/json",
            "image/png",
            "image/jpeg",
            "image/gif",
            "image/webp",
            "image/svg+xml",
        }
        headers = {
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "X-Content-Type-Options": "nosniff",
        }
        if action == "download" or mime not in safe:
            filename = (
                params.get("path", "").replace("\\", "/").rsplit("/", 1)[-1]
                or "download"
            )
            headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(
                filename, safe=""
            )
            mime = "application/octet-stream"
        return StreamingResponse(
            upstream.aiter_bytes(),
            media_type=mime,
            headers=headers,
            background=BackgroundTask(upstream.aclose),
        )

    return services, macros, get_settings
