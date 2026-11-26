"""Trusted launcher for the WASI guest, invoked with an immutable staging directory."""

import sys
import tempfile
from importlib import metadata
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from wasi_python import Grant, run


def pytest_archive(path):
    # Package only interpreter code from the pinned distribution inventory.
    # User site directories, plugins, .pth files and host config are never mounted.
    with ZipFile(path, "w", ZIP_DEFLATED) as bundle:
        for name in ("pytest", "pluggy", "packaging", "iniconfig", "pygments"):
            dist = metadata.distribution(name)
            for member in dist.files or ():
                if member.parts[0] not in {
                    "pytest",
                    "_pytest",
                    "py",
                    "py.py",
                    "pluggy",
                    "packaging",
                    "iniconfig",
                    "pygments",
                }:
                    continue
                if member.suffix == ".py":
                    bundle.write(dist.locate_file(member), member.as_posix())
            bundle.writestr(
                f"{name}-{dist.version}.dist-info/METADATA",
                f"Metadata-Version: 2.1\nName: {name}\nVersion: {dist.version}\n",
            )


def main():
    language, stage, script, *args = sys.argv[1:]
    # Guest starts in its virtual root; chdir to the read-only bundle before
    # executing through runpy so relative declared input paths work normally.
    startup = "import os,runpy,sys; os.chdir('/work'); sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')"
    grants = [Grant(stage, "/work")]
    with tempfile.TemporaryDirectory(prefix="fleet-testlib-") as libs:
        if language == "pytest":
            pytest_archive(Path(libs) / "packages.zip")
            grants.append(Grant(libs, "/libtest"))
            startup = (
                "import os,sys; os.chdir('/work'); sys.path.insert(0,'/libtest/packages.zip'); "
                "import pytest; raise SystemExit(pytest.main(['-q','--capture=sys','--disable-plugin-autoload',"
                "'-p','no:cacheprovider','-p','no:faulthandler','-p','no:logging','--color=no','/work/'+sys.argv[1]]))"
            )
        result = run(
            ["-B", "-c", startup, script, *args],
            root=False,
            grants=grants,
            env={},
            deadline_s=25 if language == "pytest" else 15,
            fuel=50_000_000_000 if language == "pytest" else 4_000_000_000,
            memory_bytes=192 * 1024 * 1024,
            output_bytes=30 * 1024,
            max_file_calls=12000,
            max_open_files=48,
        )
    sys.stdout.buffer.write(result.stdout + result.stderr)
    if result.trapped or result.truncated:
        print("\nGuest limit:", result.trapped or result.truncated)
        return 1
    return int(result.exit_code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
