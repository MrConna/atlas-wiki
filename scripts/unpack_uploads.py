#!/usr/bin/env python3
"""Safely restore Atlas's flat uploads archive without following links."""

import shutil
import os
import sys
import tarfile
from pathlib import Path, PurePosixPath


MAX_FILE_BYTES = int(os.getenv("ATLAS_RESTORE_MAX_UPLOAD_FILE_BYTES", str(100 * 1024**2)))
MAX_TOTAL_BYTES = int(os.getenv("ATLAS_RESTORE_MAX_UPLOAD_BYTES", str(100 * 1024**3)))
MAX_FILES = int(os.getenv("ATLAS_RESTORE_MAX_UPLOAD_FILES", "1000000"))


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: unpack_uploads.py UPLOADS_TAR DESTINATION", file=sys.stderr)
        return 2
    archive, destination = Path(sys.argv[1]), Path(sys.argv[2])
    with tarfile.open(archive, "r:") as bundle:
        seen = set()
        validated = []
        total_size = 0
        for member in bundle.getmembers():
            normalized = member.name.removeprefix("./")
            if normalized in {"", "."} and member.isdir():
                continue
            path = PurePosixPath(normalized)
            if path.is_absolute() or ".." in path.parts or len(path.parts) != 1:
                raise ValueError("uploads archive contains an unsafe path")
            if normalized in seen or not member.isfile():
                raise ValueError("uploads archive contains a duplicate or non-regular file")
            if member.size < 0 or member.size > MAX_FILE_BYTES:
                raise ValueError("upload exceeds the configured per-file size limit")
            seen.add(normalized)
            validated.append((member, normalized))
            total_size += member.size
            if len(validated) > MAX_FILES or total_size > MAX_TOTAL_BYTES:
                raise ValueError("uploads archive exceeds the configured total size limit")
        for member, normalized in validated:
            source = bundle.extractfile(member)
            if source is None:
                raise ValueError(f"cannot read upload {normalized}")
            with (destination / normalized).open("xb") as output:
                shutil.copyfileobj(source, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
