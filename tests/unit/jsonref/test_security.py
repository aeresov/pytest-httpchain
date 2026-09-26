import pytest

from pytest_httpchain.jsonref.exceptions import ReferenceResolverError
from pytest_httpchain.jsonref.loader import load_json


@pytest.mark.parametrize(("kwargs", "limit"), [({}, 3), ({"max_parent_traversal_depth": 1}, 1)], ids=["default", "custom"])
def test_parent_traversal_limit_is_inclusive(create_json_file, kwargs, limit):
    """``limit`` levels of ``..`` resolve; one more is refused."""
    create_json_file("target.json", {"value": 42})
    at_limit = create_json_file("d/" * limit + "ok.json", {"data": {"$ref": "../" * limit + "target.json#/value"}})
    beyond = create_json_file("d/" * (limit + 1) + "bad.json", {"data": {"$ref": "../" * (limit + 1) + "target.json#/value"}})

    assert load_json(at_limit, **kwargs) == {"data": 42}
    with pytest.raises(ReferenceResolverError, match=f"exceeds maximum parent traversal depth of {limit}"):
        load_json(beyond, **kwargs)


def test_root_path_blocks_escape(create_json_files, tmp_path):
    """The target exists, so the error must name the sandbox boundary —
    "not found" would send the user hunting for a typo."""
    files = create_json_files({"outside.json": {"secret": "x"}, "root/inside.json": {"data": {"$ref": "../outside.json#/secret"}}})
    with pytest.raises(ReferenceResolverError, match="resolves outside the reference root"):
        load_json(files["root/inside.json"], root_path=tmp_path / "root")


def test_root_path_allows_parent_traversal_within_root(create_json_files, tmp_path):
    files = create_json_files({"root/data.json": {"value": 42}, "root/subdir/test.json": {"data": {"$ref": "../data.json#/value"}}})
    assert load_json(files["root/subdir/test.json"], root_path=tmp_path / "root") == {"data": 42}


@pytest.mark.parametrize("ref", ["/etc/passwd", "C:\\x.json", "\\x.json"], ids=["posix", "windows-drive", "windows-rooted"])
def test_absolute_path_rejected(create_json_file, ref):
    """An absolute path has no ".." parts, so the traversal limit never fires,
    and ``base / "/abs"`` collapses to "/abs" — escaping the sandbox. Judged
    under both path flavors, since scenario files are portable."""
    file = create_json_file("test.json", {"data": {"$ref": ref}})
    with pytest.raises(ReferenceResolverError, match="Absolute reference paths are not allowed"):
        load_json(file)
