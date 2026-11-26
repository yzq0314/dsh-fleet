"""Read public Pocket settings and probe HTTP on loopback without following redirects."""
import json
import os
from pathlib import Path

import httpx


def _port(value):
    if type(value) is int and 1 <= value <= 65535:
        return value
    return None


async def probe_pocket(config, transport=None):
    home = Path(config.get("dsh_home") or os.environ.get("DSH_HOME") or Path.home() / ".dsh")
    path = home / "dsh-pocket" / "settings.json"
    settings = {}
    try:
        with path.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) <= 65536:
            value = json.loads(raw)
            if isinstance(value, dict):
                settings = value
    except (OSError, ValueError):
        pass
    explicit = config.get("pocket_port")
    configured = _port(explicit if explicit is not None else settings.get("proxyPort"))
    ports = [configured] if configured else ([] if explicit is not None else [13081, 3081])
    info = {"pocket_port": configured, "pocket_lan_auth_enabled": None,
            "pocket_status": "stopped" if configured or settings else "unconfigured",
            "pocket_hint": "在 DSH Desktop 启用 compatibility 模式并检查 Pocket 设置。"}
    if type(settings.get("lanAuthEnabled")) is bool:
        info["pocket_lan_auth_enabled"] = settings["lanAuthEnabled"]
    async with httpx.AsyncClient(trust_env=False, timeout=.4, transport=transport) as client:
        for port in ports:
            try:
                async with client.stream("GET", f"http://127.0.0.1:{port}/") as reply:
                    if reply.status_code in (200, 301, 302, 303, 307, 308, 401, 403):
                        return {**info, "pocket_port": port, "pocket_status": "ready",
                                "pocket_hint": "本机 HTTP 已响应，登录状态由 Pocket 验证。"}
            except httpx.HTTPError:
                continue
    return info
