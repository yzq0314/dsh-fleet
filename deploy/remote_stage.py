"""Stage a tracked release snapshot via OpenSSH on explicitly named hosts."""

import argparse
import re
import subprocess
import tempfile
import uuid
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument(
        "--target", required=True, help="Comma-separated bootstrap aliases"
    )
    parser.add_argument("--remote-dir", default=".dsh-fleet-releases")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[\w.-]+(?:/[\w.-]+)*", args.remote_dir, re.ASCII) or any(
        s in {".", ".."} for s in args.remote_dir.split("/")
    ):
        parser.error("--remote-dir must be a simple relative path")
    data = yaml.safe_load(args.bootstrap.read_text(encoding="utf-8-sig"))
    known = {entry["alias"]: entry for entry in [data["hub"], *data["nodes"]]}
    names = args.target.split(",")
    if args.config and (len(names) != 1 or not args.config.is_file()):
        parser.error(
            "--config requires one explicit target and an existing per-device config"
        )
    targets = []
    for name in names:
        if name not in known:
            parser.error(f"unknown alias {name!r}")
        entry = known[name]
        user, host = entry.get("ssh_user"), entry.get("host")
        if (
            not user
            or not host
            or not re.fullmatch(r"[A-Za-z0-9_.][\w.-]*", user)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host)
        ):
            parser.error(f"configure ssh_user and host for {name!r}")
        targets.append(
            (name, f"{user}@{host}", str(entry.get("os", "")).lower().startswith("win"))
        )
    release = "fleet-" + uuid.uuid4().hex[:12]
    folder = f"{args.remote_dir}/{release}"
    head = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
    )
    dirty = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if head.returncode:
        print(
            "No committed HEAD exists. Make an audited source commit before staging a release.",
            file=sys.stderr,
        )
    elif dirty:
        print(
            "The working tree has uncommitted changes. This stages only the committed HEAD.",
            file=sys.stderr,
        )
    if not args.dry_run and head.returncode:
        return 2
    if args.dry_run:
        for name, host, windows in targets:
            print(
                f"{name}: stage Git HEAD via verified SSH -> {host}:{folder}"
                + (" + private config" if args.config else "")
            )
        return
    common = [
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=5",
    ]
    with tempfile.TemporaryDirectory() as temp:
        archive = Path(temp) / "source.tar"
        subprocess.run(
            [
                "git",
                "-C",
                str(ROOT),
                "archive",
                "--format=tar",
                "-o",
                str(archive),
                "HEAD",
            ],
            check=True,
        )
        for name, host, windows in targets:
            create = (
                f"mkdir {folder.replace('/', chr(92))}"
                if windows
                else f"umask 077 && mkdir -p {args.remote_dir} && mkdir {folder}"
            )
            subprocess.run(["ssh", *common, host, create], check=True)
            subprocess.run(
                ["scp", *common, str(archive), f"{host}:{folder}/source.tar"],
                check=True,
            )
            subprocess.run(
                ["ssh", *common, host, f"tar -xf {folder}/source.tar -C {folder}"],
                check=True,
            )
            if args.config:
                subprocess.run(
                    [
                        "scp",
                        *common,
                        str(args.config.resolve()),
                        f"{host}:{folder}/fleet.local.yaml",
                    ],
                    check=True,
                )
                if not windows:
                    subprocess.run(
                        ["ssh", *common, host, f"chmod 600 {folder}/fleet.local.yaml"],
                        check=True,
                    )
            print(
                f"{name}: staged {folder}. Start the platform installer from that release directory."
            )


if __name__ == "__main__":
    raise SystemExit(main())
