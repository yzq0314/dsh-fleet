import asyncio
import mimetypes
import ntpath
import os
import stat
import tempfile
from pathlib import Path, PureWindowsPath
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask


def is_link(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def reserved(part):
    if hasattr(ntpath, "isreserved"):
        return ntpath.isreserved(part)
    return PureWindowsPath(part).is_reserved()


class Volume:
    def __init__(self, config, write_barriers=()):
        self.name = config["name"]
        configured = config.get("path") or config.get("root")
        if not configured:
            raise ValueError("A volume needs an explicit root")
        self.root = Path(configured).resolve()
        self.read_only = bool(config.get("read_only", True))
        self.write_barriers = write_barriers

    def target(self, relative):
        if (
            "\x00" in relative
            or "\\" in relative
            or PureWindowsPath(relative).drive
            or relative.startswith("/")
        ):
            raise HTTPException(403, "Invalid volume path")
        path = self.root
        for part in relative.split("/"):
            if part in ("", "."):
                continue
            if (
                part == ".."
                or ":" in part
                or part.endswith((" ", "."))
                or reserved(part)
            ):
                raise HTTPException(403, "Invalid volume path")
            path = path / part
            if os.path.lexists(path) and is_link(path):
                raise HTTPException(
                    403, "Links and reparse points are outside the volume API"
                )
        if not self.root.is_dir():
            raise HTTPException(503, "Volume is unavailable")
        if not path.resolve().is_relative_to(self.root):
            raise HTTPException(403, "Path leaves the volume")
        if path.is_file() and path.stat().st_nlink > 1:
            raise HTTPException(403, "Hard-linked files are outside the volume API")
        return path

    def entries(self, relative, query=None):
        directory = self.target(relative)
        if not directory.is_dir():
            raise HTTPException(404, "Directory not found")
        result, pending, examined = [], [directory], 0
        while pending:
            for path in sorted(
                pending.pop().iterdir(), key=lambda p: p.name.casefold()
            ):
                examined += 1
                if examined > 10_000 or len(result) >= 500:
                    return {"entries": result, "truncated": True}
                if is_link(path):
                    continue
                if query is not None and path.is_dir():
                    pending.append(path)
                if query is None or query.casefold() in path.name.casefold():
                    result.append(
                        {
                            "name": path.name,
                            "path": path.relative_to(self.root).as_posix(),
                            "is_dir": path.is_dir(),
                            "size": 0 if path.is_dir() else path.stat().st_size,
                        }
                    )
        return {"entries": result, "truncated": False}

    async def upload(
        self, relative, request: Request, byte_limit, *, create_only=False
    ):
        if self.read_only:
            raise HTTPException(403, "Read-only volume")
        path = self.target(relative)
        if any(path.is_relative_to(root) for root in self.write_barriers):
            raise HTTPException(403, "The configured export root is read-only")
        if path == self.root or not path.parent.is_dir() or path.is_dir():
            raise HTTPException(400, "An existing parent and a file name are required")
        length = 0
        fd, filename = tempfile.mkstemp(dir=path.parent, prefix=".upload-")
        try:
            with os.fdopen(fd, "wb") as handle:
                async for chunk in request.stream():
                    length += len(chunk)
                    if length > byte_limit:
                        raise HTTPException(413, "Upload exceeds the size limit")
                    await asyncio.to_thread(handle.write, chunk)
            if self.target(relative) != path or any(
                path.is_relative_to(root) for root in self.write_barriers
            ):
                raise HTTPException(409, "Volume changed during upload")
            if create_only:
                try:
                    if os.name == "nt":
                        os.rename(filename, path)
                    else:
                        os.link(filename, path)
                except FileExistsError as exc:
                    raise HTTPException(409, "目标文件已存在，请另选名称") from exc
                except OSError as exc:
                    raise HTTPException(409, "该存储不支持创建互斥目标文件") from exc
            else:
                os.replace(filename, path)
        finally:
            Path(filename).unlink(missing_ok=True)
        return {"path": relative, "size": length}

    def serve(self, relative, preview=False):
        path = self.target(relative)
        if not path.is_file():
            raise HTTPException(404, "File not found")
        headers = {
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        }
        mime = mimetypes.guess_type(path.name)[0] or ""
        handle = path.open("rb")
        try:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink > 1
                or not os.path.samestat(info, self.target(relative).stat())
            ):
                raise HTTPException(403, "File changed during open")
            if preview and path.suffix.lower() == ".svg":
                from .svg_preview import sanitize_svg

                image = sanitize_svg(handle.read(512 * 1024 + 1))
                handle.close()
                return Response(image, media_type="image/svg+xml", headers=headers)
            if preview and mime not in {
                "image/png",
                "image/jpeg",
                "image/gif",
                "image/webp",
            }:
                if not (
                    mime.startswith("text/")
                    or path.suffix.lower()
                    in {".csv", ".txt", ".log", ".json", ".yaml", ".yml", ".md", ".tsv"}
                ):
                    raise HTTPException(
                        415, "Preview supports text, CSV and raster images"
                    )
                content = handle.read(128 * 1024)
                handle.close()
                return Response(
                    content.decode("utf-8-sig", errors="replace"),
                    media_type="text/plain",
                    headers=headers,
                )
            if not preview:
                headers["Content-Disposition"] = (
                    "attachment; filename*=UTF-8''" + quote(path.name, safe="")
                )
            headers["Content-Length"] = str(info.st_size)
            return StreamingResponse(
                iter(lambda: handle.read(128 * 1024), b""),
                headers=headers,
                media_type=mime if preview else "application/octet-stream",
                background=BackgroundTask(handle.close),
            )
        except BaseException:
            handle.close()
            raise
