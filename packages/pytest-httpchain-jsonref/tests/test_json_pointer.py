import pytest
from pytest_httpchain_jsonref.exceptions import ReferenceResolverError
from pytest_httpchain_jsonref.loader import load_json


class TestJsonPointerBasics:
    """Tests for basic JSON pointer functionality per RFC 6901."""

    def test_pointer_without_leading_slash_is_invalid(self, datadir):
        """JSON pointer after # must start with '/'."""
        json_file = datadir / "case_pointer_no_slash.json"
        with pytest.raises(ReferenceResolverError, match="Invalid .ref format"):
            load_json(json_file)

    def test_tilde_escapes_decode_correctly(self, datadir):
        """~0 decodes to ~ and ~1 decodes to /."""
        json_file = datadir / "case_pointer_escape.json"
        result = load_json(json_file)
        assert result["ref1"] == 1
        assert result["ref2"] == 2

    def test_array_index_navigation(self, datadir):
        """Numeric path segments navigate into arrays."""
        json_file = datadir / "case_pointer_array.json"
        result = load_json(json_file)
        assert result["ref0"] == "first"
        assert result["ref1"] == "second"
        assert result["ref2"] == "third"

    def test_bare_hash_without_slash_is_invalid(self, datadir):
        """#foo is invalid - must be #/foo."""
        json_file = datadir / "case_pointer_empty.json"
        with pytest.raises(ReferenceResolverError, match="Invalid .ref format"):
            load_json(json_file)

    def test_special_characters_in_keys(self, datadir):
        """Keys with special chars (space, @, dots) work correctly."""
        json_file = datadir / "case_pointer_special_chars.json"
        result = load_json(json_file)
        assert result["ref1"] == 1
        assert result["ref2"] == 2
        assert result["ref3"] == 3
        assert result["ref4"] == 4

    def test_unicode_characters_in_keys(self, datadir):
        """Keys with unicode characters work correctly."""
        json_file = datadir / "case_pointer_unicode.json"
        result = load_json(json_file)
        assert result["ref1"] == "Japanese"
        assert result["ref2"] == "French"
        assert result["ref3"] == "emoji"

    def test_deeply_nested_navigation(self, datadir):
        """Pointer can navigate multiple levels deep."""
        json_file = datadir / "case_pointer_nested.json"
        result = load_json(json_file)
        assert result["ref"] == "deep"

    def test_empty_string_as_key(self, datadir):
        """Empty string is a valid key, accessed via #/."""
        json_file = datadir / "case_pointer_empty_key.json"
        result = load_json(json_file)
        assert result["ref"] == "empty key"

    def test_numeric_string_keys_in_objects(self, datadir):
        """Numeric strings like '0', '42' are valid object keys."""
        json_file = datadir / "case_pointer_numeric_keys.json"
        result = load_json(json_file)
        assert result["ref0"] == "zero"
        assert result["ref1"] == "one"
        assert result["ref42"] == "forty-two"


class TestJsonPointerEdgeCases:
    """Tests for JSON pointer edge cases per RFC 6901."""

    def test_array_index_with_leading_zero_is_rejected(self, create_json_file):
        """Array index '01' is invalid per RFC 6901."""
        file = create_json_file(
            "test.json",
            {"items": ["a", "b", "c"], "ref": {"$ref": "#/items/01"}},
        )
        with pytest.raises(ReferenceResolverError, match="leading zeros"):
            load_json(file)

    def test_array_index_007_style_is_rejected(self, create_json_file):
        """Array index '007' is invalid per RFC 6901."""
        file = create_json_file(
            "test.json",
            {
                "items": ["a", "b", "c", "d", "e", "f", "g", "h"],
                "ref": {"$ref": "#/items/007"},
            },
        )
        with pytest.raises(ReferenceResolverError, match="leading zeros"):
            load_json(file)

    def test_tilde_01_decodes_to_literal_tilde_1(self, create_json_file):
        """~01 decodes to ~1 (tilde escape processed first)."""
        file = create_json_file(
            "test.json",
            {"~1": "tilde-one-key", "ref": {"$ref": "#/~01"}},
        )
        result = load_json(file)
        assert result["ref"] == "tilde-one-key"

    def test_tilde_00_decodes_to_literal_tilde_0(self, create_json_file):
        """~00 decodes to ~0 (key containing literal ~0)."""
        file = create_json_file(
            "test.json",
            {"~0": "tilde-zero-key", "ref": {"$ref": "#/~00"}},
        )
        result = load_json(file)
        assert result["ref"] == "tilde-zero-key"

    def test_leading_zeros_valid_for_object_keys(self, create_json_file):
        """Leading zeros restriction only applies to arrays, not object keys."""
        file = create_json_file(
            "test.json",
            {"data": {"007": "james bond"}, "ref": {"$ref": "#/data/007"}},
        )
        result = load_json(file)
        assert result["ref"] == "james bond"

    def test_empty_string_key_in_nested_path(self, create_json_file):
        """Double slash in path accesses empty string key."""
        file = create_json_file(
            "test.json",
            {"a": {"": {"b": "value"}}, "ref": {"$ref": "#/a//b"}},
        )
        result = load_json(file)
        assert result["ref"] == "value"

    def test_key_with_both_slash_and_tilde(self, create_json_file):
        """Key 'a/b~c' requires both escape sequences."""
        file = create_json_file(
            "test.json",
            {"a/b~c": "complex-key-value", "ref": {"$ref": "#/a~1b~0c"}},
        )
        result = load_json(file)
        assert result["ref"] == "complex-key-value"
