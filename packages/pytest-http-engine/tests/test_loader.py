import json
import logging

import pytest

from pytest_http_engine.loader import LoaderError, load_json


class TestLoadJson:
    """Test the load_json() function."""

    def test_refs_combinations(self, datadir):
        json_file = datadir / "refs.json"
        result = load_json(json_file)
        logging.info(json.dumps(result))
        assert result["simple"]["a"] == "refs"
        assert result["ref_sibling_simple"]["a"] == "sibling"
        assert result["ref_sibling_nested"]["a"] == "nested_one"
        assert result["ref_nested_simple"]["a"] == "nested_one"
        assert result["ref_nested_sibling"]["a"] == "sibling"
        assert result["ref_nested_nested"]["a"] == "nested_two"

    def test_merge_combinations(self, datadir):
        json_file = datadir / "merge.json"
        result = load_json(json_file)
        logging.info(json.dumps(result))
        # simple
        assert result["simple"]["a"] == "merge"
        assert result["simple"]["b"]["a"] == "sibling"
        # sibling props
        assert result["sibling_props"]["a"] == "sibling"
        assert result["sibling_props"]["b"] == "merge"
        # multilevel
        assert result["multilevel"]["a"] == "nested_two"
        assert result["multilevel"]["b"]["a"] == "nested_one"
        assert result["multilevel"]["c"]["a"] == "sibling"
        assert result["multilevel"]["d"]["a"] == "nested_two"
        # nested_props
        assert result["nested_props"]["a"]["b"] == "complex_a_b"
        assert result["nested_props"]["a"]["c"] == "complex_a_c"
        assert result["nested_props"]["a"]["d"]["a"] == "nested_two"

    def test_merge_conflict(self, datadir):
        """Test that conflicting $refs raise LoaderError."""
        json_file = datadir / "conflict.json"
        with pytest.raises(LoaderError, match="Merge conflict"):
            load_json(json_file)

    def test_multiple_conflicts(self, datadir):
        """Test different types of merge conflicts."""
        json_file = datadir / "multi_conflict.json"
        with pytest.raises(LoaderError, match="Merge conflict: Cannot merge str with list"):
            load_json(json_file)

    def test_same_type_merge_conflict(self, datadir):
        """Test that merging same types with different values raises conflicts."""
        json_file = datadir / "same_type_merge.json"
        # Now same-type overrides are also conflicts
        with pytest.raises(LoaderError, match="Merge conflict: Cannot override str value"):
            load_json(json_file)

    def test_value_override_behavior(self, datadir):
        """Test that value overrides are conflicts but dict merges are allowed."""
        json_file = datadir / "value_override.json"
        # String override should fail
        with pytest.raises(LoaderError, match="Merge conflict: Cannot override str value"):
            load_json(json_file)

    def test_allowed_dict_merge(self, datadir):
        """Test that dict merges are still allowed."""
        json_file = datadir / "allowed_merge.json"
        result = load_json(json_file)

        # Both settings should be present after merge
        assert result["dict_merge"]["config"]["existing_setting"] == "original"
        assert result["dict_merge"]["config"]["new_setting"] == "value"

    def test_list_merge_conflict(self, datadir):
        """Test that list merges are treated as conflicts by default."""
        json_file = datadir / "list_merge.json"
        # Lists are treated as value overrides by default, thus conflicts
        with pytest.raises(LoaderError, match="Merge conflict: Cannot override list value"):
            load_json(json_file)

    def test_list_merge_allowed(self, datadir):
        """Test that lists can be merged when merge_lists=True."""
        json_file = datadir / "list_merge.json"
        result = load_json(json_file, merge_lists=True)

        # Lists should be appended
        assert result["list_merge"]["items"] == [1, 2, 3, 4, 5, 6]

        # Nested lists should also be appended
        assert result["nested_list_merge"]["data"]["items"] == ["old1", "old2", "new1", "new2"]

    def test_list_type_conflicts(self, datadir):
        """Test that type mismatches with lists still raise conflicts."""
        json_file = datadir / "list_type_conflict.json"

        # Even with merge_lists=True, type mismatches should conflict
        with pytest.raises(LoaderError, match="Merge conflict: Cannot merge"):
            load_json(json_file, merge_lists=True)
