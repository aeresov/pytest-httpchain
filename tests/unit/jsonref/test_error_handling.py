import json
import os
import sys

import pytest

from pytest_httpchain.jsonref.exceptions import DuplicateKeyError, InvalidJSONError, ReferenceResolverError
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
    even from a referenced file — because the validator dispatches on its
    InvalidJSONError base."""
    (tmp_path / "dup.json").write_text('{"a": 1, "a": 2}')
    (tmp_path / "main.json").write_text('{"data": {"$ref": "dup.json"}}')
    with pytest.raises(DuplicateKeyError, match="Duplicate key 'a'"):
        load_json(tmp_path / entry)


@pytest.mark.parametrize(
    ("content", "match", "cause"),
    [
        pytest.param('{"name": "café"}'.encode("latin-1"), "is not valid UTF-8", UnicodeDecodeError, id="not-utf8"),
        # Well-formed JSON past CPython's default int-string conversion limit (4300 digits).
        pytest.param(b'{"n": ' + b"1" * 5000 + b"}", "cannot be parsed: Exceeds the limit", ValueError, id="int-too-long"),
    ],
)
@pytest.mark.parametrize("entry", ["bad.json", "main.json"], ids=["main-file", "referenced-file"])
def test_unreadable_content_names_its_file(tmp_path, entry, content, match, cause):
    """Content the reader cannot parse, short of a syntax error, is an
    InvalidJSONError naming the file it is in — the included one when that is
    where it failed. Both causes are plain ValueErrors, not JSONDecodeErrors,
    so they used to escape the loader raw."""
    (tmp_path / "bad.json").write_bytes(content)
    (tmp_path / "main.json").write_text('{"data": {"$include": "bad.json"}}')
    with pytest.raises(InvalidJSONError, match=rf"bad\.json {match}") as excinfo:
        load_json(tmp_path / entry)
    assert type(excinfo.value.__cause__) is cause


@pytest.mark.skipif(sys.platform == "win32", reason="Windows' non-strict realpath passes such a path through, so it is reported as not found")
@pytest.mark.parametrize("ref", ["a\x00.json", "\ud800.json"], ids=["nul", "lone-surrogate"])
def test_reference_path_the_os_rejects_is_a_resolver_error(tmp_path, ref):
    """One JSON \\u escape spells either, and the OS path call rejects both with
    a ValueError outside every caller's except block: a raw traceback. The
    message shows the path's repr, since neither prints as itself."""
    (tmp_path / "main.json").write_text(json.dumps({"data": {"$include": ref}}))
    with pytest.raises(ReferenceResolverError, match="is not a valid file path") as excinfo:
        load_json(tmp_path / "main.json")
    assert str(excinfo.value).startswith(f"Reference path {ref!r} ")


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
