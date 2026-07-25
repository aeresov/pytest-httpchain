"""The JSON Schema for scenario files: the ``Scenario`` model's schema, widened
so editors accept a reference directive wherever a named type or a root-level
property is expected."""

from typing import Any

from pytest_httpchain.models import Scenario

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_ID = "https://aeresov.github.io/pytest-httpchain/schema/scenario.schema.json"


def _one_of_to_any_of(node: Any) -> None:
    """Recursively rename ``oneOf`` to ``anyOf`` in place."""
    if isinstance(node, dict):
        if "oneOf" in node:
            node["anyOf"] = node.pop("oneOf")
        for value in node.values():
            _one_of_to_any_of(value)
    elif isinstance(node, list):
        for item in node:
            _one_of_to_any_of(item)


def _add_jsonref_support(schema: dict[str, Any]) -> dict[str, Any]:
    """Accept reference objects at named-type and root-property sites.

    The resolver can substitute any element, but wrapping every inline subschema
    would balloon the output, so anonymous nested schemas are left untouched: a
    reference still resolves there at runtime, it is just not described.
    """
    if "$defs" not in schema:
        schema["$defs"] = {}

    schema["$defs"]["JsonRef"] = {
        "type": "object",
        "description": "Reference to external JSON file or JSON pointer. Use $include or $merge (preferred) or $ref. Resolved when the scenario file is loaded.",
        "properties": {
            "$include": {
                "type": "string",
                "description": "Path to external JSON file, JSON pointer (#/path), or combined (file.json#/path). Preferred over $ref to avoid VS Code conflicts.",
            },
            "$merge": {
                "type": "string",
                "description": "Alias for $include. Path to external JSON file, JSON pointer (#/path), or combined (file.json#/path).",
            },
            "$ref": {
                "type": "string",
                "description": "Legacy alias for $include. May conflict with VS Code's own $ref handling.",
            },
        },
        # Without at least one directive key this branch would match EVERY
        # object, silencing the strict alternative in the surrounding anyOf.
        "anyOf": [
            {"required": ["$include"]},
            {"required": ["$merge"]},
            {"required": ["$ref"]},
        ],
        "additionalProperties": True,
    }

    # Pydantic emits oneOf for tagged unions, which would reject a reference
    # object for matching the JsonRef branch of every member. anyOf keeps the
    # same accept set otherwise, since members forbid each other's tag fields.
    _one_of_to_any_of(schema)

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

    # The root forbids extra keys, so keys handled before model validation must
    # be declared: "$schema" and the directives the loader resolves.
    schema.setdefault("properties", {})
    schema["properties"]["$schema"] = {
        "type": "string",
        "description": "URL of this schema, for editor as-you-type validation. Dropped during model validation.",
    }
    for directive, directive_def in schema["$defs"]["JsonRef"]["properties"].items():
        schema["properties"][directive] = directive_def

    return schema


def build_schema() -> dict[str, Any]:
    """Return the augmented JSON Schema dict for the ``Scenario`` model."""
    schema = Scenario.model_json_schema()
    schema["$schema"] = SCHEMA_DIALECT
    schema["$id"] = SCHEMA_ID
    return _add_jsonref_support(schema)
