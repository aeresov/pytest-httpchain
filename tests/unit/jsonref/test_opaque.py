"""Opaque-subtree support: the consumer can exclude document positions from
reference resolution.

pytest-httpchain uses this for inline JSON Schemas (``verify.body.schema``),
where ``$ref``/``$defs`` are standard JSON Schema vocabulary addressed to the
schema validator — not scenario directives addressed to this resolver.
"""

from pytest_httpchain.jsonref.loader import load_json


def schema_positions(path: tuple[str | int, ...]) -> bool:
    """Example matcher: any value held by a key named 'schema'."""
    return len(path) > 0 and path[-1] == "schema"


class TestOpaqueSubtrees:
    def test_opaque_subtree_passes_through_verbatim(self, create_json_files):
        files = create_json_files(
            {
                "main.json": {
                    "a": {"$ref": "frag.json"},
                    "b": {"schema": {"$ref": "#/$defs/item", "$defs": {"item": {"type": "string"}}}},
                },
                "frag.json": {"value": 42},
            }
        )
        result = load_json(files["main.json"], opaque=schema_positions)
        assert result["a"] == {"value": 42}, "resolution outside opaque positions must be unaffected"
        assert result["b"]["schema"] == {"$ref": "#/$defs/item", "$defs": {"item": {"type": "string"}}}

    def test_opaque_position_applies_inside_fragments(self, create_json_files):
        """Paths compose across file boundaries: a fragment spliced at position
        p has its content judged at p + <fragment-relative path>."""
        files = create_json_files(
            {
                "main.json": {"outer": {"$ref": "frag.json"}},
                "frag.json": {"schema": {"$ref": "#/nope"}, "y": {"$ref": "#/z"}, "z": 2},
            }
        )
        result = load_json(files["main.json"], opaque=schema_positions)
        assert result["outer"]["schema"] == {"$ref": "#/nope"}, "no pointer error: the schema subtree must not be resolved"
        assert result["outer"]["y"] == 2, "fragment-internal refs outside opaque positions still resolve"

    def test_opaque_list_positions(self, create_json_files):
        """List indices participate in the composed path."""
        files = create_json_files(
            {
                "main.json": {"items": [{"schema": {"$ref": "#/x"}}, {"other": {"$ref": "#/x"}}], "x": 1},
            }
        )
        result = load_json(files["main.json"], opaque=schema_positions)
        assert result["items"][0]["schema"] == {"$ref": "#/x"}
        assert result["items"][1]["other"] == 1

    def test_without_opaque_schema_positions_resolve_as_before(self, create_json_files):
        files = create_json_files(
            {
                "main.json": {"b": {"schema": {"$ref": "frag.json"}}},
                "frag.json": {"type": "object"},
            }
        )
        result = load_json(files["main.json"])
        assert result["b"]["schema"] == {"type": "object"}


class TestOpaqueMergeAtomicity:
    """Sibling merging must not blend content INSIDE an opaque position: the
    subtree is verbatim foreign vocabulary, so two differing values at the same
    opaque position are a merge conflict (no-silent-contradiction), and equal
    values merge as anywhere else."""

    def test_differing_values_at_opaque_position_conflict(self, create_json_files):
        files = create_json_files(
            {
                "main.json": {"outer": {"$merge": "frag.json", "schema": {"a": 1}}},
                "frag.json": {"schema": {"b": 2}},
            }
        )
        import pytest

        from pytest_httpchain.jsonref.exceptions import ReferenceResolverError

        with pytest.raises(ReferenceResolverError, match="conflict"):
            load_json(files["main.json"], opaque=schema_positions)

    def test_equal_values_at_opaque_position_merge(self, create_json_files):
        files = create_json_files(
            {
                "main.json": {"outer": {"$merge": "frag.json", "schema": {"a": 1}, "extra": True}},
                "frag.json": {"schema": {"a": 1}},
            }
        )
        result = load_json(files["main.json"], opaque=schema_positions)
        assert result["outer"] == {"schema": {"a": 1}, "extra": True}
