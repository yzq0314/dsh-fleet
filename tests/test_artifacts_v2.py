import os
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest
from fastapi import HTTPException

from fleet_node.artifacts import capture
from fleet_node.svg_preview import sanitize_svg
from fleet_node.volumes import Volume


def test_static_svg_is_image_only():
    raw = b'<svg xmlns="http://www.w3.org/2000/svg"><script>unexpected</script><foreignObject><html/></foreignObject><rect id="shape" width="12" height="15" onload="unexpected"/><use href="#shape"/><image href="https://example.test/image"/><path style="fill:url(https://example.test/p);stroke:#fff"/></svg>'
    result = sanitize_svg(raw)
    assert (
        b"unexpected" not in result
        and b"example.test" not in result
        and b"foreignObject" not in result
    )
    assert b"#shape" in result and ET.fromstring(result).tag.endswith("svg")
    with pytest.raises(HTTPException):
        sanitize_svg(b'<!DOCTYPE svg [<!ENTITY a "bad">]><svg/>')
    with pytest.raises(HTTPException):
        sanitize_svg(b"x" * (512 * 1024 + 1))


def test_snapshot_bound_to_validated_workspace_output(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    original = work / "result.md"
    original.write_text("# hello", encoding="utf8")
    config = {"_artifact_spool": str(tmp_path / "spool")}
    job = SimpleNamespace(workspace=str(work), id="job-1")
    record = capture(config, job, original, set())
    original.write_text("changed")
    volume = Volume({"name": record["volume"], "path": config["_artifact_spool"]})
    assert volume.target(record["path"]).read_text() == "# hello"
    other = tmp_path / "private.md"
    other.write_text("private")
    assert capture(config, job, other, set()) is None
    assert capture(config, job, original, {work}) is None
    twin = work / "linked.md"
    os.link(other, twin)
    assert capture(config, job, twin, set()) is None
