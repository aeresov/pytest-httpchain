import json
import os
import sys

import pytest

from pytest_httpchain.jsonref.exceptions import DuplicateKeyError, ReferenceResolverError
from pytest_httpchain.jsonref.loader import load_json


def test_missing_reference_file(datadir):
    with pytest.raises(ReferenceResolverError, match="not found"):
        load_json(datadir / "case_missing_ref.json")


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        pytest.param("bad.json", "Failed to load JSON from", id="main-file"),
        pytest.param("main.json", "Failed to load external reference bad.json", id="referenced-file"),
    ],
)
def test_malformed_json_chains_the_decode_error(tmp_path, entry, match):
    """The validator reports INVALID_JSON by finding a JSONDecodeError as the cause."""
    (tmp_path / "bad.json").write_text('{"invalid": json}')
    (tmp_path / "main.json").write_text('{"data": {"$ref": "bad.json"}}')
    with pytest.raises(ReferenceResolverError, match=match) as excinfo:
        load_json(tmp_path / entry)
    assert isinstance(excinfo.value.__cause__, json.JSONDecodeError)


@pytest.mark.parametrize("entry", ["dup.json", "main.json"], ids=["main-file", "referenced-file"])
def test_duplicate_key_rejected(tmp_path, entry):
    """A duplicate key errors instead of silently keeping the last value (in
    scenario terms, silently deleting a step). It stays a DuplicateKeyError —
    even from a referenced file — because the validator dispatches on it."""
    (tmp_path / "dup.json").write_text('{"a": 1, "a": 2}')
    (tmp_path / "main.json").write_text('{"data": {"$ref": "dup.json"}}')
    with pytest.raises(DuplicateKeyError, match="Duplicate key 'a'"):
        load_json(tmp_path / entry)


def test_invalid_json_pointer(datadir):
    with pytest.raises(ReferenceResolverError, match="Invalid JSON pointer"):
        load_json(datadir / "case_invalid_pointer.json")


def test_merge_non_dict_with_siblings(datadir):
    with pytest.raises(ReferenceResolverError, match="Cannot merge non-dict reference"):
        load_json(datadir / "case_merge_non_dict.json")


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses filesystem permission bits, so chmod(0o000) stays readable",
)
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="chmod(0o000) does not block reads on Windows, the file stays readable",
)
def test_unreadable_reference_file(create_json_files):
    files = create_json_files({"restricted.json": {"value": 42}, "main.json": {"data": {"$ref": "restricted.json#/value"}}})
    files["restricted.json"].chmod(0o000)
    with pytest.raises(ReferenceResolverError, match="Failed to load external reference"):
        load_json(files["main.json"])
