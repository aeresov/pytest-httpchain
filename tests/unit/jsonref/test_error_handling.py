import json
import os
import sys

import pytest

from pytest_httpchain.jsonref.exceptions import DuplicateKeyError, ReferenceResolverError
from pytest_httpchain.jsonref.loader import load_json
from tests.unit.helpers import TOO_DEEP_TO_PARSE, TOO_DEEP_TO_WALK


def test_missing_reference_file(datadir):
    with pytest.raises(ReferenceResolverError, match="not found"):
        load_json(datadir / "case_missing_ref.json")


def test_nul_in_reference_path(create_json_file):
    """Path operations raise a bare ValueError on a NUL byte."""
    file = create_json_file("main.json", {"data": {"$ref": "a\0b.json"}})
    with pytest.raises(ReferenceResolverError, match=r"Reference path contains a NUL character: 'a\\x00b.json'"):
        load_json(file)


@pytest.mark.parametrize(
    ("content", "cause", "reason"),
    [
        pytest.param(b'{"invalid": json}', json.JSONDecodeError, "Expecting value", id="malformed"),
        # Not a JSONDecodeError: read_text fails before the decoder runs.
        pytest.param(b'{"x": "\xff"}', UnicodeDecodeError, "'utf-8' codec can't decode byte 0xff", id="not-utf-8"),
        # Not a ValueError at all. 3.14 bounds the decoder by the real stack
        # size, and with a stack of 16 MB or more it parses this and the
        # resolver's walk raises instead.
        pytest.param(TOO_DEEP_TO_PARSE, RecursionError, r"nested too deeply \((.*while decoding a JSON array.*|maximum recursion depth exceeded)\)$", id="too-deep-to-parse"),
        # Parses, but the resolver's own walk spends a frame per level.
        pytest.param(TOO_DEEP_TO_WALK, RecursionError, r"nested too deeply \(maximum recursion depth exceeded\)$", id="too-deep-to-walk"),
    ],
)
@pytest.mark.parametrize(
    ("entry", "match"),
    [
        pytest.param("bad.json", "Failed to load JSON from", id="main-file"),
        pytest.param("main.json", "Failed to load external reference bad.json", id="referenced-file"),
    ],
)
def test_unloadable_json_chains_the_cause(tmp_path, entry, match, content, cause, reason):
    """One exception type for every consumer; the validator classifies on the
    chained cause (INVALID_JSON for syntax and encoding, PARSE_ERROR for depth)."""
    (tmp_path / "bad.json").write_bytes(content)
    (tmp_path / "main.json").write_text('{"data": {"$ref": "bad.json"}}')
    with pytest.raises(ReferenceResolverError, match=f"^{match}.*: {reason}") as excinfo:
        load_json(tmp_path / entry)
    assert isinstance(excinfo.value.__cause__, cause)


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
