"""Execute immutable inputs in a capability-limited guest. No host-shell backend."""

import asyncio
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .paths import guarded_target, sensitive_path
from .processes import spawn_settings, stop_process
from .volumes import reserved

EXTENSIONS = {
    "python": {".py"},
    "pytest": {".py"},
    "node": {".js", ".cjs", ".mjs"},
    "powershell": {".ps1"},
}
MAX_FILE = 2 * 1024 * 1024
MAX_INPUT = 8 * MAX_FILE
MAX_OUTPUT = 65536
SLOTS = asyncio.Semaphore(2)


def wants_native(config, language):
    return (
        os.name == "nt"
        and config.get("policy_mode", "balanced") == "balanced"
        and config.get("windows_runner", "native") == "native"
        and language in {"python", "node", "powershell"}
        and language not in config.get("sandbox_images", {})
    )


def available(config):
    images = config.get("sandbox_images", {})
    if not isinstance(images, dict):
        raise ValueError("sandbox_images must map language names to pinned image IDs")
    if config.get("windows_runner", "native") not in {"native", "isolated"}:
        raise ValueError("windows_runner must be native or isolated")

    result = []
    native = set()
    if os.name == "nt" and config.get("policy_mode", "balanced") == "balanced":
        from .windows_runtime import native_languages

        native = set(native_languages())
    wasi = importlib.util.find_spec("wasi_python") and importlib.util.find_spec(
        "wasmtime"
    )
    for language in EXTENSIONS:
        if language in images:
            enabled = re.fullmatch(r"sha256:[0-9a-f]{64}", str(images[language]))
        elif wants_native(config, language):
            enabled = language in native
        else:
            enabled = wasi and (
                language == "python"
                or language == "pytest"
                and importlib.util.find_spec("pytest")
            )
        if enabled:
            result.append(language)
    return result


def checked_name(raw):
    if (
        not isinstance(raw, str)
        or not raw
        or len(raw) > 250
        or "\\" in raw
        or ":" in raw
        or "\x00" in raw
    ):
        raise ValueError("Use a workspace-relative POSIX file path")
    parts = raw.split("/")
    if any(
        p in {"", ".", ".."} or p.endswith((" ", ".")) or reserved(p) for p in parts
    ):
        raise ValueError("Invalid relative path")
    return PurePosixPath(raw)


def secure_read(workspace, relative, protected, limit):
    target = guarded_target(workspace, relative)
    if (
        target is None
        or not target.is_relative_to(workspace)
        or sensitive_path(target, protected)
    ):
        raise PermissionError("Input is outside the allowed workspace")
    before = target.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_size > limit
        or before.st_nlink > 1
    ):
        raise ValueError("Script and inputs must be bounded regular files")
    fd = os.open(
        target, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (
            not os.path.samestat(before, opened)
            or guarded_target(workspace, relative) != target
        ):
            raise PermissionError("Input changed while opening")
        data = stream.read(limit + 1)
        final = os.fstat(stream.fileno())
        if (
            final.st_nlink != 1
            or final.st_size != opened.st_size
            or final.st_mtime_ns != opened.st_mtime_ns
            or guarded_target(workspace, relative) != target
            or not os.path.samestat(target.stat(follow_symlinks=False), final)
        ):
            raise PermissionError("Input changed during the snapshot")
    if len(data) > limit:
        raise ValueError("Input exceeds size limit")
    return data


@dataclass
class Plan:
    language: str
    script: str
    files: dict[str, bytes]
    args: list[str]


async def prepare(args, task, config, protected, authorize):
    if config.get("policy_mode", "balanced") == "strict_hash":
        raise PermissionError("Dynamic runs disabled by strict_hash")
    if not isinstance(args, dict) or set(args) - {
        "language",
        "script",
        "inputs",
        "args",
    }:
        raise ValueError("Invalid fleet_run arguments")
    language = args.get("language", "python")
    if language not in available(config):
        raise ValueError(
            "No isolated runner available; install the sandbox extra or configure a pinned guest image"
        )
    script = str(checked_name(args.get("script")))
    if Path(script).suffix.casefold() not in EXTENSIONS[language]:
        raise ValueError("Script extension does not match the isolated runner")
    inputs = args.get("inputs", [])
    argv = args.get("args", [])
    if (
        not isinstance(inputs, list)
        or len(inputs) > 24
        or not isinstance(argv, list)
        or len(argv) > 32
    ):
        raise ValueError("Too many inputs or arguments")
    if any(not isinstance(s, str) or len(s) > 1024 or "\x00" in s for s in argv):
        raise ValueError("Invalid guest arguments")
    if language == "pytest" and argv:
        raise ValueError("pytest uses fixed isolated arguments")
    names = list(dict.fromkeys([script, *(str(checked_name(s)) for s in inputs)]))
    workspace = Path(task.workspace).resolve()
    paths = [str(workspace / name) for name in names]
    # Validate boundaries before any read. A dynamic outside workspace has
    # already crossed the task-level approval and still needs file authorization.
    for name in names:
        target = guarded_target(workspace, name)
        if (
            target is None
            or not target.is_relative_to(workspace)
            or sensitive_path(target, protected)
        ):
            raise PermissionError("Protected input")
    if not await authorize(
        "读取隔离执行快照 " + json.dumps(names, ensure_ascii=False), paths, False
    ):
        raise PermissionError("Input read denied")
    files = {}
    total = 0
    for name in names:
        data = await asyncio.to_thread(
            secure_read, workspace, name, protected, min(MAX_FILE, MAX_INPUT - total)
        )
        files[name] = data
        total += len(data)
    content = files[script].decode("utf-8-sig")
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    description = json.dumps(
        {"language": language, "script": script, "args": argv, "inputs_sha256": hashes},
        ensure_ascii=False,
    )
    # Balanced review applies to bulk data disclosure across the guest boundary.
    # All modes retain user-defined and high-risk rules and the same hard sandbox.
    review = config.get("policy_mode", "balanced") == "balanced" and (
        total > MAX_FILE or len(names) > 8
    )
    if not await authorize("fleet_run " + description + "\n" + content, paths, review):
        raise PermissionError("Sandbox execution denied")
    return Plan(language, script, files, argv)


async def bounded(cmd, *, env=None, timeout=90, max_output=MAX_OUTPUT):
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **spawn_settings(),
    )
    try:
        result = bytearray()
        async with asyncio.timeout(timeout):
            while chunk := await proc.stdout.read(8192):
                result.extend(chunk)
                if len(result) > max_output:
                    raise ValueError("Guest output exceeded limit")
            code = await proc.wait()
        return f"exit={code}\n" + result.decode("utf8", errors="replace")
    finally:
        await stop_process(proc)


