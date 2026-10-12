import os
from pathlib import Path, PurePosixPath
import tempfile


def confined_path(root: Path, relative: str) -> Path:
    """Filesystem containment only; every caller must check domain ACL separately."""
    if not relative or "\\" in relative or ":" in relative or "\x00" in relative:
        raise ValueError("Invalid storage path")
    parts = PurePosixPath(relative)
    if parts.is_absolute() or any(part in (".", "..") for part in relative.split("/")):
        raise ValueError("Invalid storage path")
    base = root.resolve()
    path = (base / Path(*parts.parts)).resolve()
    if not path.is_relative_to(base) or path == base:
        raise ValueError("Storage path escapes root")
    return path


def atomic_write(root: Path, relative: str, data: bytes) -> Path:
    """Atomic single-blob replacement; DB/file orchestration belongs to file domain.

    Storage directories are private to the service account. This is not an ACL or
    a distributed locking API; future DAV/file writes must serialize metadata.
    """
    destination = confined_path(root, relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination
