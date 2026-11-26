#!/usr/bin/env bash
# Install for the current non-root systemd user.
set -euo pipefail
umask 077
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ $# -ne 1 || ! -f "$1" ]]; then
    echo "Usage: bash deploy/install-linux-hub.sh /path/to/private/hub.yaml" >&2; exit 2
fi
config="$(realpath -- "$1")"
command -v uv >/dev/null || { echo "Install uv first" >&2; exit 2; }
command -v systemctl >/dev/null
systemctl --user show-environment >/dev/null
cd -- "$repo"
uv sync --no-dev
if [[ ! -f apps/web-pwa/dist/index.html ]]; then
    (cd apps/web-pwa && npm ci && npm run build)
fi
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p -- "$unit_dir"
# Escape specifiers and quote literal paths according to systemd.syntax.
"$repo/.venv/bin/python" - "$repo" "$config" "$unit_dir/dsh-fleet-hub.service" <<'PY'
from pathlib import Path
import os
import secrets
import sys
import yaml

repo, config, unit = map(Path, sys.argv[1:])
def quoted(value):
    value = str(value)
    if any(ord(c) < 32 for c in value):
        raise SystemExit("Control character in path")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'

data = yaml.safe_load(config.read_text(encoding="utf-8-sig"))
envfile = unit.with_name("dsh-fleet-hub.env")
if not envfile.exists():
    lines = []
    for field in ("password", "api_key"):
        env_name = "FLEET_" + field.upper()
        value = os.getenv(env_name)
        if not value and not data.get(field):
            value = secrets.token_urlsafe(32)
        if value:
            if any(ord(char) < 32 for char in value):
                raise SystemExit("Newline or control character in a supplied credential")
            literal = value.replace('\\', '\\\\').replace('"', '\\"')
            lines.append(f'{env_name}="{literal}"')
    with envfile.open("x", encoding="utf8") as stream:
        stream.write("\n".join(lines) + "\n")
    envfile.chmod(0o600)
print("Private service credential overrides:", envfile)
text = [
    "[Unit]", "Description=DSH Fleet Hub", "After=network-online.target",
    "[Service]", "Type=simple", "WorkingDirectory=" + quoted(repo),
    "Environment=" + quoted(f"PYTHONPATH={repo}:{repo / 'apps/fleet-hub'}"),
    "EnvironmentFile=" + quoted(envfile),
    "ExecStart=" + quoted(repo / ".venv/bin/python").replace("$", "$$") + " -m fleet_hub.main --config " + quoted(config).replace("$", "$$"),
    "Restart=on-failure", "RestartSec=4", "UMask=0077", "NoNewPrivileges=true",
    "[Install]", "WantedBy=default.target", "",
]
unit.write_text("\n".join(text), encoding="utf8")
PY
chmod 600 -- "$config"
systemctl --user daemon-reload
systemctl --user enable dsh-fleet-hub.service
systemctl --user restart dsh-fleet-hub.service
systemctl --user is-active dsh-fleet-hub.service
echo "For boot before login, enable linger for this user with loginctl enable-linger."
