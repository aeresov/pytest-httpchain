import pytest

from pytest_httpchain.jsonref.exceptions import ReferenceResolverError
from pytest_httpchain.jsonref.loader import load_json


class TestRefResolution:
    """Tests for basic $ref resolution."""

    @pytest.mark.parametrize(
        "case_file",
        [
            "case_ref_self.json",
            "case_ref_sibling.json",
            "case_ref_child.json",
            "case_ref_chain.json",
        ],
    )
    def test_ref_resolves_to_referenced_value(self, datadir, case_file):
        """$ref should be replaced with the referenced value."""
        result = load_json(datadir / case_file)
        assert result["source"]["value"] == 42
        assert result["target"]["value"] == 42

    @pytest.mark.parametrize(
        "case_file",
        [
            "case_ref_self.json",
            "case_ref_sibling.json",
            "case_ref_child.json",
            "case_ref_chain.json",
        ],
    )
    def test_ref_key_removed_after_resolution(self, datadir, case_file):
        """$ref key should not appear in resolved output."""
        result = load_json(datadir / case_file)
        assert "$ref" not in result["target"]


class TestMerging:
    """Tests for $ref merging with sibling properties."""

    def test_merge_ref_with_sibling_properties(self, datadir):
        """When $ref has sibling properties, they should be merged into the result."""
        result = load_json(datadir / "case_merge_sibling.json")
        assert result["result"]["value"] == 42
        assert result["result"]["extra_value"] == 42
        assert "$ref" not in result["result"]

    def test_merge_preserves_nested_structure(self, datadir):
        """Nested objects with refs should all be resolved."""
        result = load_json(datadir / "case_merge_nested.json")
        assert result["result"]["original"]["value"] == 42
        assert result["result"]["from_ref"]["value"] == 42

    def test_merge_multiple_refs_in_nested_structure(self, datadir):
        """Multiple $refs at different nesting levels should all resolve."""
        result = load_json(datadir / "case_merge_multi.json")
        assert result["result"]["first"]["value"] == 42
        assert result["result"]["second"]["value"] == 42
        assert result["result"]["second"]["nested"]["extra_value"] == 42

    def test_deep_merge_combines_nested_dicts(self, datadir):
        """Deep merge should combine nested dictionaries from ref and siblings."""
        result = load_json(datadir / "case_merge_dict.json")
        assert result["result"]["merged"]["local"] == 42
        assert result["result"]["merged"]["other"] == 42

    def test_merge_concatenates_lists(self, datadir):
        """Lists from ref and siblings should be concatenated."""
        result = load_json(datadir / "case_merge_list.json")
        assert "from_local" in result["result"]["items"]
        assert "from_sibling" in result["result"]["items"]

    def test_merge_conflict_raises_error(self, datadir):
        """Conflicting scalar values should raise ReferenceResolverError."""
        with pytest.raises(ReferenceResolverError, match="Merge conflict"):
            load_json(datadir / "case_merge_conflict_simple.json")

    def test_merge_with_empty_object_ref(self, create_json_file):
        """Merging with empty object ref should preserve only sibling properties."""
        create_json_file("empty.json", {})
        file = create_json_file(
            "main.json",
            {"data": {"$ref": "empty.json", "extra": "value"}},
        )
        result = load_json(file)
        assert result["data"] == {"extra": "value"}
        assert "$ref" not in result["data"]

    def test_ref_to_null_value(self, create_json_file):
        """Reference to null value should resolve to null."""
        create_json_file("ref.json", {"value": None})
        file = create_json_file(
            "main.json",
            {"data": {"$ref": "ref.json#/value"}},
        )
        result = load_json(file)
        assert result["data"] is None


