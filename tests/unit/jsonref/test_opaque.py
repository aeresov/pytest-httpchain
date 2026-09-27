"""Position predicates: the consumer can exclude document positions from
reference resolution (``opaque``), or from list concatenation and dict
blending when siblings merge (``atomic``).

pytest-httpchain uses the first for inline JSON Schemas (``verify.body.schema``),
where ``$ref``/``$defs`` are standard JSON Schema vocabulary addressed to the
schema validator — not scenario directives addressed to this resolver — and the
second for ``verify.status``, whose list entries are alternatives, and each
``verify.jmespath`` expectation, which is one expected value.
"""

import pytest

from pytest_httpchain.jsonref.exceptions import ReferenceResolverError
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

    @pytest.mark.parametrize(
        ("sibling", "referenced"),
        [
            pytest.param({"a": 1}, {"b": 2}, id="dicts-do-not-deep-merge"),
            pytest.param([1], [2], id="lists-do-not-concatenate"),
            # JSON equality inside the value too: Python's True == 1 kept one.
            pytest.param({"const": 1}, {"const": True}, id="true-is-not-1-inside"),
        ],
    )
    def test_differing_values_at_opaque_position_conflict(self, create_json_files, sibling, referenced):
        files = create_json_files(
            {
                "main.json": {"outer": {"$merge": "frag.json", "schema": sibling}},
                "frag.json": {"schema": referenced},
            }
        )
        with pytest.raises(ReferenceResolverError, match="Merge conflict at schema"):
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


def status_positions(path: tuple[str | int, ...]) -> bool:
    """Example matcher: any value held by a key named 'status'."""
    return len(path) > 0 and path[-1] == "status"


class TestAtomicPositions:
    """An atomic position merges like a scalar — equal keeps, different is a
    merge conflict — for a list whose entries are alternatives, which
    concatenation would widen. Unlike an opaque one, its content still
    resolves."""

    @pytest.mark.parametrize(
        ("sibling", "referenced"),
        [
            pytest.param([404], ["2xx"], id="lists-do-not-concatenate"),
            pytest.param({"a": 1}, {"b": 2}, id="dicts-do-not-deep-merge"),
        ],
    )
    def test_differing_values_at_atomic_position_conflict(self, create_json_files, sibling, referenced):
        files = create_json_files(
            {
                "main.json": {"outer": {"$merge": "frag.json", "status": sibling}},
                "frag.json": {"status": referenced},
            }
        )
        with pytest.raises(ReferenceResolverError, match="Merge conflict at status"):
            load_json(files["main.json"], atomic=status_positions)

    @pytest.mark.parametrize(
        ("sibling", "referenced"),
        [
            pytest.param([1], [True], id="array-member"),
            pytest.param({"eq": {"active": 1}}, {"eq": {"active": True}}, id="object-member"),
        ],
    )
    def test_equality_is_json_equality_at_any_depth(self, create_json_files, sibling, referenced):
        """Python's ``[True] == [1]`` held, so one side was dropped without a
        conflict: true is not 1 inside a list or an object either."""
        files = create_json_files(
            {
                "main.json": {"outer": {"$merge": "frag.json", "status": sibling}},
                "frag.json": {"status": referenced},
            }
        )
        with pytest.raises(ReferenceResolverError, match="Merge conflict at status"):
            load_json(files["main.json"], atomic=status_positions)

    def test_equal_as_json_keeps(self, create_json_files):
        files = create_json_files(
            {
                "main.json": {"outer": {"$merge": "frag.json", "status": [1.0, {"a": 2}]}},
                "frag.json": {"status": [1, {"a": 2.0}]},
            }
        )
        assert load_json(files["main.json"], atomic=status_positions)["outer"]["status"] == [1, {"a": 2.0}]

    def test_reference_written_at_an_atomic_position_merges_its_siblings(self, create_json_files):
        """The position is kept whole against a value written for it elsewhere.
        A reference written at it with siblings beside it composes one value
        on purpose, so it merges key by key."""
        files = create_json_files(
            {
                "main.json": {"outer": {"status": {"$merge": "frag.json#/range", "lt": 100}}},
                "frag.json": {"range": {"gt": 0}},
            }
        )
        assert load_json(files["main.json"], atomic=status_positions)["outer"]["status"] == {"gt": 0, "lt": 100}

    def test_key_both_sides_give_at_an_atomic_position_must_agree(self, create_json_files):
        files = create_json_files(
            {
                "main.json": {"outer": {"status": {"$merge": "frag.json#/range", "gt": 1}}},
                "frag.json": {"range": {"gt": 0}},
            }
        )
        with pytest.raises(ReferenceResolverError, match="^Merge conflict at gt$"):
            load_json(files["main.json"], atomic=status_positions)

    def test_equal_values_at_atomic_position_merge(self, create_json_files):
        files = create_json_files(
            {
                "main.json": {"outer": {"$merge": "frag.json", "status": ["2xx", 304], "extra": [2]}},
                "frag.json": {"status": ["2xx", 304], "extra": [1]},
            }
        )
        result = load_json(files["main.json"], atomic=status_positions)
        assert result["outer"] == {"status": ["2xx", 304], "extra": [1, 2]}, "lists elsewhere still concatenate"

    def test_atomic_position_content_still_resolves(self, create_json_files):
        files = create_json_files(
            {
                "main.json": {"outer": {"status": {"$include": "codes.json#/ok"}}},
                "codes.json": {"ok": [200, 201]},
            }
        )
        assert load_json(files["main.json"], atomic=status_positions)["outer"]["status"] == [200, 201]

    def test_atomic_position_applies_inside_fragments(self, create_json_files):
        """A fragment's own merge is judged at the reference site's position
        (``outer.status``), not at its place in the fragment (``ok.status``)."""
        files = create_json_files(
            {
                "main.json": {"outer": {"$merge": "frag.json#/ok"}},
                "frag.json": {"base": {"status": ["2xx"]}, "ok": {"$merge": "#/base", "status": [404]}},
            }
        )
        with pytest.raises(ReferenceResolverError, match="Merge conflict at status"):
            load_json(files["main.json"], atomic=lambda path: path == ("outer", "status"))
