"""Administrator appearance shared by authenticated consoles."""
import json
import re
from typing import Annotated, Literal

from fastapi import HTTPException, Request, Response
from pydantic import Field, field_validator

from .models import Fields
from .store import dump

Block = Literal["overview", "topology", "devices", "hardware", "services"]
BLOCKS = ["overview", "topology", "devices", "hardware", "services"]
Key = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[\w.-]+$")]
Shortcut = Annotated[str, Field(pattern=r"^(workbench|(?:service|volume|macro):[\w.-]{1,100})$")]


class Layout(Fields):
    desktop: list[Block] = Field(default_factory=lambda: BLOCKS.copy(), max_length=5)
    mobile: list[Block] = Field(default_factory=lambda: BLOCKS.copy(), max_length=5)

    @field_validator("desktop", "mobile")
    @classmethod
    def unique(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("Duplicate layout section")
        return value


class Appearance(Fields):
    version: Literal[1] = 1
    theme: Literal["industrial", "light", "midnight", "oled", "glass", "paper"] = "industrial"
    accent: str = Field(default="#a5ecc4", pattern=r"^#[0-9a-fA-F]{6}$")
    radius: Literal[4, 10, 16] = 10
    density: Literal["compact", "comfortable"] = "comfortable"
    font: Literal["system", "mono"] = "system"
    motion: bool = True
    glow: bool = True
    blur: int = Field(default=12, ge=0, le=32)
    shade: int = Field(default=50, ge=0, le=90)
    wallpaper: bool = False
    css: str = Field(default="", max_length=512)
    layout: Layout = Field(default_factory=Layout)
    shortcuts: dict[Key, Annotated[list[Shortcut], Field(min_length=1, max_length=2)]] = Field(default_factory=dict, max_length=256)

    @field_validator("css")
    @classmethod
    def controlled_declarations(cls, value):
        for line in value.split(";"):
            if line.strip() and not re.fullmatch(r"\s*--(?:panel|layer|line|text|mute)\s*:\s*#[0-9a-fA-F]{6}\s*", line):
                raise ValueError("Use palette variables and six-digit hex colors")
        return value


def register_appearance(app, store):
    @app.get("/api/ui-preferences")
    async def read_preferences():
        row = await store.row("SELECT doc FROM ui_preferences WHERE id=1")
        if row:
            return json.loads(row["doc"])
        old = json.loads((await store.row("SELECT value FROM metadata WHERE key='settings'"))["value"])
        theme = old.get("theme", "industrial")
        accent = {"mint": "#a5ecc4", "amber": "#f4c382", "blue": "#9eceff"}.get(old.get("accent"), "#a5ecc4")
        return Appearance(theme=theme if theme in ("industrial","light","midnight") else "industrial", accent=accent).model_dump()

    @app.put("/api/ui-preferences")
    async def save_preferences(value: Appearance):
        async with store.transaction() as db:
            await db.execute("INSERT OR REPLACE INTO ui_preferences VALUES (1,?)", (dump(value.model_dump()),))
        return value

    @app.put("/api/ui-preferences/wallpaper")
    async def upload(request: Request):
        mime = request.headers.get("content-type", "")
        if mime not in ("image/png", "image/jpeg", "image/webp"):
            raise HTTPException(415, "仅支持 PNG、JPEG 和 WebP")
        image = bytearray()
        async for chunk in request.stream():
            if len(image) + len(chunk) > 2 * 1024**2:
                raise HTTPException(413, "壁纸不能超过 2 MiB")
            image.extend(chunk)
        valid = {
            "image/png": image.startswith(b"\x89PNG\r\n\x1a\n") and image[12:16]==b"IHDR" and image[-8:-4]==b"IEND",
            "image/jpeg": image.startswith(b"\xff\xd8\xff") and image.endswith(b"\xff\xd9"),
            "image/webp": image[:4]==b"RIFF" and image[8:12]==b"WEBP" and image[12:16] in (b"VP8 ",b"VP8L",b"VP8X") and int.from_bytes(image[4:8],"little")+8==len(image),
        }
        if not valid[mime]:
            raise HTTPException(415, "图像格式与内容不符")
        async with store.transaction() as db:
            await db.execute("INSERT OR REPLACE INTO ui_wallpaper VALUES (1,?,?)",(mime,bytes(image)))
        return {"ok": True}

    @app.get("/api/ui-preferences/wallpaper")
    async def wallpaper():
        row = await store.row("SELECT mime,image FROM ui_wallpaper WHERE id=1")
        if not row:
            raise HTTPException(404, "尚未上传壁纸")
        return Response(content=row["image"], media_type=row["mime"], headers={"Cache-Control":"private, no-store","Content-Security-Policy":"default-src 'none'; sandbox"})
