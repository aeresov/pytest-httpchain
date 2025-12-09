import pytest
from pytest_httpchain_jsonref.loader import load_json


class TestCircularReferences:
    def test_circular_external_ref_direct(self, datadir):
        """Test detection of direct circular reference: A.json → B.json → A.json"""
        json_file = datadir / "case_circular_direct_a.json"
        with pytest.raises(RuntimeError, match="Circular reference detected"):
            load_json(json_file)

    def test_circular_external_ref_indirect(self, datadir):
        """Test detection of indirect circular reference: A.json → B.json → C.json → A.json"""
        json_file = datadir / "case_circular_indirect_a.json"
        with pytest.raises(RuntimeError, match="Circular reference detected"):
            load_json(json_file)

    def test_circular_internal_ref(self, datadir):
        """Test detection of circular internal reference: #/a → #/b → #/a"""
        json_file = datadir / "case_circular_internal.json"
        with pytest.raises(RuntimeError, match="Circular reference detected"):
            load_json(json_file)

    def test_self_reference_to_parent(self, datadir):
        """Test detection of self-reference to parent object"""
        json_file = datadir / "case_self_ref_parent.json"
        with pytest.raises(RuntimeError, match="Circular reference detected"):
            load_json(json_file)