class TestSchemaKeyPassthrough:
    """ "$schema" keys are content to the loader — never stripped at any level.

    Stripping here once corrupted a JSON Schema document pulled into a host
    via $include (its dialect declaration vanished). Tolerating "$schema" is
    the consumer's job (pytest-httpchain models drop it during validation).
    """

    def test_schema_key_preserved_in_main_document(self, create_json_file):
        file = create_json_file(
            "main.json",
            {"$schema": "https://example.test/schema.json", "value": 42},
        )
        result = load_json(file)
        assert result["$schema"] == "https://example.test/schema.json"
        assert result["value"] == 42

    def test_schema_key_preserved_in_included_fragment(self, create_json_files):
        """An $include'd JSON Schema document must keep its dialect declaration."""
        files = create_json_files(
            {
                "draft07.schema.json": {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"},
                "main.json": {"verify": {"schema": {"$include": "draft07.schema.json"}}},
            }
        )
        result = load_json(files["main.json"])
        assert result["verify"]["schema"]["$schema"] == "http://json-schema.org/draft-07/schema#"
        assert result["verify"]["schema"]["type"] == "object"


class TestExternalReferences:
    """Tests for external file references."""

    def test_ref_to_whole_external_file(self, create_json_file):
        """$ref without pointer should include entire external file."""
        create_json_file("external.json", {"value": 42, "name": "external"})
        file = create_json_file("main.json", {"data": {"$ref": "external.json"}})
        result = load_json(file)
        assert result["data"]["value"] == 42
        assert result["data"]["name"] == "external"
        assert "$ref" not in result["data"]

    def test_same_file_can_be_referenced_multiple_times(self, create_json_file):
        """Multiple refs to same file should not trigger circular reference error."""
        create_json_file("shared.json", {"common": "value"})
        file = create_json_file(
            "main.json",
            {
                "first": {"$ref": "shared.json#/common"},
                "second": {"$ref": "shared.json#/common"},
                "third": {"$ref": "shared.json"},
            },
        )
        result = load_json(file)
        assert result["first"] == "value"
        assert result["second"] == "value"
        assert result["third"]["common"] == "value"

    def test_multiple_different_external_refs(self, create_json_file):
        """Document can reference multiple different external files."""
        create_json_file("a.json", {"a": 1})
        create_json_file("b.json", {"b": 2})
        file = create_json_file(
            "main.json",
            {
                "ref_a": {"$ref": "a.json"},
                "ref_b": {"$ref": "b.json"},
            },
        )
        result = load_json(file)
        assert result["ref_a"]["a"] == 1
        assert result["ref_b"]["b"] == 2


class TestPrimitiveReferences:
    """Tests for references that resolve to primitive JSON values."""

    def test_ref_resolves_to_string(self, create_json_file):
        """$ref can resolve to a string value."""
        file = create_json_file(
            "test.json",
            {"source": "hello world", "ref": {"$ref": "#/source"}},
        )
        result = load_json(file)
        assert result["ref"] == "hello world"

    def test_ref_resolves_to_integer(self, create_json_file):
        """$ref can resolve to an integer value."""
        file = create_json_file(
            "test.json",
            {"source": 42, "ref": {"$ref": "#/source"}},
        )
        result = load_json(file)
        assert result["ref"] == 42

    def test_ref_resolves_to_float(self, create_json_file):
        """$ref can resolve to a float value."""
        file = create_json_file(
            "test.json",
            {"source": 3.14159, "ref": {"$ref": "#/source"}},
        )
        result = load_json(file)
        assert result["ref"] == 3.14159

    def test_ref_resolves_to_true(self, create_json_file):
        """$ref can resolve to boolean true."""
        file = create_json_file(
            "test.json",
            {"source": True, "ref": {"$ref": "#/source"}},
        )
        result = load_json(file)
        assert result["ref"] is True

    def test_ref_resolves_to_false(self, create_json_file):
        """$ref can resolve to boolean false."""
        file = create_json_file(
            "test.json",
            {"source": False, "ref": {"$ref": "#/source"}},
        )
        result = load_json(file)
        assert result["ref"] is False

    def test_ref_resolves_to_null(self, create_json_file):
        """$ref can resolve to null."""
        file = create_json_file(
            "test.json",
            {"source": None, "ref": {"$ref": "#/source"}},
        )
        result = load_json(file)
        assert result["ref"] is None


class TestArrayReferences:
    """Tests for references involving arrays."""

    def test_ref_resolves_to_array(self, create_json_file):
        """$ref can resolve to an entire array."""
        file = create_json_file(
            "test.json",
            {"source": [1, 2, 3], "ref": {"$ref": "#/source"}},
        )
        result = load_json(file)
        assert result["ref"] == [1, 2, 3]

    def test_ref_to_array_element_by_index(self, create_json_file):
        """$ref can target specific array element by index."""
        file = create_json_file(
            "test.json",
            {"source": ["first", "second", "third"], "ref": {"$ref": "#/source/1"}},
        )
        result = load_json(file)
        assert result["ref"] == "second"

    def test_refs_inside_array_elements(self, create_json_file):
        """$ref objects inside arrays should be resolved."""
        file = create_json_file(
            "test.json",
            {
                "template": {"type": "item"},
                "items": [
                    {"$ref": "#/template"},
                    {"$ref": "#/template"},
                    {"name": "custom"},
                ],
            },
        )
        result = load_json(file)
        assert result["items"][0] == {"type": "item"}
        assert result["items"][1] == {"type": "item"}
        assert result["items"][2] == {"name": "custom"}
        assert "$ref" not in result["items"][0]

    def test_ref_to_nested_array_element(self, create_json_file):
        """$ref can navigate into nested arrays."""
        file = create_json_file(
            "test.json",
            {"matrix": [[1, 2], [3, 4], [5, 6]], "ref": {"$ref": "#/matrix/1/0"}},
        )
        result = load_json(file)
        assert result["ref"] == 3

    def test_ref_to_property_inside_array_element(self, create_json_file):
        """$ref can target object property inside array element."""
        file = create_json_file(
            "test.json",
            {
                "users": [{"name": "Alice"}, {"name": "Bob"}],
                "first_user_name": {"$ref": "#/users/0/name"},
            },
        )
        result = load_json(file)
        assert result["first_user_name"] == "Alice"


@pytest.mark.parametrize("directive", ["$ref", "$include", "$merge"])
class TestDirectiveAliases:
    """All three spellings ($ref/$include/$merge) must behave identically.

    $include/$merge are preferred (they avoid VS Code JSON Schema conflicts);
    $ref is the legacy spelling. Parametrizing here keeps their coverage even.
    """

    def test_resolves_internal_reference(self, create_json_file, directive):
        """Directive works for internal references."""
        file = create_json_file(
            "test.json",
            {"source": {"value": 42}, "target": {directive: "#/source"}},
        )
        result = load_json(file)
        assert result["target"]["value"] == 42
        assert directive not in result["target"]

    def test_resolves_external_reference(self, create_json_file, directive):
        """Directive works for external file references."""
        create_json_file("external.json", {"data": "from external"})
        file = create_json_file(
            "test.json",
            {"imported": {directive: "external.json"}},
        )
        result = load_json(file)
        assert result["imported"]["data"] == "from external"

    def test_with_pointer(self, create_json_file, directive):
        """Directive works with JSON pointers into an external file."""
        create_json_file("external.json", {"nested": {"value": 99}})
        file = create_json_file(
            "test.json",
            {"target": {directive: "external.json#/nested/value"}},
        )
        result = load_json(file)
        assert result["target"] == 99

    def test_with_sibling_merge(self, create_json_file, directive):
        """Directive supports deep merging with sibling properties."""
        file = create_json_file(
            "test.json",
            {
                "base": {"a": 1, "b": 2},
                "extended": {directive: "#/base", "c": 3},
            },
        )
        result = load_json(file)
        assert result["extended"] == {"a": 1, "b": 2, "c": 3}

    def test_in_array(self, create_json_file, directive):
        """Directive works inside arrays."""
        file = create_json_file(
            "test.json",
            {
                "template": {"type": "item"},
                "items": [{directive: "#/template"}, {directive: "#/template"}],
            },
        )
        result = load_json(file)
        assert result["items"] == [{"type": "item"}, {"type": "item"}]

    def test_non_string_value_raises(self, create_json_file, directive):
        """A non-string directive value must raise (M38)."""
        file = create_json_file(
            "test.json",
            {"target": {directive: ["#/source"]}},
        )
        with pytest.raises(ReferenceResolverError, match="must be a string"):
            load_json(file)


class TestMultipleDirectives:
    """An object may carry at most one reference directive (M37)."""

    @pytest.mark.parametrize(
        "directives",
        [
            ("$ref", "$include"),
            ("$ref", "$merge"),
            ("$include", "$merge"),
            ("$ref", "$include", "$merge"),
        ],
    )
    def test_multiple_directives_raise(self, create_json_file, directives):
        """More than one directive key in one object must raise, not silently drop."""
        create_json_file("external.json", {"value": 42})
        file = create_json_file(
            "test.json",
            {"target": dict.fromkeys(directives, "external.json")},
        )
        with pytest.raises(ReferenceResolverError, match="Multiple reference directives"):
            load_json(file)


class TestNullMergeSemantics:
    """Null is not an override: the no-last-wins promise holds for null like
    any other value. A null/value pair in the same position is a conflict."""

    def test_null_sibling_conflicts_with_value(self, create_json_file):
        create_json_file("base.json", {"value": 42})
        file = create_json_file("main.json", {"data": {"$ref": "base.json", "value": None}})
        with pytest.raises(ReferenceResolverError, match="Merge conflict at value"):
            load_json(file)

    def test_value_sibling_conflicts_with_null_base(self, create_json_file):
        create_json_file("base.json", {"value": None})
        file = create_json_file("main.json", {"data": {"$ref": "base.json", "value": 42}})
        with pytest.raises(ReferenceResolverError, match="Merge conflict at value"):
            load_json(file)

    def test_equal_values_are_not_a_conflict(self, create_json_file):
        create_json_file("base.json", {"value": 42})
        file = create_json_file("main.json", {"data": {"$ref": "base.json", "value": 42}})
        assert load_json(file)["data"] == {"value": 42}

    def test_equal_nulls_are_not_a_conflict(self, create_json_file):
        create_json_file("base.json", {"value": None})
        file = create_json_file("main.json", {"data": {"$ref": "base.json", "value": None}})
        assert load_json(file)["data"] == {"value": None}
