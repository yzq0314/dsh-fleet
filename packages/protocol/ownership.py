"""Process-lifetime exclusive ownership of a single Fleet SQLite database."""

import os
from pathlib import Path
from typing import BinaryIO


def claim_database(path: str | Path, role: str) -> BinaryIO | None:
    """Return the held lock handle. Closing it or exiting the process releases ownership."""
    if str(path) == ":memory:":
        return None
    filename = Path(path).resolve()
    filename.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = filename.with_suffix(filename.suffix + ".lock").open("a+b")
    try:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError(
                f"Cannot acquire {role} database ownership; check for another instance"
            ) from error
        descriptor = os.open(
            filename, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        return handle
    except BaseException:
        handle.close()
        raise
