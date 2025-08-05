import pytest
from pytest_httpchain_engine.exceptions import LoaderError
from pytest_httpchain_engine.loader import load_json


class TestLoadJson:
    """Test the load_json() function."""

    def test_ref_self(self, datadir):
        json_file = datadir / "case_ref_self.json"
        result = load_json(json_file)
        assert result["l0-a"]["l1-a"] == 1
        assert result["l0-b"]["l1-a"] == 1

    def test_ref_sibling(self, datadir):
        json_file = datadir / "case_ref_sibling.json"
        result = load_json(json_file)
        assert result["l0-a"]["l1-a"] == 1
        assert result["l0-b"]["l1-a"] == 1

    def test_ref_child(self, datadir):
        json_file = datadir / "case_ref_child.json"
        result = load_json(json_file)
        assert result["l0-a"]["l1-a"] == 1
        assert result["l0-b"]["l1-a"] == 1

    def test_ref_chain(self, datadir):
        json_file = datadir / "case_ref_chain.json"
        result = load_json(json_file)
        assert result["l0-a"]["l1-a"] == 1
        assert result["l0-b"]["l1-a"] == 1

    def test_merge_sibling(self, datadir):
        json_file = datadir / "case_merge_sibling.json"
        result = load_json(json_file)
        assert result["l0-a"]["l1-a"] == 1
        assert result["l0-a"]["l1-c"] == 1

    def test_merge_nested(self, datadir):
        json_file = datadir / "case_merge_nested.json"
        result = load_json(json_file)
        assert result["l0-a"]["l1-a"]["l2-a"] == 1
        assert result["l0-a"]["l1-b"]["l1-a"] == 1

    def test_merge_multi(self, datadir):
        json_file = datadir / "case_merge_multi.json"
        result = load_json(json_file)
        assert result["l0-a"]["l1-a"]["l1-a"] == 1
        assert result["l0-a"]["l1-b"]["l1-a"] == 1
        assert result["l0-a"]["l1-b"]["l2-a"]["l1-c"] == 1

    def test_merge_dict(self, datadir):
        json_file = datadir / "case_merge_dict.json"
        result = load_json(json_file)
        assert result["l0-a"]["l1-d"]["l2-a"] == 1
        assert result["l0-a"]["l1-d"]["l2-b"] == 1

    def test_merge_list(self, datadir):
        json_file = datadir / "case_merge_list.json"
        result = load_json(json_file)
        assert "l2-a" in result["l0-a"]["l1-e"]
        assert "l2-b" in result["l0-a"]["l1-e"]

    def test_merge_conflict_simple(self, datadir):
        json_file = datadir / "case_merge_conflict_simple.json"
        with pytest.raises(LoaderError, match="Merge conflict"):
            load_json(json_file)
