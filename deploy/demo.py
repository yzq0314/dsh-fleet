"""Run a local three-node Fleet demo using real HTTP connections."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

import yaml
import psutil

ROOT = Path(__file__).resolve().parents[1]


def atomic_private(path, value):
    data = (
        json.dumps(value, ensure_ascii=False, indent=2)
        if path.suffix == ".json"
        else yaml.safe_dump(value, allow_unicode=True)
    )
    fd, temporary = tempfile.mkstemp(prefix=".new-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def free_ports(count, hub_port):
    sockets = []
    try:
        for index in range(count):
            sock = socket.socket()
            sockets.append(sock)
            sock.bind(("127.0.0.1", hub_port if index == 0 else 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


class Cluster:
    def __init__(self, directory, port=8765, wol_port=9):
        self.state = Path(directory).resolve()
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.port = port
        self.wol_port = wol_port
        self.children = []
        self.runtime = {}
        self.logs = []
        self.lock = self.state / "running.lock"
        self.has_lock = False

    def spawn(self, name, module, config):
        conf = self.state / f"{name}.yaml"
        atomic_private(conf, config)
        log = (self.state / f"{name}.log").open("w", encoding="utf8")
        self.logs.append(log)
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            str(p) for p in [ROOT, ROOT / "apps/fleet-hub", ROOT / "apps/node-sidecar"]
        )
        self.children.append(
            subprocess.Popen(
                [sys.executable, "-m", module, "--config", str(conf)],
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
        )

    def start(self):
        try:
            fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as error:
            raise RuntimeError(
                "此演示目录已锁定。请选择另一目录，或确认旧进程已全部退出后移除 running.lock。"
            ) from error
        self.has_lock = True
        with os.fdopen(fd, "w") as lock:
            lock.write(str(os.getpid()))
        try:
            return self._start()
        except BaseException:
            self.close()
            raise

    def _start(self):
        hub_port, *node_ports = free_ports(4, self.port)
        hub_url = f"http://127.0.0.1:{hub_port}"
        password = os.environ.get("FLEET_PASSWORD") or secrets.token_urlsafe(18)
        api_key = os.environ.get("FLEET_API_KEY") or secrets.token_urlsafe(32)
        common = {
            "bind_mode": "ip",
            "bind_ip": "127.0.0.1",
            "allowed_cidrs": ["127.0.0.0/8"],
        }
        nodes, spaces = [], {}
        for index, alias in enumerate(("Atlas", "Birch", "Cedar")):
            ident = f"demo-{chr(97 + index)}"
            workspace = self.state / ident / "work"
            inbox = workspace / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            spaces[ident] = str(workspace)
            volumes = [{"name": "inbox", "path": str(inbox), "read_only": False}]
            if ident == "demo-b":
                nas = workspace / "nas"
                nas.mkdir(exist_ok=True)
                (nas / "welcome.txt").write_text("Fleet NAS demo\n", encoding="utf8")
                volumes.append({"name": "nas", "path": str(nas), "read_only": False})
            secret = secrets.token_urlsafe(32)
            node_url = f"http://127.0.0.1:{node_ports[index]}"
            node = {
                "id": ident,
                "alias": alias,
                "url": node_url,
                "secret": secret,
                "tags": ["dev", "nas"] if ident == "demo-b" else ["dev"],
                "mac": f"02:00:00:00:00:0{index + 1}",
                "broadcast": "127.0.0.1",
            }
            nodes.append(node)
            self.spawn(
                ident,
                "fleet_node.main",
                dict(
                    common,
                    port=node_ports[index],
                    secret=secret,
                    node_id=ident,
                    hub_url=hub_url,
                    trusted_workspaces=[str(workspace)],
                    allow_arbitrary_dirs=True,
                    volumes=volumes,
                    agent="mock",
                    approval_timeout=25,
                ),
            )
        # Seed a fresh DB each launch so every rotating credential matches its Sidecar.
        db = self.state / ("demo-" + secrets.token_hex(4) + ".sqlite3")
        self.spawn(
            "hub",
            "fleet_hub.main",
            dict(
                common,
                port=hub_port,
                password=password,
                api_key=api_key,
                database=str(db),
                nodes=nodes,
                poll_interval=1,
                use_private_env=False,
                wol_port=self.wol_port,
                services=[
                    {
                        "id": "demo-nas",
                        "name": "演示 NAS",
                        "url": hub_url,
                        "node_id": "demo-b",
                        "volume": "nas",
                    }
                ],
                file_hook=str(self.state / "events.jsonl"),
            ),
        )
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + 50
        request = Request(
            hub_url + "/api/nodes", headers={"Authorization": f"Bearer {api_key}"}
        )
        while time.monotonic() < deadline:
            if any(child.poll() is not None for child in self.children):
                raise RuntimeError(f"组件意外退出，请检查 {self.state} 下的日志")
            try:
                with opener.open(request, timeout=2) as response:
                    if response.status == 200:
                        data = json.load(response)
                        rows = (
                            data
                            if isinstance(data, list)
                            else data.get("nodes", data.get("items", []))
                        )
                        if len(rows) == 3 and all(
                            n.get("online") or n.get("status") == "online" for n in rows
                        ):
                            break
            except (URLError, HTTPError, OSError):
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError("三节点启动超时，请检查演示目录中的组件日志")
        self.runtime = {
            "hub_url": hub_url,
            "password": password,
            "api_key": api_key,
            "workspaces": spaces,
            "state_dir": str(self.state),
        }
        atomic_private(self.state / "demo.json", self.runtime)
        return self.runtime

    def stop(self, proc, *, abrupt=False):
        if proc.poll() is not None:
            return
        try:
            parent = psutil.Process(proc.pid)
            # Windows venv python.exe can be a redirector with a separate Python
            # process holding the TCP listener. Take the snapshot before signalling.
            family = [*reversed(parent.children(recursive=True)), parent]
        except psutil.NoSuchProcess:
            return
        for member in family:
            try:
                member.kill() if abrupt else member.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(family, timeout=4)
        for member in alive:
            try:
                member.kill()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(alive, timeout=4)
        if alive:
            raise RuntimeError("Fleet demo subprocesses failed to exit")
        proc.wait(4)

    def close(self):
        for proc in reversed(self.children):
            self.stop(proc)
        for log in self.logs:
            log.close()
        if self.has_lock:
            self.lock.unlink(missing_ok=True)
            self.has_lock = False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port", type=int, default=8765, help="Hub port, 0 for ephemeral"
    )
    parser.add_argument("--state-dir", type=Path, default=ROOT / ".run/demo")
    args = parser.parse_args()
    web = ROOT / "apps/web-pwa"
    if not (web / "dist/index.html").is_file():
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        if not npm:
            parser.error(
                "Building the PWA for the first launch requires Node.js and npm on PATH."
            )
        if not (web / "node_modules/.bin/vite").is_file():
            subprocess.run([npm, "ci"], cwd=web, check=True)
        subprocess.run([npm, "run", "build"], cwd=web, check=True)
    cluster = Cluster(args.state_dir, args.port)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        info = cluster.start()
        print(
            f"Fleet Hub    {info['hub_url']}\n登录密码     {info['password']}\nCLI 凭据     {cluster.state / 'demo.json'}\nCtrl+C 关闭演示集群",
            flush=True,
        )
        while all(proc.poll() is None for proc in cluster.children):
            time.sleep(0.5)
        raise RuntimeError("组件已退出，请查看演示目录日志")
    except KeyboardInterrupt:
        pass
    finally:
        cluster.close()


if __name__ == "__main__":
    main()
