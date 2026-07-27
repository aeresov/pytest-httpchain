"""Build the JSON Schema for pytest-httpchain scenario files.

Shared by the ``pytest-httpchain schema`` CLI command and
``scripts/generate_schema.py``. The schema is derived from the Pydantic
``Scenario`` model and augmented so editors accept pytest-httpchain's
``$ref``/``$include``/``$merge`` reference directives wherever a named type
(a ``$defs`` entry) or a root-level scenario property is expected. Inline,
anonymous nested schemas are not wrapped.
"""

from typing import Any

from pydantic.json_schema import GenerateJsonSchema, JsonSchemaValue
from pydantic_core import core_schema

from pytest_httpchain.models import Scenario

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_ID = "https://aeresov.github.io/pytest-httpchain/schema/scenario.schema.json"


class _AnyOfTaggedUnions(GenerateJsonSchema):
    """Emit tagged unions as ``anyOf`` instead of pydantic's ``oneOf``.

    A reference object matches the JsonRef branch of EVERY union member (each
    ``$defs`` entry is wrapped in ``_add_jsonref_support``), which ``oneOf``
    counts as "valid under more than one" and rejects. ``anyOf`` keeps the
    same accept set otherwise: members forbid each other's tag fields, so a
    non-reference object can never match two branches. Overriding the
    generator hook scopes the rename to exactly the tagged-union sites,
    instead of a blanket post-hoc walk over the emitted document.
    """

    def tagged_union_schema(self, schema: core_schema.TaggedUnionSchema) -> JsonSchemaValue:
        json_schema = super().tagged_union_schema(schema)
        if "oneOf" in json_schema:
            json_schema["anyOf"] = json_schema.pop("oneOf")
        return json_schema


def _add_jsonref_support(schema: dict[str, Any]) -> dict[str, Any]:
    """Allow ``$include``/``$merge``/``$ref`` objects as alternatives at named
    type and root-property sites.

    pytest_httpchain.jsonref can substitute any element at runtime, but wrapping
    *every* inline subschema would balloon the schema, so only each ``$defs``
    definition and each root-level scenario property is wrapped in an ``anyOf``
    that also accepts a reference object — otherwise editors flag missing
    required properties when a reference is used at one of those sites. Inline,
    anonymous nested schemas are left untouched; a reference used there is still
    resolved at runtime but is not described to the editor.
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

    # The Scenario model forbids extra keys, so the root carries
    # additionalProperties: false. Keys that are legitimate in a scenario
    # *file* but handled before model validation must be declared explicitly:
    # "$schema" (editor metadata, stripped by the loader) and the reference
    # directives (resolved by the loader, supported at the document root).
    schema.setdefault("properties", {})
    schema["properties"]["$schema"] = {
        "type": "string",
        "description": "URL of this schema, for editor as-you-type validation. Dropped during model validation.",
    }
    # The loop copies all three directive properties ($include/$merge/$ref)
    # verbatim from the JsonRef definition — including $ref's legacy-alias
    # description.
    for directive, directive_def in schema["$defs"]["JsonRef"]["properties"].items():
        schema["properties"][directive] = directive_def

    return schema


def build_schema() -> dict[str, Any]:
    """Return the augmented JSON Schema dict for the ``Scenario`` model."""
    schema = Scenario.model_json_schema(schema_generator=_AnyOfTaggedUnions)
    schema["$schema"] = SCHEMA_DIALECT
    schema["$id"] = SCHEMA_ID
    return _add_jsonref_support(schema)
