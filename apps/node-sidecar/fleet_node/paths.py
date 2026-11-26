"""Local control-plane files and Windows special paths are never agent workspaces."""

import os
import sys
from pathlib import Path, PureWindowsPath

from .volumes import is_link, reserved


def protected_roots(config):
    roots = {Path(__file__).resolve().parents[3], Path(sys.prefix).resolve()}
    if dsh_home := config.get("dsh_home") or os.environ.get("DSH_HOME"):
        roots.add(Path(dsh_home).expanduser().resolve())
    if config.get("_artifact_spool"):
        roots.add(Path(config["_artifact_spool"]).resolve())
    if path := config.get("_config_file"):
        roots.add(Path(path).resolve())
    database = config.get("state_path") or (
        Path(path).with_suffix(".state.sqlite3") if path else None
    )
    if database and str(database) != ":memory:":
        roots.update(
            Path(str(database) + suffix).resolve()
            for suffix in ("", "-wal", "-shm", ".lock")
        )
    for key in (
        "WINDIR",
        "ProgramFiles",
        "ProgramFiles(x86)",
        "ProgramData",
        "APPDATA",
    ):
        if value := os.environ.get(key):
            roots.add(Path(value).resolve())
    if value := os.environ.get("LOCALAPPDATA"):
        base = Path(value)
        roots.update(
            p for p in base.iterdir() if p.name.casefold() != "temp" and not is_link(p)
        )
    if os.name != "nt":
        roots.update(
            Path(p)
            for p in ("/proc", "/sys", "/dev", "/etc", "/boot", "/usr", "/bin", "/sbin")
        )
        roots.update(Path.home() / p for p in (".local", ".config", "Library"))
    return roots


def guarded_target(workspace, raw):
    if (
        not isinstance(raw, str)
        or not raw
        or raw.startswith(("\\\\", "//", "~"))
        or "\x00" in raw
    ):
        return None
    native = Path(raw)
    windows = PureWindowsPath(raw)
    if windows.drive and not native.is_absolute():
        return None
    parts = windows.parts[1:] if windows.anchor else windows.parts
    if any(
        ":" in p or p.endswith((" ", ".")) and p not in {".", ".."} or reserved(p)
        for p in parts
    ):
        return None
    path = workspace / native
    if any(os.path.lexists(p) and is_link(p) for p in [path, *path.parents]):
        return None
    if path.is_file() and path.stat().st_nlink > 1:
        return None
    return path.resolve()


def sensitive_path(target, roots):
    if any(target.is_relative_to(root) for root in roots):
        return True
    names = {
        ".ssh",
        ".aws",
        ".git",
        ".dsh",
        ".codex",
        ".gnupg",
        ".config",
        ".kube",
        ".azure",
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".bashrc",
        ".bash_profile",
        ".zshrc",
        "credentials",
        "authorized_keys",
        "node.yaml",
        "hub.yaml",
        "hub.db",
        "fleet.db",
        "fleet.sqlite3",
    }
    return any(
        p.casefold() in names or p.casefold().startswith(".env") for p in target.parts
    )
