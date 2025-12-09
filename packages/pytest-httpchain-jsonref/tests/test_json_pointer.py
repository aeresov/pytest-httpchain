import pytest
from pytest_httpchain_jsonref.exceptions import ReferenceResolverError
from pytest_httpchain_jsonref.loader import load_json


class TestJsonPointer:
    def test_pointer_without_leading_slash(self, datadir):
        """Test error when JSON pointer doesn't start with '/'"""
        json_file = datadir / "case_pointer_no_slash.json"
        with pytest.raises(ReferenceResolverError, match="Invalid .ref format"):
            load_json(json_file)

    def test_pointer_escape_sequences(self, datadir):
        """Test JSON pointer escape sequences: ~0 → ~ and ~1 → /"""
        json_file = datadir / "case_pointer_escape.json"
        result = load_json(json_file)
        assert result["ref1"] == 1
        assert result["ref2"] == 2

    def test_pointer_array_access(self, datadir):
        """Test JSON pointer array element access"""
        json_file = datadir / "case_pointer_array.json"
        result = load_json(json_file)
        assert result["ref0"] == "first"
        assert result["ref1"] == "second"
        assert result["ref2"] == "third"

    def test_empty_pointer(self, datadir):
        """Test that bare # (without /) is treated as invalid format"""
        json_file = datadir / "case_pointer_empty.json"
        with pytest.raises(ReferenceResolverError, match="Invalid .ref format"):
            load_json(json_file)

    def test_pointer_special_characters(self, datadir):
        """Test JSON pointers with special characters in keys"""
        json_file = datadir / "case_pointer_special_chars.json"
        result = load_json(json_file)
        assert result["ref1"] == 1
        assert result["ref2"] == 2
        assert result["ref3"] == 3
        assert result["ref4"] == 4

    def test_pointer_unicode_characters(self, datadir):
        """Test JSON pointers with Unicode characters"""
        json_file = datadir / "case_pointer_unicode.json"
        result = load_json(json_file)
        assert result["ref1"] == "Japanese"
        assert result["ref2"] == "French"
        assert result["ref3"] == "emoji"

    def test_pointer_to_nested_structure(self, datadir):
        """Test JSON pointers navigating deeply nested structures"""
        json_file = datadir / "case_pointer_nested.json"
        result = load_json(json_file)
        assert result["ref"] == "deep"

    def test_pointer_empty_string_key(self, datadir):
        """Test JSON pointer with empty string as key"""
        json_file = datadir / "case_pointer_empty_key.json"
        result = load_json(json_file)
        assert result["ref"] == "empty key"

    def test_pointer_numeric_string_keys(self, datadir):
        """Test JSON pointers with numeric string keys in objects"""
        json_file = datadir / "case_pointer_numeric_keys.json"
        result = load_json(json_file)
        assert result["ref0"] == "zero"
        assert result["ref1"] == "one"
        assert result["ref42"] == "forty-two"

    def test_external_ref_whole_file(self, datadir):
        """Test external reference without hash/pointer (references whole file)"""
        json_file = datadir / "case_external_empty_pointer.json"
        result = load_json(json_file)
        assert result["data"]["value"] == 42
        assert result["data"]["name"] == "external"
