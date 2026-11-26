"""Enroll a local Sidecar; obtain the one-time token from your Hub first."""

import argparse
import ipaddress
import json
import os
import socket
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import yaml
from fleet_node.paths import guarded_target, protected_roots, sensitive_path


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def prepare_workspace(requested, cfg, created):
    protected = protected_roots({"_config_file": str(cfg)})

    def checked(raw):
        path = guarded_target(Path.cwd(), str(raw.expanduser()))
        if path is None or sensitive_path(path, protected):
            raise ValueError(
                "Workspace path is protected or contains a filesystem link"
            )
        return path

    if requested:
        roots = [checked(path) for path in requested]
        if any(not path.is_dir() for path in roots):
            raise ValueError("Each --workspace must be an existing directory")
    else:
        documents = checked(Path.home() / "Documents")
        markers = [p / ".git" for p in (documents, *documents.parents)]
        if any(marker.is_file() or (marker / "HEAD").is_file() for marker in markers):
            raise ValueError(
                "The default workspace is inside a source checkout. Supply --workspace."
            )
        if not documents.is_dir():
            documents.mkdir(mode=0o700)
            created.append(documents)
        default = checked(documents / "Fleet-Work")
        if default.exists():
            default = Path(tempfile.mkdtemp(prefix="Fleet-Work-", dir=documents))
        else:
            default.mkdir(mode=0o700)
        created.append(default)
        roots = [default]
    # A new directory prevents publishing files from a pre-existing personal inbox.
    inbox = Path(tempfile.mkdtemp(prefix="fleet-inbox-", dir=roots[0]))
    created.append(inbox)
    checked(inbox)
    return roots, inbox


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--hub", required=True)
    p.add_argument("--token", default=os.getenv("FLEET_JOIN_TOKEN"))
    p.add_argument(
        "--url", help="Address reachable from the hub; prompted when omitted"
    )
    p.add_argument("--alias", default=socket.gethostname())
    p.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "local/node.local.yaml",
    )
    p.add_argument("--cidr", action="append", default=[])
    p.add_argument("--workspace", action="append", type=Path, default=[])
    p.add_argument("--tag", action="append", default=[])
    p.add_argument("--bind-ip", help="Local overlay interface IP for DNS node URLs")
    args = p.parse_args()
    if not args.token:
        p.error("supply --token or set FLEET_JOIN_TOKEN")
    if not args.url:
        args.url = input(
            "Sidecar URL reachable from the Hub (http://HOST:PORT): "
        ).strip()
    hub, node = urlsplit(args.hub), urlsplit(args.url)
    if any(
        u.scheme not in {"http", "https"}
        or not u.hostname
        or u.username
        or u.password
        or u.query
        or u.fragment
        or u.path.rstrip("/")
        for u in (hub, node)
    ):
        p.error("use an HTTP(S) origin without embedded credentials")
    if node.scheme != "http":
        p.error("the sidecar listens on HTTP inside the encrypted overlay")
    try:
        ip = ipaddress.ip_address(args.bind_ip or node.hostname)
        if ip.is_unspecified or ip.is_multicast:
            p.error("use a specific local unicast interface IP")
        hub_ips = {
            s[4][0]
            for s in socket.getaddrinfo(
                hub.hostname, hub.port or 80, type=socket.SOCK_STREAM
            )
        }
        networks = [
            str(ipaddress.ip_network(c, strict=False)) for c in (args.cidr or hub_ips)
        ]
    except (ValueError, OSError) as error:
        p.error(f"check --bind-ip, hub DNS and --cidr: {error}")
    cfg = args.config.expanduser().resolve()
    cfg.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Claim a new filename before spending the single-use join token.
    with cfg.open("xb"):
        pass
    os.chmod(cfg, 0o600)
    temp = None
    registered = False
    created = []
    try:
        work, inbox = prepare_workspace(args.workspace, cfg, created)
        payload = json.dumps(
            {
                "token": args.token,
                "url": args.url.rstrip("/"),
                "alias": args.alias,
                "tags": args.tag,
            }
        ).encode()
        req = Request(
            args.hub.rstrip("/") + "/api/enroll",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(req, timeout=10) as res:
            enrollment = json.load(res)
        registered = True
        fd, temp = tempfile.mkstemp(prefix=".enroll-", dir=cfg.parent)
        with os.fdopen(fd, "w", encoding="utf8") as f:
            yaml.safe_dump(
                {
                    "bind_mode": "ip",
                    "bind_ip": str(ip),
                    "port": node.port or 80,
                    "allowed_cidrs": networks,
                    "secret": enrollment["secret"],
                    "node_id": enrollment["id"],
                    "hub_url": args.hub.rstrip("/"),
                    "trusted_workspaces": [str(w) for w in work],
                    "allow_arbitrary_dirs": False,
                    "volumes": [
                        {"name": "inbox", "path": str(inbox), "read_only": False}
                    ],
                    "agent": "dsh",
                },
                f,
                allow_unicode=True,
            )
        os.replace(temp, cfg)
    except BaseException:
        if registered:
            print("配对已在 Hub 登记。若文件未写入，请从 Hub 移除该节点后重新配对。")
        if cfg.exists() and cfg.stat().st_size == 0:
            cfg.unlink()
        for directory in reversed(created):
            try:
                directory.rmdir()
            except OSError:
                pass  # A nonempty directory may have received user files.
        raise
    finally:
        if temp:
            Path(temp).unlink(missing_ok=True)
    print(f"Paired {enrollment['id']}. Local config: {cfg}")


if __name__ == "__main__":
    main()
