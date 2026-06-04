#!/usr/bin/env python3
"""Generate JSON Schema from Pydantic models for IDE support.

Run with: uv run python scripts/generate_schema.py

The schema is written to docs/schema/scenario.schema.json
"""

import json
import subprocess
from pathlib import Path

from pytest_httpchain.schema import build_schema


def find_project_root() -> Path:
    """Find project root by looking for pyproject.toml."""
    try:
        result = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True)
        return Path(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    current = Path.cwd()
    while current != current.parent:
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent

    raise RuntimeError("Could not find project root (no pyproject.toml found)")


def main():
    schema = build_schema()
    project_root = find_project_root()
    output_path = project_root / "docs" / "schema" / "scenario.schema.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w") as f:
        json.dump(schema, f, indent=2)

    print(f"Schema written to: {output_path}")
    print(f"Schema has {len(schema.get('$defs', {}))} definitions")


if __name__ == "__main__":
    main()
