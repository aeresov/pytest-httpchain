import json
import logging

from pytest_http_engine.loader import load_json


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
