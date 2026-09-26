"""JSON pointer navigation through load_json(), per RFC 6901."""

import pytest

from pytest_httpchain.jsonref.exceptions import ReferenceResolverError
from pytest_httpchain.jsonref.loader import load_json


@pytest.mark.parametrize(
    ("key", "pointer"),
    [
        pytest.param("a~b", "#/a~0b", id="tilde"),
        pytest.param("c/d", "#/c~1d", id="slash"),
        pytest.param("a/b~c", "#/a~1b~0c", id="slash-and-tilde"),
        pytest.param("~1", "#/~01", id="~01-is-literal-~1"),
        pytest.param("~0", "#/~00", id="~00-is-literal-~0"),
        pytest.param("key with spaces", "#/key with spaces", id="spaces"),
        pytest.param("k-e_y.w@h", "#/k-e_y.w@h", id="dash-underscore-dot-at"),
        pytest.param("日本語", "#/日本語", id="cjk"),
        pytest.param("émojis", "#/émojis", id="accented"),
        pytest.param("🎉", "#/🎉", id="emoji"),
        pytest.param("0", "#/0", id="numeric-string-key"),
        pytest.param("007", "#/007", id="leading-zeros-fine-for-object-keys"),
        pytest.param("", "#/", id="empty-key"),
    ],
)
def test_pointer_selects_object_key(create_json_file, key, pointer):
    file = create_json_file("test.json", {key: "hit", "ref": {"$ref": pointer}})
    assert load_json(file)["ref"] == "hit"


@pytest.mark.parametrize(
    ("doc", "pointer", "expected"),
    [
        pytest.param({"l1": {"l2": {"l3": {"l4": {"l5": "deep"}}}}}, "#/l1/l2/l3/l4/l5", "deep", id="deeply-nested"),
        pytest.param({"a": {"": {"b": "value"}}}, "#/a//b", "value", id="empty-key-mid-path"),
        pytest.param({"items": ["first", "second"]}, "#/items/1", "second", id="array-index"),
        pytest.param({"users": [{"name": "Alice"}]}, "#/users/0/name", "Alice", id="array-then-key"),
        pytest.param({"matrix": [[1, 2], [3, 4]]}, "#/matrix/1/0", 3, id="nested-arrays"),
    ],
)
def test_pointer_walks_multi_segment_path(create_json_file, doc, pointer, expected):
    file = create_json_file("test.json", {**doc, "ref": {"$ref": pointer}})
    assert load_json(file)["ref"] == expected


@pytest.mark.parametrize(
    ("index", "match"),
    [
        ("-1", "not a valid RFC 6901 index"),
        ("+1", "not a valid RFC 6901 index"),
        (" 1", "not a valid RFC 6901 index"),
        ("1_0", "not a valid RFC 6901 index"),
        ("notanumber", "not a valid RFC 6901 index"),
        ("01", "has leading zeros"),
        ("007", "has leading zeros"),
        ("10", "list index out of range"),
    ],
)
def test_invalid_array_index_rejected(create_json_file, index, match):
    """RFC 6901 array indices are digit-only without leading zeros: Python's
    int() would accept '-1' and silently index from the wrong end."""
    file = create_json_file("test.json", {"items": [1, 2, 3], "ref": {"$ref": f"#/items/{index}"}})
    with pytest.raises(ReferenceResolverError, match=f"Invalid JSON pointer .*{match}"):
        load_json(file)


@pytest.mark.parametrize("ref", ["#data/value", "#"], ids=["no-leading-slash", "bare-hash"])
def test_fragment_must_be_a_slash_pointer(create_json_file, ref):
    file = create_json_file("test.json", {"data": {"value": 42}, "ref": {"$ref": ref}})
    with pytest.raises(ReferenceResolverError, match="Invalid .ref format"):
        load_json(file)