def container_command(config, plan, directory, name):
    # Only a local engine, image ID and fixed guest entry point are accepted.
    engine = shutil.which("docker")
    image = config.get("sandbox_images", {}).get(plan.language)
    if (
        not engine
        or plan.language not in {"python", "node", "powershell", "pytest"}
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(image))
    ):
        raise ValueError("Pinned container runtime unavailable")
    endpoint = (
        "npipe:////./pipe/docker_engine"
        if os.name == "nt"
        else "unix:///var/run/docker.sock"
    )
    if "," in str(directory):
        raise ValueError("Staging path cannot be represented as a Docker mount")
    base = [engine, "--host", endpoint]
    entry, args = {
        "python": ("python", ["-I", "-B", "/work/" + plan.script, *plan.args]),
        "node": ("node", ["/work/" + plan.script, *plan.args]),
        "powershell": (
            "pwsh",
            [
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                "/work/" + plan.script,
                *plan.args,
            ],
        ),
        "pytest": (
            "python",
            [
                "-I",
                "-B",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "/work/" + plan.script,
            ],
        ),
    }[plan.language]
    cmd = [
        *base,
        "run",
        "--pull=never",
        "--rm",
        "--name",
        name,
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "65534:65534",
        "--read-only",
        "--pids-limit",
        "32",
        "--cpus",
        "0.5",
        "--memory",
        "256m",
        "--memory-swap",
        "256m",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,noexec,size=8m",
        "--mount",
        f"type=bind,source={directory},target=/work,readonly",
        "--workdir",
        "/work",
        "--entrypoint",
        entry,
        image,
        *args,
    ]
    return base, cmd


async def execute(plan, config):
    if config.get("policy_mode", "balanced") == "strict_hash":
        raise PermissionError("Policy no longer allows dynamic runs")
    async with SLOTS:
        with tempfile.TemporaryDirectory(prefix="fleet-guest-") as tmp:
            stage = Path(tmp) / "work"
            stage.mkdir()
            for name, data in plan.files.items():
                destination = stage / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            if wants_native(config, plan.language):
                from .windows_guest import run_native

                return await run_native(plan, Path(tmp), stage, MAX_OUTPUT)
            if plan.language in {
                "python",
                "pytest",
            } and plan.language not in config.get("sandbox_images", {}):
                # The worker only imports audited host code. The file runs inside WASI.
                helper = Path(__file__).with_name("wasi_worker.py")
                env = {
                    k: v
                    for k, v in os.environ.items()
                    if k.upper()
                    in {
                        "SYSTEMROOT",
                        "WINDIR",
                        "TMP",
                        "TEMP",
                        "TMPDIR",
                        "LANG",
                        "HOME",
                        "USERPROFILE",
                        "LOCALAPPDATA",
                        "XDG_CACHE_HOME",
                    }
                }
                return await bounded(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        str(helper),
                        plan.language,
                        str(stage),
                        plan.script,
                        *plan.args,
                    ],
                    env=env,
                    timeout=180,
                    max_output=MAX_OUTPUT + 1024,
                )
            # Keep the enclosing temp directory private; the mounted child
            # needs to be traversable by the container's unprivileged UID.
            stage.chmod(0o755)
            for copied in stage.rglob("*"):
                copied.chmod(0o755 if copied.is_dir() else 0o644)
            name = "fleet-" + uuid.uuid4().hex
            base, cmd = container_command(config, plan, stage, name)
            client_config = Path(tmp) / "docker-client"
            client_config.mkdir(mode=0o700)
            env = {
                k: v
                for k, v in os.environ.items()
                if k.upper()
                in {"SYSTEMROOT", "WINDIR", "TMP", "TEMP", "TMPDIR", "PATH", "LANG"}
            }
            env["DOCKER_CONFIG"] = str(client_config)
            details = await bounded(
                [
                    *base,
                    "image",
                    "inspect",
                    "--format",
                    "{{.Os}} {{json .Config.Volumes}}",
                    config["sandbox_images"][plan.language],
                ],
                env=env,
                timeout=5,
            )
            if details.strip() not in {"exit=0\nlinux null", "exit=0\nlinux {}"}:
                raise RuntimeError(
                    "The pinned Linux image must exist locally and declare no implicit volumes"
                )
            try:
                return await bounded(cmd, env=env, timeout=30)
            finally:
                await bounded([*base, "rm", "-f", name], env=env, timeout=8)
