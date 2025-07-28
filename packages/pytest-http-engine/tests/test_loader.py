import json
import logging

from pytest_http_engine.loader import load_json


class TestLoadJson:
    """Test the load_json() function."""

    def test_mixed_merge(self, datadir):
        json_file = datadir / "mixed_merge.json"
        result = load_json(json_file)
        logging.info(json.dumps(result))
        assert "request" in result["stages"][0]
        assert "request" in result["stages"][1]
