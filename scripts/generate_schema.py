#!/usr/bin/env python3
"""Generate JSON Schema from Pydantic models for IDE support.

Run with: uv run python scripts/generate_schema.py

The schema is written to docs/schema/scenario.schema.json
"""

import json
import subprocess
from pathlib import Path

from pytest_httpchain_models import Scenario


def find_project_root() -> Path:
    """Find project root by looking for pyproject.toml."""
    # Try git root first
    try:
        result = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True)
        return Path(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    # Fall back to searching from current directory
    current = Path.cwd()
    while current != current.parent:
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent

    raise RuntimeError("Could not find project root (no pyproject.toml found)")


def main():
    # Generate JSON Schema from the Scenario model
    schema = Scenario.model_json_schema()

    # Add $schema and metadata
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://pytest-httpchain.readthedocs.io/schema/scenario.json"

    # Write to docs folder
    project_root = find_project_root()
    output_path = project_root / "docs" / "schema" / "scenario.schema.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w") as f:
        json.dump(schema, f, indent=2)

    print(f"Schema written to: {output_path}")
    print(f"Schema has {len(schema.get('$defs', {}))} definitions")


if __name__ == "__main__":
    main()
