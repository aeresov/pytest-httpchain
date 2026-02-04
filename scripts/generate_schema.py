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


def add_jsonref_support(schema: dict) -> dict:
    """Add support for pytest-httpchain's $ref resolution to the schema.

    pytest-httpchain-jsonref allows $ref at any object level in JSON files,
    resolving external file references at runtime. This function modifies
    the schema to allow objects with $ref as valid alternatives, so VS Code
    and other JSON Schema validators don't complain about missing properties
    when $ref is used.
    """
    if "$defs" not in schema:
        schema["$defs"] = {}

    # Add a definition for reference objects ($include, $merge, or $ref)
    # $include/$merge are preferred as VS Code treats $ref as a JSON Schema keyword
    schema["$defs"]["JsonRef"] = {
        "type": "object",
        "description": "Reference to external JSON file or JSON pointer. Use $include or $merge (preferred) or $ref. Resolved at runtime by pytest-httpchain-jsonref.",
        "properties": {
            "$include": {
                "type": "string",
                "description": "Path to external JSON file, JSON pointer (#/path), or combined (file.json#/path). Preferred over $ref to avoid VS Code conflicts.",
            },
            "$merge": {
                "type": "string",
                "description": "Alias for $include. Path to external JSON file, JSON pointer (#/path), or combined (file.json#/path).",
            },
        },
        "additionalProperties": True,
    }

    # Wrap ALL definitions with anyOf to allow $ref as alternative
    # (jsonref can substitute any element, not just objects)
    for type_name, original_def in list(schema["$defs"].items()):
        if type_name == "JsonRef":
            continue

        schema["$defs"][type_name] = {"anyOf": [{"$ref": "#/$defs/JsonRef"}, original_def]}
        # Preserve title and description at the anyOf level
        if "title" in original_def:
            schema["$defs"][type_name]["title"] = original_def.pop("title")
        if "description" in original_def:
            schema["$defs"][type_name]["description"] = original_def.pop("description")

    # Also handle ALL root-level properties
    for prop_name, prop_def in list(schema.get("properties", {}).items()):
        schema["properties"][prop_name] = {
            "anyOf": [{"$ref": "#/$defs/JsonRef"}, prop_def],
            "title": prop_def.get("title", prop_name),
        }
        if "description" in prop_def:
            schema["properties"][prop_name]["description"] = prop_def.get("description")

    return schema


def main():
    # Generate JSON Schema from the Scenario model
    schema = Scenario.model_json_schema()

    # Add $schema and metadata
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://aeresov.github.io/pytest-httpchain/schema/scenario.schema.json"

    # Add support for pytest-httpchain's $ref resolution
    schema = add_jsonref_support(schema)

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
