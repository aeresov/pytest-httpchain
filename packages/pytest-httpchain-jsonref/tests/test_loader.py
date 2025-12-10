import pytest
from pytest_httpchain_jsonref.exceptions import ReferenceResolverError
from pytest_httpchain_jsonref.loader import load_json


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
