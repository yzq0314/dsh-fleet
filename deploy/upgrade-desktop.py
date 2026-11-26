"""Update the installed local Desktop bundle via the live SDK, preserving pairing."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile

import yaml

from fleet_node.agents import dsh_command

FILES = ("package.json", "cordis.patch.yml", "index.mjs", "handoff.mjs", "model-service.mjs")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--cli", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = root / "integrations/dsh-app-plugin"
    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8-sig"))
    home = Path(cfg.get("dsh_home") or os.environ.get("DSH_HOME") or Path.home() / ".dsh")
    profile = home / "profiles/desktop"
    installed = profile / "node_modules/@dsh-fleet/desktop"
    expected = {name: (source/name).read_bytes() for name in FILES}
    pairing = home / "fleet-desktop/config.json"
    digest = hashlib.sha256(pairing.read_bytes()).digest()

    def current():
        bundles = json.loads((profile/"package.json").read_text(encoding="utf-8-sig"))["dsh"]["profile"]["bundles"]
        return "@dsh-fleet/desktop" in bundles and all((installed/k).is_file() and (installed/k).read_bytes()==v for k,v in expected.items())

    bundles = json.loads((profile/"package.json").read_text(encoding="utf-8-sig"))["dsh"]["profile"]["bundles"]
    if "@dsh-fleet/desktop" not in bundles and not installed.exists():
        print("Desktop bundle is not enabled in this profile; previous selection preserved")
        return
    if current():
        print("Desktop bundle verified; already current")
        return
    env = {**os.environ, "DSH_HOME": str(home)}
    if args.cli:
        cli = args.cli.resolve(strict=True)
        if cli.suffix.lower() not in (".cmd", ".exe"):
            raise ValueError("Expected installed DSH launcher")
        argv = [str(cli)]
    else:
        argv = dsh_command(env, cfg.get("dsh_cli"))
    package = root / "integrations" / "fleet-desktop.tgz"
    with tarfile.open(package, "w:gz") as archive:
        for name in FILES:
            archive.add(source/name, "package/"+name)
    # Desktop's native manager owns profile/lockfile consistency.
    subprocess.run([*argv,"plugin","--profile","desktop","add","--prefer-offline","--ignore-scripts",str(package)],
                   env=env, check=True, timeout=150, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not current() or hashlib.sha256(pairing.read_bytes()).digest()!=digest:
        raise RuntimeError("Desktop package or existing pairing could not be verified")
    print("Desktop bundle updated; reopen Desktop to load this version")


if __name__ == "__main__":
    main()
