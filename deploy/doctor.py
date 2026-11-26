"""Run read-only, authenticated checks of a local Hub or Sidecar configuration."""

import argparse
import ipaddress
import json
import os
import socket
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import psutil
import yaml


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--url", help="Reachable local service URL, useful for DNS or interface binding"
    )
    args = parser.parse_args()
    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8-sig"))
    checks = []
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def check(name, operation):
        started = time.monotonic()
        try:
            detail = operation()
            success = True
        except (
            ValueError,
            OSError,
            URLError,
            KeyError,
            RuntimeError,
            psutil.Error,
        ) as exc:
            success = False
            detail = f"HTTP {exc.code}" if isinstance(exc, HTTPError) else str(exc)
        checks.append(
            {
                "check": name,
                "ok": success,
                "ms": round((time.monotonic() - started) * 1000, 1),
                "detail": detail,
            }
        )
        return detail if success else None

    def fetch(url, token=None, method="GET", status_only=False):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        parts = urlsplit(url)
        if (
            parts.scheme not in {"https", "http"}
            or not parts.hostname
            or parts.username
            or parts.password
        ):
            raise ValueError("invalid HTTP origin")
        with opener.open(
            Request(url, method=method, headers=headers), timeout=4
        ) as res:
            if status_only:
                return res.status
            return json.load(res)

    def bookmark(url):
        try:
            return fetch(url, method="GET", status_only=True)
        except HTTPError as exc:
            if exc.code in {301, 302, 303, 307, 308, 401, 403}:
                return {"reachable": True, "http_status": exc.code}
            raise

    port = int(cfg["port"])

    def bindings():
        rows = [
            conn.laddr.ip
            for conn in psutil.net_connections(kind="inet")
            if conn.status == psutil.CONN_LISTEN and conn.laddr.port == port
        ]
        if not rows:
            raise ValueError("no listener visible on this host")
        if any(ipaddress.ip_address(addr).is_unspecified for addr in rows):
            raise ValueError("wildcard listener detected")
        if (
            cfg.get("bind_mode", "ip") == "ip"
            and cfg.get("bind_ip", "127.0.0.1") not in rows
        ):
            raise ValueError("listener differs from configured binding")
        return sorted(set(rows))

    addresses = check("local listener", bindings)
    host = cfg.get("bind_ip") or (addresses or ["127.0.0.1"])[0]
    url = args.url or f"http://{'[' + host + ']' if ':' in host else host}:{port}"
    url = url.rstrip("/")
    hub = "database" in cfg or "api_key" in cfg
    if hub:
        key = os.environ.get("FLEET_API_KEY") or cfg.get("api_key")
        nodes = check("Hub API credential", lambda: fetch(url + "/api/nodes", key))
        rows = (
            nodes
            if isinstance(nodes, list)
            else (nodes or {}).get("nodes", (nodes or {}).get("items", []))
        )
        for node in rows:
            ident = node["id"]

            def online(n=node):
                if not (n.get("online") or n.get("status") == "online"):
                    raise ValueError("offline")
                return {"rtt_ms": n.get("rtt")}

            check(f"node {ident}", online)
            base = f"{url}/api/nodes/{quote(ident, safe='')}/volumes"
            volumes = check(f"volumes {ident}", lambda: fetch(base, key))
            volume_rows = (
                volumes
                if isinstance(volumes, list)
                else (volumes or {}).get("volumes", (volumes or {}).get("items", []))
            )
            for vol in volume_rows:
                name = vol["name"]
                check(
                    f"read {ident}/{name}",
                    lambda: (
                        bool(
                            fetch(
                                base + "/" + quote(name, safe="") + "/list?path=", key
                            )
                        )
                        or "empty"
                    ),
                )
        services = check("service registry", lambda: fetch(url + "/api/services", key))
        services = (
            services
            if isinstance(services, list)
            else (services or {}).get("services", (services or {}).get("items", []))
        )
        for service in services:
            check("bookmark " + service["name"], lambda: bookmark(service["url"]))
    else:
        check("Sidecar credential", lambda: bool(fetch(url + "/health", cfg["secret"])))
        for volume in cfg.get("volumes", []):

            def read_volume():
                with os.scandir(volume["path"]) as entries:
                    next(entries, None)
                return "readable"

            check("read " + volume["name"], read_volume)
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if all(c["ok"] for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
