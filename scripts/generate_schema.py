"""Generate JSON Schema from Pydantic models for IDE support.

Run with: uv run python scripts/generate_schema.py [--check]

The schema is written to docs/schema/scenario.schema.json, and a versioned copy,
whose $id names the project version, to docs/schema/v<version>/scenario.schema.json
when that copy does not exist yet; an existing versioned copy is never rewritten.

With --check nothing is written: the script exits 1, naming each file, when a
file a plain run would write is missing or differs from what it would write,
byte for byte, and 0 when every such file is current. A plain run writes the
platform's newlines, CRLF on Windows, as text mode does, and --check expects
them, so it passes right after a plain run on every platform, and on a Git for
Windows checkout (core.autocrlf=true), which has the committed files in CRLF.
A Windows checkout with LF files (core.autocrlf=input or false) fails --check
until a plain run rewrites them.
"""

import argparse
import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

from pytest_httpchain.schema import build_schema

# This script lives in <root>/scripts/, so the root is one level up. Deriving it
# from __file__ rather than the CWD or `git rev-parse` keeps regeneration
# independent of where it is invoked from — and of which checkout is current.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = PROJECT_ROOT / "docs" / "schema" / "scenario.schema.json"


def _schema_bytes(schema: dict[str, Any]) -> bytes:
    # The indented JSON and a trailing newline, as `Path.write_text(text,
    # encoding="utf-8")` writes them: UTF-8, each newline the platform's
    # (`os.linesep`). A plain run writes these bytes and --check compares each
    # file with them, so the two cannot disagree on any platform.
    text = json.dumps(schema, indent=2) + "\n"
    return text.replace("\n", os.linesep).encode("utf-8")


def schema_files(schema: dict[str, Any]) -> tuple[dict[Path, bytes], Path]:
    """The files a plain run writes, each with its bytes, in the order it writes
    them, and the versioned copy's path, whether it is among them or not.

    The versioned copy has an immutable-per-release $id under
    /schema/v<version>/. Copies accumulate in the repo, so every released schema
    URL stays resolvable while the unversioned URL tracks latest; on a version
    bump --check flags the new copy until it is committed. An existing
    versioned copy is never rewritten, so it is among the files only while it
    is missing: the pyproject version stays at the last release while main
    accumulates unreleased schema changes, so regenerating it would silently
    repoint a "pinned" release URL at unreleased content.
    """
    files = {SCHEMA_PATH: _schema_bytes(schema)}
    version = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    versioned_path = SCHEMA_PATH.parent / f"v{version}" / SCHEMA_PATH.name
    if not versioned_path.exists():
        versioned_schema = dict(schema)
        versioned_schema["$id"] = schema["$id"].replace("/schema/", f"/schema/v{version}/")
        files[versioned_path] = _schema_bytes(versioned_schema)
    return files, versioned_path


def check_files(files: dict[Path, bytes]) -> int:
    """Write nothing; name each file that is missing or does not hold its bytes
    exactly. The exit code: 1 if any, else 0."""
    stale = 0
    for path, data in files.items():
        if not path.exists():
            problem = "missing"
        elif path.read_bytes() != data:
            problem = "out of date"
        else:
            continue
        stale += 1
        print(f"{path.relative_to(PROJECT_ROOT).as_posix()}: {problem}", file=sys.stderr)
    if stale:
        print("Regenerate with `uv run python scripts/generate_schema.py` and commit the result.", file=sys.stderr)
        return 1
    print("Schema files are up to date.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the scenario JSON Schema under docs/schema/ from the Pydantic models.")
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1, naming each file, if a file a plain run would write is missing or out of date")
    args = parser.parse_args(argv)

    schema = build_schema()
    files, versioned_path = schema_files(schema)
    if args.check:
        return check_files(files)

    for path, data in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    if versioned_path in files:
        print(f"Versioned copy: {versioned_path}")
    else:
        print(f"Versioned copy exists, left untouched: {versioned_path}")
    print(f"Schema written to: {SCHEMA_PATH}")
    print(f"Schema has {len(schema.get('$defs', {}))} definitions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
