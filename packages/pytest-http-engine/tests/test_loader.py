import json
import logging

from pytest_http_engine.loader import load_json


class TestLoadJson:
    """Test the load_json() function."""

    def test_mixed_merge(self, datadir):
        json_file = datadir / "mixed_merge.json"
        result = load_json(json_file)
        logging.info(json.dumps(result))
        assert "altec_status_text" in result["stages"][1]["save"]["vars"]
        assert "another_var" in result["stages"][1]["save"]["vars"]
        assert "url" in result["stages"][0]["request"]
        assert "format" in result["stages"][2]["request"]["params"]
        assert "data" in result["stages"][2]["request"]["params"]
