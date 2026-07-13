import pytest

from pytest_httpchain.jsonref.exceptions import ReferenceResolverError
from pytest_httpchain.jsonref.loader import load_json


class TestCircularReferences:
    def test_circular_external_ref_direct(self, datadir):
        """Test detection of direct circular reference: A.json → B.json → A.json"""
        json_file = datadir / "case_circular_direct_a.json"
        with pytest.raises(ReferenceResolverError, match="Circular reference detected"):
            load_json(json_file)

    def test_circular_external_ref_indirect(self, datadir):
        """Test detection of indirect circular reference: A.json → B.json → C.json → A.json"""
        json_file = datadir / "case_circular_indirect_a.json"
        with pytest.raises(ReferenceResolverError, match="Circular reference detected"):
            load_json(json_file)

    def test_circular_internal_ref(self, datadir):
        """Test detection of circular internal reference: #/a → #/b → #/a"""
        json_file = datadir / "case_circular_internal.json"
        with pytest.raises(ReferenceResolverError, match="Circular reference detected"):
            load_json(json_file)

    def test_self_reference_to_parent(self, datadir):
        """Test detection of self-reference to parent object"""
        json_file = datadir / "case_self_ref_parent.json"
        with pytest.raises(ReferenceResolverError, match="Circular reference detected"):
            load_json(json_file)

    def test_internal_pointer_reused_across_documents_is_not_circular(self, create_json_files):
        """Two documents that each use the internal pointer #/a are NOT a cycle.

        Internal JSON pointers are document-local: main's #/a and ext's #/a are
        distinct. Regression for H5, where internal refs were tracked by pointer
        string only and inherited into the child tracker used for external
        files, raising a phantom "Circular reference detected: #/a".
        """
        files = create_json_files(
            {
                "main.json": {
                    "x": {"$include": "#/a"},
                    "a": {"sub": {"$include": "ext.json"}},
                },
                "ext.json": {
                    "a": {"val": 1},
                    "ref": {"$include": "#/a"},
                },
            }
        )
        result = load_json(files["main.json"])
        assert result["x"]["sub"]["ref"] == {"val": 1}
        assert result["a"]["sub"]["ref"] == {"val": 1}
