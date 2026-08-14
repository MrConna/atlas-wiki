#!/usr/bin/env python3
"""Safely unpack the fixed Atlas backup bundle layout."""

import shutil
import sys
import tarfile
from pathlib import Path


EXPECTED = {"manifest.json", "SHA256SUMS", "database.dump", "uploads.tar"}


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: unpack_backup.py ARCHIVE DESTINATION", file=sys.stderr)
        return 2
    archive, destination = Path(sys.argv[1]), Path(sys.argv[2])
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)) or set(names) != EXPECTED:
            raise ValueError("backup bundle has unexpected or duplicate members")
        if any(not member.isfile() for member in members):
            raise ValueError("backup bundle members must be regular files")
        for member in members:
            source = bundle.extractfile(member)
            if source is None:
                raise ValueError(f"cannot read backup member {member.name}")
            target = destination / member.name
            with target.open("xb") as output:
                shutil.copyfileobj(source, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

