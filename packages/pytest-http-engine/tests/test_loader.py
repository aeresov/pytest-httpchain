import json
import stat
from unittest.mock import patch

import pytest
from jsonref import JsonRefError
from pytest_http_engine.loader import LoaderError, load_json


class TestLoadJson:
    """Test the load_json() function."""

    def test_load_simple_json(self, datadir):
        """Test loading a simple valid JSON file."""
        json_file = datadir / "valid_simple.json"
        result = load_json(json_file)

        assert isinstance(result, dict)
        assert result["key"] == "value"
        assert result["number"] == 42

    def test_load_json_with_references(self, datadir):
        """Test loading JSON file with $ref references."""
        json_file = datadir / "valid_with_refs.json"
        result = load_json(json_file)

        assert isinstance(result, dict)
        assert len(result["data"]) == 2
        # First item should be resolved from shared_stage.json
        assert result["data"][0]["shared_key"] == "shared_value"
        # Second item should be from main file
        assert result["data"][1]["local_key"] == "local_value"
        # $ref keys should be removed
        assert "$ref" not in str(result)

    def test_invalid_json_syntax(self, datadir):
        """Test loading file with invalid JSON syntax raises LoaderError."""
        json_file = datadir / "invalid_json.json"

        with pytest.raises(LoaderError, match="Invalid JSON"):
            load_json(json_file)

    def test_missing_referenced_file(self, datadir):
        """Test loading file with missing $ref raises LoaderError."""
        json_file = datadir / "missing_ref_proper.json"

        with pytest.raises(LoaderError, match="Invalid JSON reference"):
            load_json(json_file)

    def test_invalid_json_in_referenced_file(self, datadir):
        """Test loading file with invalid JSON in referenced file raises LoaderError."""
        json_file = datadir / "with_invalid_ref.json"

        with pytest.raises(LoaderError, match="Invalid JSON reference"):
            load_json(json_file)

    def test_file_permission_error(self, tmp_path):
        """Test loading file with permission error raises LoaderError."""
        json_file = tmp_path / "test.json"
        json_file.write_text('{"valid": "json"}')

        # Remove read permissions
        json_file.chmod(stat.S_IWRITE)

        with pytest.raises(LoaderError, match="Error reading file"):
            load_json(json_file)

    def test_unicode_decode_error(self, datadir):
        """Test loading file with invalid UTF-8 encoding raises LoaderError."""
        json_file = datadir / "invalid_utf8.json"

        with pytest.raises(LoaderError, match="Error reading file"):
            load_json(json_file)

    def test_nonexistent_file(self, tmp_path):
        """Test loading nonexistent file raises FileNotFoundError (not caught by load_json)."""
        json_file = tmp_path / "nonexistent.json"

        with pytest.raises(FileNotFoundError):
            load_json(json_file)

    @patch("jsonref.replace_refs")
    def test_jsonref_error(self, mock_replace_refs, datadir):
        """Test jsonref.JsonRefError is handled properly."""
        json_file = datadir / "valid_simple.json"
        mock_replace_refs.side_effect = JsonRefError("Test jsonref error", "fake_ref")

        with pytest.raises(LoaderError, match="Invalid JSON reference"):
            load_json(json_file)

    @patch("jsonref.replace_refs")
    def test_os_error_in_jsonref(self, mock_replace_refs, datadir):
        """Test OSError during jsonref processing is handled properly."""
        json_file = datadir / "valid_simple.json"
        mock_replace_refs.side_effect = OSError("Test OS error")

        with pytest.raises(LoaderError, match="Error reading referenced file"):
            load_json(json_file)

    @patch("mergedeep.merge")
    def test_merge_type_error(self, mock_merge, datadir):
        """Test TypeError during merge raises LoaderError."""
        json_file = datadir / "valid_simple.json"
        mock_merge.side_effect = TypeError("Cannot merge types")

        with pytest.raises(LoaderError, match="Unable to merge with references"):
            load_json(json_file)

    def test_non_json_content(self, datadir):
        """Test loading file with non-JSON content raises LoaderError."""
        json_file = datadir / "binary_like.json"

        with pytest.raises(LoaderError, match="Invalid JSON"):
            load_json(json_file)

    def test_empty_file(self, tmp_path):
        """Test loading empty file raises LoaderError."""
        json_file = tmp_path / "empty.json"
        json_file.write_text("")

        with pytest.raises(LoaderError, match="Invalid JSON"):
            load_json(json_file)

    def test_refs_removed_from_nested_structures(self, tmp_path):
        """Test that $ref keys are properly removed from nested data structures."""
        # Create a referenced file
        ref_file = tmp_path / "nested_ref.json"
        ref_file.write_text('{"nested": {"value": "from_ref"}}')

        # Create main file with nested $ref
        main_file = tmp_path / "main.json"
        main_content = {"data": {"items": [{"$ref": "nested_ref.json"}, {"normal": "value"}]}}
        main_file.write_text(json.dumps(main_content))

        result = load_json(main_file)

        # Ensure no $ref keys remain anywhere in the structure
        result_str = json.dumps(result)
        assert "$ref" not in result_str

        # But the referenced content should be merged
        assert result["data"]["items"][0]["nested"]["value"] == "from_ref"
        assert result["data"]["items"][1]["normal"] == "value"

    def test_complex_merge_scenario(self, tmp_path):
        """Test complex merge scenario with multiple references and nested data."""
        # Create base config reference
        base_config = tmp_path / "base_config.json"
        base_config.write_text(json.dumps({"timeout": 30, "retries": 3, "headers": {"User-Agent": "test-client"}}))

        # Create auth reference
        auth_config = tmp_path / "auth_config.json"
        auth_config.write_text(json.dumps({"headers": {"Authorization": "Bearer token"}, "verify_ssl": True}))

        # Create main file that references both
        main_file = tmp_path / "complex_merge.json"
        main_content = {"name": "complex_test", "config": {"$ref": "base_config.json"}, "auth": {"$ref": "auth_config.json"}, "custom": {"debug": True}}
        main_file.write_text(json.dumps(main_content))

        result = load_json(main_file)

        # Verify structure and merging
        assert result["name"] == "complex_test"
        assert result["config"]["timeout"] == 30
        assert result["config"]["headers"]["User-Agent"] == "test-client"
        assert result["auth"]["headers"]["Authorization"] == "Bearer token"
        assert result["auth"]["verify_ssl"] is True
        assert result["custom"]["debug"] is True

        # Ensure no $ref keys remain
        assert "$ref" not in json.dumps(result)
