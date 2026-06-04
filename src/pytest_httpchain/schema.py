"""Build the JSON Schema for pytest-httpchain scenario files.

Shared by the ``pytest-httpchain schema`` CLI command and
``scripts/generate_schema.py``. The schema is derived from the Pydantic
``Scenario`` model and augmented so editors accept pytest-httpchain's
``$ref``/``$include``/``$merge`` reference directives at any object level.
"""

from typing import Any

from pytest_httpchain_models import Scenario

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_ID = "https://aeresov.github.io/pytest-httpchain/schema/scenario.schema.json"


def _add_jsonref_support(schema: dict[str, Any]) -> dict[str, Any]:
    """Allow ``$include``/``$merge``/``$ref`` objects as alternatives anywhere.

    pytest-httpchain-jsonref can substitute any element at runtime, so each
    definition and root property is wrapped in an ``anyOf`` that also accepts a
    reference object — otherwise editors flag missing required properties when a
    reference is used.
    """
    if "$defs" not in schema:
        schema["$defs"] = {}

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

    for type_name, original_def in list(schema["$defs"].items()):
        if type_name == "JsonRef":
            continue
        schema["$defs"][type_name] = {"anyOf": [{"$ref": "#/$defs/JsonRef"}, original_def]}
        if "title" in original_def:
            schema["$defs"][type_name]["title"] = original_def.pop("title")
        if "description" in original_def:
            schema["$defs"][type_name]["description"] = original_def.pop("description")

    for prop_name, prop_def in list(schema.get("properties", {}).items()):
        schema["properties"][prop_name] = {
            "anyOf": [{"$ref": "#/$defs/JsonRef"}, prop_def],
            "title": prop_def.get("title", prop_name),
        }
        if "description" in prop_def:
            schema["properties"][prop_name]["description"] = prop_def.get("description")

    return schema


def build_schema() -> dict[str, Any]:
    """Return the augmented JSON Schema dict for the ``Scenario`` model."""
    schema = Scenario.model_json_schema()
    schema["$schema"] = SCHEMA_DIALECT
    schema["$id"] = SCHEMA_ID
    return _add_jsonref_support(schema)
