import pytest
from pytest_httpchain_jsonref.exceptions import ReferenceResolverError
from pytest_httpchain_jsonref.loader import load_json


class TestErrorHandling:
    def test_missing_reference_file(self, datadir):
        """Test error when referenced file doesn't exist"""
        json_file = datadir / "case_missing_ref.json"
        with pytest.raises(ReferenceResolverError, match="not found"):
            load_json(json_file)

    def test_malformed_json_in_reference(self, datadir):
        """Test error when referenced file contains invalid JSON"""
        json_file = datadir / "case_malformed_ref.json"
        with pytest.raises(ReferenceResolverError, match="Failed to load external reference"):
            load_json(json_file)

    def test_malformed_json_in_main_file(self, datadir):
        """Test error when main file contains invalid JSON"""
        json_file = datadir / "case_malformed_main.json"
        with pytest.raises(ReferenceResolverError, match="Failed to load JSON"):
            load_json(json_file)

    def test_invalid_json_pointer(self, datadir):
        """Test error when JSON pointer points to non-existent path"""
        json_file = datadir / "case_invalid_pointer.json"
        with pytest.raises(ReferenceResolverError, match="Invalid JSON pointer"):
            load_json(json_file)

    def test_invalid_json_pointer_in_external_file(self, datadir):
        """Test error when JSON pointer in external reference doesn't exist"""
        json_file = datadir / "case_invalid_external_pointer.json"
        with pytest.raises(ReferenceResolverError, match="Invalid JSON pointer"):
            load_json(json_file)

    def test_merge_non_dict_with_siblings(self, datadir):
        """Test error when trying to merge sibling properties with non-dict reference"""
        json_file = datadir / "case_merge_non_dict.json"
        with pytest.raises(ReferenceResolverError, match="Cannot merge non-dict reference"):
            load_json(json_file)

    def test_array_index_out_of_bounds(self, datadir):
        """Test error when array index in JSON pointer is out of bounds"""
        json_file = datadir / "case_array_out_of_bounds.json"
        with pytest.raises(ReferenceResolverError, match="Invalid JSON pointer"):
            load_json(json_file)

    def test_invalid_array_index(self, datadir):
        """Test error when using non-numeric string as array index"""
        json_file = datadir / "case_array_invalid_index.json"
        with pytest.raises(ReferenceResolverError, match="Invalid JSON pointer"):
            load_json(json_file)

    def test_file_permission_error(self, tmp_path):
        """Test handling of file permission errors"""
        # Create a file
        restricted_file = tmp_path / "restricted.json"
        restricted_file.write_text('{"value": 42}')

        # Make it unreadable (may not work on all systems)
        try:
            restricted_file.chmod(0o000)

            # Create a test file that references it
            test_file = tmp_path / "test.json"
            test_file.write_text('{"data": {"$ref": "restricted.json#/value"}}')

            with pytest.raises(ReferenceResolverError):
                load_json(test_file)

        finally:
            # Restore permissions for cleanup
            try:
                restricted_file.chmod(0o644)
            except Exception:
                pass
