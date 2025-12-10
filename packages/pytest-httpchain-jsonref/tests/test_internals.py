"""Tests for internal implementation classes.

Note: These test internal implementation details. The public API is just load_json().
These tests ensure correct behavior of the building blocks.
"""

from pathlib import Path

import pytest
from pytest_httpchain_jsonref.exceptions import ReferenceResolverError
from pytest_httpchain_jsonref.plumbing.circular import CircularDependencyTracker
from pytest_httpchain_jsonref.plumbing.path import PathValidator


class TestCircularDependencyTracker:
    """Tests for CircularDependencyTracker class."""

    def test_first_external_ref_allowed(self):
        """First reference to a file should be allowed."""
        tracker = CircularDependencyTracker()
        path = Path("/test/file.json")
        # Should not raise
        tracker.check_external_ref(path, "/pointer")

    def test_duplicate_external_ref_raises(self):
        """Second reference to same file+pointer should raise."""
        tracker = CircularDependencyTracker()
        path = Path("/test/file.json")
        tracker.check_external_ref(path, "/pointer")
        with pytest.raises(ReferenceResolverError, match="Circular reference detected"):
            tracker.check_external_ref(path, "/pointer")

    def test_same_file_different_pointer_allowed(self):
        """Same file with different pointer should be allowed."""
        tracker = CircularDependencyTracker()
        path = Path("/test/file.json")
        tracker.check_external_ref(path, "/pointer1")
        # Should not raise - different pointer
        tracker.check_external_ref(path, "/pointer2")

    def test_clear_external_ref_allows_revisit(self):
        """After clearing, same ref should be allowed again."""
        tracker = CircularDependencyTracker()
        path = Path("/test/file.json")
        tracker.check_external_ref(path, "/pointer")
        tracker.clear_external_ref(path, "/pointer")
        # Should not raise after clearing
        tracker.check_external_ref(path, "/pointer")

    def test_first_internal_ref_allowed(self):
        """First internal reference should be allowed."""
        tracker = CircularDependencyTracker()
        # Should not raise
        tracker.check_internal_ref("/a/b/c")

    def test_duplicate_internal_ref_raises(self):
        """Second reference to same pointer should raise."""
        tracker = CircularDependencyTracker()
        tracker.check_internal_ref("/a/b/c")
        with pytest.raises(ReferenceResolverError, match="Circular reference detected"):
            tracker.check_internal_ref("/a/b/c")

    def test_clear_internal_ref_allows_revisit(self):
        """After clearing, same internal ref should be allowed again."""
        tracker = CircularDependencyTracker()
        tracker.check_internal_ref("/a/b/c")
        tracker.clear_internal_ref("/a/b/c")
        # Should not raise after clearing
        tracker.check_internal_ref("/a/b/c")

    def test_child_tracker_inherits_external_refs(self):
        """Child tracker should inherit parent's external reference set."""
        parent = CircularDependencyTracker()
        path = Path("/test/file.json")
        parent.check_external_ref(path, "/pointer")

        child = parent.create_child_tracker()
        # Child should see parent's refs
        with pytest.raises(ReferenceResolverError, match="Circular reference detected"):
            child.check_external_ref(path, "/pointer")

    def test_child_tracker_inherits_internal_refs(self):
        """Child tracker should inherit parent's internal reference set."""
        parent = CircularDependencyTracker()
        parent.check_internal_ref("/a/b")

        child = parent.create_child_tracker()
        # Child should see parent's refs
        with pytest.raises(ReferenceResolverError, match="Circular reference detected"):
            child.check_internal_ref("/a/b")

    def test_child_modifications_dont_affect_parent(self):
        """Child tracker modifications should not affect parent."""
        parent = CircularDependencyTracker()
        child = parent.create_child_tracker()

        # Add ref in child
        child.check_external_ref(Path("/child/file.json"), "/pointer")

        # Parent should not see it (can add same ref)
        parent.check_external_ref(Path("/child/file.json"), "/pointer")

    def test_clear_nonexistent_ref_is_safe(self):
        """Clearing a ref that doesn't exist should not raise."""
        tracker = CircularDependencyTracker()
        # Should not raise
        tracker.clear_external_ref(Path("/nonexistent.json"), "/pointer")
        tracker.clear_internal_ref("/nonexistent")


