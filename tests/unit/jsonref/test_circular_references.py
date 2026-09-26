import pytest

from pytest_httpchain.jsonref.exceptions import ReferenceResolverError
from pytest_httpchain.jsonref.loader import load_json


@pytest.mark.parametrize(
    "case_file",
    [
        pytest.param("case_circular_direct_a.json", id="external-a-b-a"),
        pytest.param("case_circular_indirect_a.json", id="external-a-b-c-a"),
        pytest.param("case_circular_internal.json", id="internal-a-b-a"),
        pytest.param("case_self_ref_parent.json", id="internal-child-to-parent"),
        pytest.param("case_empty_ref.json", id="empty-ref-is-whole-document"),
    ],
)
def test_cycle_detected(datadir, case_file):
    with pytest.raises(ReferenceResolverError, match="Circular reference detected"):
        load_json(datadir / case_file)


EXT = {"a": {"val": 1}, "ref": {"val": 1}}


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        pytest.param(
            {"main.json": {"x": {"$ref": "b.json#/p"}}, "b.json": {"p": {"$ref": "b.json#/q"}, "q": 1}},
            {"x": 1},
            id="same-file-different-pointer",
        ),
        pytest.param(
            {
                "main.json": {"x": {"$include": "#/a"}, "a": {"sub": {"$include": "ext.json"}}},
                "ext.json": {"a": {"val": 1}, "ref": {"$include": "#/a"}},
            },
            {"x": {"sub": EXT}, "a": {"sub": EXT}},
            id="internal-pointer-reused-across-documents",
        ),
    ],
)
def test_nested_reuse_is_not_a_cycle(create_json_files, files, expected):
    """External refs are keyed by (file, pointer), and internal pointers are
    document-local: main's #/a and ext's #/a are distinct. Regression for H5,
    where internal refs were inherited into the child tracker used for external
    files, raising a phantom "Circular reference detected: #/a"."""
    assert load_json(create_json_files(files)["main.json"]) == expected
