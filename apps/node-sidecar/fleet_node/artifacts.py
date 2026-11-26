"""Capture a bounded previewable file without exporting the rest of its workspace."""

import secrets
from pathlib import Path

from .runner import secure_read
from .volumes import Volume, is_link

VOLUME = "fleet-artifacts"
FORMATS = {".md", ".markdown", ".txt", ".csv", ".tsv", ".png", ".jpg", ".jpeg", ".svg"}


def capture(config, task, target, protected):
    root = config.get("_artifact_spool")
    workspace = Path(task.workspace)
    if (
        not root
        or target.suffix.lower() not in FORMATS
        or not target.is_relative_to(workspace)
    ):
        return None
    try:
        content = secure_read(
            workspace,
            str(target.relative_to(workspace)),
            protected,
            int(config.get("max_artifact_bytes", 16 * 1024 * 1024)),
        )
    except (OSError, ValueError):
        # A removed, replaced or oversized file can no longer be offered for preview.
        return None
    root = Path(root)
    root.mkdir(exist_ok=True, parents=True, mode=0o700)
    if is_link(root):
        return None
    volume = Volume({"name": VOLUME, "path": str(root), "read_only": True})
    volume.target(task.id).mkdir(mode=0o700, exist_ok=True)
    name = f"{task.id}/{secrets.token_hex(8)}-{target.name}"
    with volume.target(name).open("xb") as output:
        output.write(content)
    return {"volume": VOLUME, "path": name, "name": target.name, "size": len(content)}
