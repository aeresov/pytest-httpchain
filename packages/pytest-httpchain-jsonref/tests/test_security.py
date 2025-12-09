import pytest
from pytest_httpchain_jsonref.exceptions import ReferenceResolverError
from pytest_httpchain_jsonref.loader import load_json


class TestSecurity:
    def test_max_parent_traversal_exceeded(self, tmp_path):
        """Test that excessive parent directory traversals are blocked"""
        # Create a deeply nested structure
        nested_dir = tmp_path / "a" / "b" / "c" / "d"
        nested_dir.mkdir(parents=True)

        # Create a target file at the top
        target_file = tmp_path / "target.json"
        target_file.write_text('{"value": 42}')

        # Create a file that tries to traverse too many parent directories
        test_file = nested_dir / "test.json"
        # This tries to go up 4 levels, but max_parent_traversal_depth defaults to 3
        test_file.write_text('{"data": {"$ref": "../../../../target.json#/value"}}')

        with pytest.raises(ReferenceResolverError, match="exceeds maximum parent traversal depth"):
            load_json(test_file)

    def test_max_parent_traversal_custom_depth(self, tmp_path):
        """Test custom max_parent_traversal_depth parameter"""
        # Create a nested structure
        nested_dir = tmp_path / "a" / "b"
        nested_dir.mkdir(parents=True)

        # Create a target file at the top
        target_file = tmp_path / "target.json"
        target_file.write_text('{"value": 42}')

        # Create a file that tries to traverse 2 parent directories
        test_file = nested_dir / "test.json"
        test_file.write_text('{"data": {"$ref": "../../target.json#/value"}}')

        # With max_depth=1, this should fail
        with pytest.raises(ReferenceResolverError, match="exceeds maximum parent traversal depth"):
            load_json(test_file, max_parent_traversal_depth=1)

        # With max_depth=2, this should succeed
        result = load_json(test_file, max_parent_traversal_depth=2)
        assert result["data"] == 42

    def test_root_path_enforcement(self, tmp_path):
        """Test that references cannot escape the root_path boundary"""
        # Create a root directory
        root_dir = tmp_path / "root"
        root_dir.mkdir()

        # Create a file outside the root
        outside_file = tmp_path / "outside.json"
        outside_file.write_text('{"secret": "should not access"}')

        # Create a file inside the root that tries to reference outside
        inside_file = root_dir / "inside.json"
        inside_file.write_text('{"data": {"$ref": "../outside.json#/secret"}}')

        # With root_path set to root_dir, this should fail
        with pytest.raises(ReferenceResolverError, match="not found"):
            load_json(inside_file, root_path=root_dir)

    def test_root_path_allows_internal_references(self, tmp_path):
        """Test that root_path allows references within the allowed directory"""
        # Create a root directory with subdirectories
        root_dir = tmp_path / "root"
        subdir = root_dir / "subdir"
        subdir.mkdir(parents=True)

        # Create a file in the root
        root_file = root_dir / "data.json"
        root_file.write_text('{"value": 42}')

        # Create a file in subdir that references the root file
        subdir_file = subdir / "test.json"
        subdir_file.write_text('{"data": {"$ref": "../data.json#/value"}}')

        # This should succeed since both files are within root_path
        result = load_json(subdir_file, root_path=root_dir)
        assert result["data"] == 42

    def test_absolute_path_injection(self, tmp_path):
        """Test that absolute paths in references are handled securely"""
        # This test verifies the package doesn't blindly follow absolute paths
        # Note: The current implementation uses relative paths only,
        # so absolute paths would be treated as filenames relative to the base

        test_file = tmp_path / "test.json"
        # Trying to use an absolute path (will be treated as relative)
        test_file.write_text('{"data": {"$ref": "/etc/passwd"}}')

        # Should fail to find the file (not try to access /etc/passwd)
        with pytest.raises(ReferenceResolverError, match="not found"):
            load_json(test_file)
