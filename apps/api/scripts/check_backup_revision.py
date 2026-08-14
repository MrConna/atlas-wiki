#!/usr/bin/env python3
"""Fail before restore mutation unless a backup revision can upgrade to this code head."""

import json
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.script.revision import RangeNotAncestorError


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: check_backup_revision.py MANIFEST", file=sys.stderr)
        return 2
    manifest = json.loads(Path(sys.argv[1]).read_text())
    backup_revision = manifest.get("alembic_version")
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    if len(heads) != 1:
        print("FAIL: repository must have exactly one Alembic head", file=sys.stderr)
        return 1
    try:
        revision = script.get_revision(backup_revision)
        if revision is None:
            raise ValueError("unknown revision")
        if backup_revision != heads[0]:
            list(script.iterate_revisions(heads[0], backup_revision))
    except (RangeNotAncestorError, ValueError):
        print("FAIL: backup Alembic revision is unknown or newer than this checkout", file=sys.stderr)
        return 1
    print(f"PASS: backup revision {backup_revision} can upgrade to {heads[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
