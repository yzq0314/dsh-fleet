"""Install or remove the Fleet bundle in an initialized Desktop profile owned by this user."""

import argparse
import hashlib
import hmac
import json
import os
import tempfile
from pathlib import Path

import yaml

NAME = "@dsh-fleet/desktop"
FILES = ("package.json", "index.mjs", "model-service.mjs", "handoff.mjs", "cordis.patch.yml")
SOURCE = Path(__file__).resolve().parent


def replace(path, data):
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def document(path, value):
    replace(
        path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )


def manage(operation, home, node_config=None):
    home = Path(home).resolve()
    profile = home / "profiles" / "desktop"
    manifest = profile / "package.json"
    package = profile / "node_modules" / "@dsh-fleet" / "desktop"
    private = home / "fleet-desktop"
    config = private / "config.json"
    if not manifest.is_file():
        raise ValueError(
            "Initialize Desktop under this OS user before managing its Fleet bundle"
        )
    for path in (manifest, package, private):
        if path.resolve() != path:
            raise ValueError("Refusing to follow a link at a managed location")
    body = json.loads(manifest.read_text(encoding="utf-8-sig"))
    bundles = body["dsh"]["profile"]["bundles"]
    if not isinstance(bundles, list):
        raise ValueError("Unsupported profile manifest")
    marker = package / ".fleet-managed"
    if package.exists() and not marker.is_file():
        raise ValueError("A different package already occupies this location")
    if operation == "install":
        data = yaml.safe_load(Path(node_config).read_text(encoding="utf-8-sig"))
        derived = hmac.new(
            data["secret"].encode(), b"dsh-fleet/desktop/v1", hashlib.sha256
        ).hexdigest()
        if config.exists() and not marker.exists():
            raise ValueError("Conflicting private configuration")
        private.mkdir(mode=0o700, parents=True, exist_ok=True)
        package.mkdir(parents=True, exist_ok=True)
        replace(marker, b"dsh-fleet desktop bundle\n")
        for filename in FILES:
            replace(package / filename, (SOURCE / filename).read_bytes())
        document(
            config,
            {"hub_url": data["hub_url"], "node_id": data["node_id"], "token": derived},
        )
        if NAME not in bundles:
            bundles.append(NAME)
            document(manifest, body)
    elif operation == "uninstall":
        if NAME in bundles and not marker.exists():
            raise ValueError("This bundle was installed by another manager")
        if NAME in bundles:
            bundles.remove(NAME)
            document(manifest, body)
        if marker.exists():
            for filename in FILES:
                (package / filename).unlink(missing_ok=True)
            marker.unlink()
            for filename in ("config.json", "bridge.json"):
                (private / filename).unlink(missing_ok=True)
            if not any(package.iterdir()):
                package.rmdir()
            if private.is_dir() and not any(private.iterdir()):
                private.rmdir()
    else:
        raise ValueError("Unknown operation")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("install", "uninstall"))
    parser.add_argument(
        "--dsh-home",
        type=Path,
        default=Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh"),
    )
    parser.add_argument("--node-config", type=Path)
    args = parser.parse_args()
    if args.operation == "install" and not args.node_config:
        parser.error("--node-config is required to derive the scoped node credential")
    manage(args.operation, args.dsh_home, args.node_config)
    print("Desktop bundle updated. Restart Desktop if profile HMR is disabled.")
