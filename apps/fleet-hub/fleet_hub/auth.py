import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from packages.protocol.network import allowed_peer

from .models import Fields


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def bearer(request):
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    return value if scheme.lower() == "bearer" else ""


class Login(Fields):
    password: str


# Credentials derived from one node may only authenticate these plugin operations.
# All other routes continue to require the administrator session or API key.
PLUGIN_OPERATIONS = {
    "POST": re.compile(
        r"^/api/desktop/([A-Za-z0-9_-]{1,100})/(?:heartbeat|events|dispatch|turns(?:/[a-f0-9]{32})?)$"
    ),
    "GET": re.compile(
        r"^/api/desktop/([A-Za-z0-9_-]{1,100})/(?:nodes|decisions/[a-f0-9]{32}|files/[A-Za-z0-9_-]{1,100}/[\w.-]{1,80})$"
    ),
}


def desktop_key(node_secret):
    return hmac.digest(node_secret.encode(), b"dsh-fleet/desktop/v1", "sha256").hex()


class Security:
    def __init__(self, config, store):
        self.config = config
        self.store = store
        self.salt = secrets.token_bytes(16)
        self.admin_hash = self.derive(config["password"])
        self.api_hash = digest(config["api_key"])
        if not config["password"] or not config["api_key"]:
            raise ValueError("请配置独立的管理员密码和 API Key")
        self.failures = defaultdict(deque)
        self.public_origin = config.get("public_url", "").rstrip("/")

    def derive(self, password):
        return hashlib.scrypt(
            password.encode(), salt=self.salt, n=2**14, r=8, p=1, dklen=32
        )

    async def valid(self, request):
        if token := bearer(request):
            pattern = PLUGIN_OPERATIONS.get(request.method)
            if pattern and (match := pattern.fullmatch(request.url.path)):
                node_id = match[1]
                row = await self.store.row(
                    "SELECT doc,secret FROM nodes WHERE id=?", node_id
                )
                if (
                    row
                    and json.loads(row["doc"]).get("enabled", True)
                    and hmac.compare_digest(
                        digest(token), digest(desktop_key(row["secret"]))
                    )
                ):
                    request.state.desktop_node = node_id
                    return True
                return False
            return hmac.compare_digest(digest(token), self.api_hash)
        session = request.cookies.get("fleet_session")
        if not session:
            return False
        return bool(
            await self.store.row(
                "SELECT 1 FROM sessions WHERE digest=? AND expires>?",
                digest(session),
                time.time(),
            )
        )

    def install(self, app):
        @app.middleware("http")
        async def guard(request: Request, call_next):
            peer = request.client.host if request.client else ""
            if not allowed_peer(peer, self.config["allowed_cidrs"]):
                return JSONResponse({"detail": "来源不在允许的网段"}, 403)
            path = request.url.path
            private_endpoint = path == "/health" or path.startswith("/api/")
            exempt = {"/api/me", "/api/login", "/api/enroll", "/api/node/verify"}
            if (
                private_endpoint
                and path not in exempt
                and not await self.valid(request)
            ):
                return JSONResponse({"detail": "请登录"}, 401)
            if request.method not in {"GET", "HEAD", "OPTIONS"} and not bearer(request):
                origin = request.headers.get("origin")
                if origin and origin not in {
                    self.public_origin,
                    str(request.base_url).rstrip("/"),
                }:
                    return JSONResponse({"detail": "来源与控制台不一致"}, 403)
                if request.headers.get("sec-fetch-site") == "cross-site":
                    return JSONResponse({"detail": "跨站请求已拒绝"}, 403)
            response = await call_next(request)
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Frame-Options"] = "DENY"
            if private_endpoint:
                response.headers["Cache-Control"] = "no-store"
            return response

        @app.get("/api/me")
        async def me(request: Request):
            return {"authenticated": await self.valid(request)}

        @app.post("/api/login")
        async def login(data: Login, request: Request):
            now = time.time()
            failures = self.failures[request.client.host]
            while failures and failures[0] < now - 60:
                failures.popleft()
            if len(failures) >= 8:
                raise HTTPException(429, "尝试过于频繁，请稍后再试")
            failures.append(now)
            if not hmac.compare_digest(
                await asyncio.to_thread(self.derive, data.password), self.admin_hash
            ):
                raise HTTPException(401, "登录口令无效")
            if now in failures:
                failures.remove(now)
            token = secrets.token_urlsafe(40)
            ttl = 12 * 3600
            async with self.store.transaction() as db:
                await db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
                await db.execute(
                    "INSERT INTO sessions VALUES (?,?)", (digest(token), now + ttl)
                )
            res = JSONResponse({"authenticated": True})
            res.set_cookie(
                "fleet_session",
                token,
                max_age=ttl,
                httponly=True,
                samesite="strict",
                secure=self.config.get("secure_cookie", False)
                or self.public_origin.startswith("https://")
                or request.url.scheme == "https",
            )
            return res

        @app.post("/api/logout")
        async def logout(request: Request):
            async with self.store.transaction() as db:
                await db.execute(
                    "DELETE FROM sessions WHERE digest=?",
                    (digest(request.cookies.get("fleet_session", "")),),
                )
            result = JSONResponse({"authenticated": False})
            result.delete_cookie("fleet_session")
            return result
