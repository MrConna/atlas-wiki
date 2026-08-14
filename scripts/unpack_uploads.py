#!/usr/bin/env python3
"""Safely restore Atlas's flat uploads archive without following links."""

import shutil
import sys
import tarfile
from pathlib import Path, PurePosixPath


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: unpack_uploads.py UPLOADS_TAR DESTINATION", file=sys.stderr)
        return 2
    archive, destination = Path(sys.argv[1]), Path(sys.argv[2])
    with tarfile.open(archive, "r:") as bundle:
        seen = set()
        for member in bundle.getmembers():
            normalized = member.name.removeprefix("./")
            if normalized in {"", "."} and member.isdir():
                continue
            path = PurePosixPath(normalized)
            if path.is_absolute() or ".." in path.parts or len(path.parts) != 1:
                raise ValueError("uploads archive contains an unsafe path")
            if normalized in seen or not member.isfile():
                raise ValueError("uploads archive contains a duplicate or non-regular file")
            seen.add(normalized)
            source = bundle.extractfile(member)
            if source is None:
                raise ValueError(f"cannot read upload {normalized}")
            with (destination / normalized).open("xb") as output:
                shutil.copyfileobj(source, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

