"""Check the working tree and staged Git blobs without printing private values."""

import os
import re
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ("local/bootstrap.local.yaml", "docs/discovery.local.md")
KEY_PATTERN = re.compile(
    rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"
    rb"|\bsk-(?:[A-Za-z0-9_-]{24,})\b"
    rb"|\b(?:gh[pousr]_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{20,}\b"
    rb"|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"
    rb"|open-apis/bot/v2/hook/[A-Fa-f0-9]{8}(?:-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12}"
)


def git(*args, check=True):
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], stdout=subprocess.PIPE, check=check
    )


def fingerprints():
    values = {str(ROOT), str(ROOT.parent), str(Path.home()), socket.gethostname()}
    bootstrap = ROOT / PRIVATE[0]
    if bootstrap.exists():
        inventory = yaml.safe_load(bootstrap.read_text(encoding="utf-8-sig"))

        def collect(item):
            if isinstance(item, list):
                for value in item:
                    collect(value)
            elif isinstance(item, dict):
                for key, value in item.items():
                    if key in {
                        "host",
                        "ssh_user",
                        "url",
                        "path",
                        "secret",
                        "password",
                        "token",
                        "api_key",
                    } and isinstance(value, str):
                        values.add(value)
                        if key == "url" and (host := urlsplit(value).hostname):
                            values.add(host)
                    if key == "allowed_cidrs" and isinstance(value, list):
                        values.update(
                            cidr
                            for cidr in value
                            if not cidr.startswith(("127.", "::1"))
                        )
                    collect(value)

        collect(inventory)
    signatures = set()
    for value in values:
        if not value or value == str(ROOT.anchor):
            continue
        variants = {value, value.replace("\\", "/"), value.replace("\\", "\\\\")}
        for variant in variants:
            signatures.update(
                variant.encode(codec).lower() for codec in ("utf-8", "utf-16le")
            )
    return signatures


def main():
    for name in PRIVATE:
        if git("check-ignore", "--no-index", "-q", name, check=False).returncode:
            sys.exit("Missing ignore rule: " + name)
    staged = {}
    for entry in git("ls-files", "--stage", "-z").stdout.split(b"\x00"):
        if entry:
            metadata, name = entry.split(b"\t", 1)
            staged[os.fsdecode(name)] = metadata.split()[1].decode("ascii")
    visible = {
        os.fsdecode(s)
        for s in git("ls-files", "--others", "--exclude-standard", "-z").stdout.split(
            b"\x00"
        )
        if s
    }
    signatures = fingerprints()
    problems = set()

    def inspect(name, raw):
        if name in PRIVATE or (name.startswith("local/") and name != "local/README.md"):
            problems.add(name)
        path = Path(name)
        private_ext = path.suffix.lower() in {".key", ".p12", ".pfx", ".kdbx"}
        if (
            private_ext
            or path.name in {"id_rsa", "id_ed25519", ".credentials.yaml"}
            or (path.name.startswith(".env") and path.name != ".env.example")
        ):
            problems.add(name)
        lowered = raw.lower()
        if KEY_PATTERN.search(raw) or any(value in lowered for value in signatures):
            problems.add(name)

    for name in sorted(visible | staged.keys()):
        file = ROOT / name
        if file.is_symlink():
            problems.add(name)
        elif file.is_file():
            inspect(name, file.read_bytes())
    # Batch staged content from Git independently of files on disk.
    if staged:
        keys = list(staged)
        request = "".join(staged[k] + "\n" for k in keys).encode("ascii")
        import io

        data = subprocess.run(
            ["git", "-C", str(ROOT), "cat-file", "--batch"],
            input=request,
            capture_output=True,
            check=True,
        )
        stream = io.BytesIO(data.stdout)
        for name in keys:
            header = stream.readline().split()
            size = int(header[2])
            inspect(name, stream.read(size))
            stream.read(1)
    if problems:
        for name in sorted(problems):
            print("Review:", name)
        return 1
    print(
        f"Privacy check passed: {len(visible | staged.keys())} candidate files, {len(staged)} indexed blobs; private files ignored."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
