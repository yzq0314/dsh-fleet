"""Create an offline source bundle for an existing interactive Windows Fleet installation."""
import argparse
import base64
import hashlib
import io
from pathlib import Path
import re
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ["README.md","pyproject.toml","uv.lock","packages","apps/node-sidecar","apps/fleet-hub",
           "integrations/hermes","integrations/dsh-app-plugin","deploy/run-sidecar.ps1",
           "deploy/windows-bootstrap.ps1","deploy/switch-windows-release.ps1","deploy/upgrade-desktop.py","deploy/upgrade-health.py"]


def build(alias, output):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}",alias):
        raise ValueError("Invalid installation alias")
    names = subprocess.check_output(["git","ls-files","-z","--",*SOURCES],cwd=ROOT).decode().split("\0")
    data = io.BytesIO()
    with zipfile.ZipFile(data,"w") as archive:
        for name in sorted(filter(None,names)):
            path = ROOT / name
            if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
                raise ValueError("Linked release file")
            entry = zipfile.ZipInfo(name)
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry,path.read_bytes())
        for required in ("deploy/upgrade-health.py","deploy/upgrade-desktop.py","packages/protocol/pocket.py","apps/node-sidecar/fleet_node/pocket_proxy.py"):
            archive.getinfo(required)
    blob=data.getvalue()
    body=(ROOT/"deploy/upgrade-windows.template.ps1").read_text("utf8")
    body=body.replace("@@ALIAS@@",alias).replace("@@PAYLOAD@@",base64.b64encode(blob).decode()).replace("@@SHA@@",hashlib.sha256(blob).hexdigest())
    output.write_text(body,encoding="utf-8-sig")


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("alias")
    p.add_argument("output",type=Path)
    args=p.parse_args()
    build(args.alias,args.output)
