#!/usr/bin/env python3
"""Generate JSON Schema from Pydantic models for IDE support.

Run with: uv run python scripts/generate_schema.py

The schema is written to docs/schema/scenario.schema.json
"""

import json
import tomllib
from pathlib import Path

from pytest_httpchain.schema import build_schema

# This script lives in <root>/scripts/, so the root is one level up. Deriving it
# from __file__ rather than the CWD or `git rev-parse` keeps regeneration
# independent of where it is invoked from — and of which checkout is current.
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main():
    schema = build_schema()
    project_root = PROJECT_ROOT
    schema_dir = project_root / "docs" / "schema"
    schema_dir.mkdir(parents=True, exist_ok=True)

    # Byte-stable output (explicit encoding, trailing newline) so the CI drift
    # check can compare the committed files with `git diff --exit-code`.
    output_path = schema_dir / "scenario.schema.json"
    output_path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")

    # Versioned copy with an immutable-per-release $id under /schema/v<version>/.
    # Copies accumulate in the repo, so every released schema URL stays
    # resolvable while the unversioned URL tracks latest; on a version bump the
    # CI drift check flags the new directory until it is committed. An existing
    # versioned copy is never rewritten: the pyproject version stays at the last
    # release while main accumulates unreleased schema changes, so regenerating
    # it would silently repoint a "pinned" release URL at unreleased content.
    version = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    versioned_path = schema_dir / f"v{version}" / "scenario.schema.json"
    if versioned_path.exists():
        print(f"Versioned copy exists, left untouched: {versioned_path}")
    else:
        versioned_path.parent.mkdir(parents=True, exist_ok=True)
        versioned_schema = dict(schema)
        versioned_schema["$id"] = schema["$id"].replace("/schema/", f"/schema/v{version}/")
        versioned_path.write_text(json.dumps(versioned_schema, indent=2) + "\n", encoding="utf-8")
        print(f"Versioned copy: {versioned_path}")

    print(f"Schema written to: {output_path}")
    print(f"Schema has {len(schema.get('$defs', {}))} definitions")


if __name__ == "__main__":
    main()