class TestPathValidator:
    """Tests for PathValidator class."""

    def test_parse_empty_pointer(self):
        """Empty pointer should return empty list."""
        result = PathValidator.parse_json_pointer("")
        assert result == []

    def test_parse_root_slash(self):
        """Single slash should return list with empty string."""
        result = PathValidator.parse_json_pointer("/")
        assert result == [""]

    def test_parse_simple_pointer(self):
        """Simple pointer should be split correctly."""
        result = PathValidator.parse_json_pointer("/a/b/c")
        assert result == ["a", "b", "c"]

    def test_parse_pointer_with_tilde_escape(self):
        """~0 should be unescaped to ~."""
        result = PathValidator.parse_json_pointer("/key~0with~0tilde")
        assert result == ["key~with~tilde"]

    def test_parse_pointer_with_slash_escape(self):
        """~1 should be unescaped to /."""
        result = PathValidator.parse_json_pointer("/key~1with~1slash")
        assert result == ["key/with/slash"]

    def test_parse_pointer_with_both_escapes(self):
        """Both escape sequences in one pointer."""
        result = PathValidator.parse_json_pointer("/a~0b~1c")
        assert result == ["a~b/c"]

    def test_parse_pointer_escape_order(self):
        """~1 replaced before ~0, so ~01 becomes ~1."""
        result = PathValidator.parse_json_pointer("/~01")
        assert result == ["~1"]

    def test_parse_pointer_without_leading_slash_raises(self):
        """Pointer without leading slash should raise."""
        with pytest.raises(ReferenceResolverError, match="must start with"):
            PathValidator.parse_json_pointer("no/leading/slash")

    def test_parse_pointer_with_numeric_parts(self):
        """Numeric parts should be returned as strings."""
        result = PathValidator.parse_json_pointer("/0/1/2")
        assert result == ["0", "1", "2"]
        assert all(isinstance(p, str) for p in result)

    def test_validate_ref_path_within_bounds(self, tmp_path):
        """Valid path within traversal limits should be allowed."""
        # Create nested structure
        subdir = tmp_path / "a" / "b"
        subdir.mkdir(parents=True)
        target = tmp_path / "a" / "target.json"
        target.write_text("{}")

        result = PathValidator.validate_ref_path(
            "../target.json",
            subdir,
            tmp_path,
            max_parent_traversal_depth=3,
        )
        assert result == target.resolve()

    def test_validate_ref_path_exceeds_depth(self, tmp_path):
        """Path exceeding max traversal depth should raise."""
        subdir = tmp_path / "a" / "b" / "c"
        subdir.mkdir(parents=True)

        with pytest.raises(ReferenceResolverError, match="exceeds maximum parent traversal depth"):
            PathValidator.validate_ref_path(
                "../../../target.json",
                subdir,
                tmp_path,
                max_parent_traversal_depth=2,
            )

    def test_validate_ref_path_file_not_found(self, tmp_path):
        """Non-existent file should raise."""
        with pytest.raises(ReferenceResolverError, match="not found"):
            PathValidator.validate_ref_path(
                "nonexistent.json",
                tmp_path,
                tmp_path,
                max_parent_traversal_depth=3,
            )

    def test_validate_ref_path_outside_root(self, tmp_path):
        """Path outside root_path should raise."""
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside.json"
        outside.write_text("{}")

        with pytest.raises(ReferenceResolverError, match="not found"):
            PathValidator.validate_ref_path(
                "../outside.json",
                root,
                root,
                max_parent_traversal_depth=3,
            )
