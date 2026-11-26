import json
from urllib.parse import quote

import httpx
from fastapi import HTTPException
from pydantic import Field

from .engine import public_node
from .models import Fields


class CopyArtifact(Fields):
    source_volume: str = Field(min_length=1, max_length=80)
    source_path: str = Field(min_length=1, max_length=4096)
    target_node: str
    target_volume: str = Field(min_length=1, max_length=80)
    target_path: str = Field(min_length=1, max_length=4096)


def register_artifacts(app, store, engine):
    @app.post("/api/tasks/{task_id}/artifacts/copy")
    async def copy_artifact(task_id: str, data: CopyArtifact):
        task = await store.row("SELECT node_id FROM tasks WHERE id=?", task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        records = await store.rows(
            "SELECT data FROM events WHERE task_id=? AND type='artifact'", task_id
        )
        if not any(
            item.get("volume") == data.source_volume
            and item.get("path") == data.source_path
            for item in (json.loads(row["data"]) for row in records)
        ):
            raise HTTPException(404, "该任务未登记此产物")
        source = await engine.node(task["node_id"])
        target = await engine.node(data.target_node)
        owner = public_node(source)
        if not owner["enabled"]:
            raise HTTPException(409, "源节点已停用")
        # Old receivers ignore unknown query parameters; require explicit support before sending bytes.
        caps = (
            (await engine.request(target, "GET", "/health"))
            .json()
            .get("capabilities", [])
        )
        if "upload_create_only" not in caps:
            raise HTTPException(409, "目标节点需要支持防覆盖上传的 Sidecar")
        path = "/api/volumes/" + quote(data.source_volume, safe="") + "/download"
        limit = int(engine.config.get("max_upload_bytes", 64 * 1024 * 1024))
        try:
            async with engine.client.stream(
                "GET",
                owner["url"] + path,
                params={"path": data.source_path},
                headers={"Authorization": "Bearer " + source["secret"]},
                timeout=60,
            ) as response:
                if not response.is_success:
                    raise HTTPException(response.status_code, "源文件暂不可读")

                async def chunks():
                    count = 0
                    async for block in response.aiter_bytes():
                        count += len(block)
                        if count > limit:
                            raise HTTPException(413, "产物超过转存限制")
                        yield block

                result = await engine.request(
                    target,
                    "PUT",
                    "/api/volumes/" + quote(data.target_volume, safe="") + "/upload",
                    params={"path": data.target_path, "create_only": "true"},
                    content=chunks(),
                    timeout=120,
                )
                return result.json()
        except httpx.RequestError as exc:
            raise HTTPException(503, "文件传输中断") from exc
