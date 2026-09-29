import json
import os
import sys

import pytest

from pytest_httpchain.jsonref.exceptions import DuplicateKeyError, FileLoadError, InvalidJSONError, ReferenceResolverError
from pytest_httpchain.jsonref.loader import load_json
from tests.unit.helpers import TOO_DEEP_TO_PARSE, TOO_DEEP_TO_WALK, on_bounded_stack


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
        # A syntax error, at the comment's opening: the file's line and column.
        pytest.param(b'{\n  "a": 1 /* never closed', json.JSONDecodeError, r"Unterminated comment: line 2 column 10 \(char 11\)$", id="unterminated-comment"),
        # Comments are blanked, not removed, so what follows is where it was.
        pytest.param(b'/* one\n two */ // three\n{"a": }', json.JSONDecodeError, r"Expecting value: line 3 column 7 \(char 30\)$", id="error-after-comments"),
        # Not a ValueError at all.
        pytest.param(TOO_DEEP_TO_PARSE, RecursionError, r"nested too deeply \(.*while decoding a JSON array", id="too-deep-to-parse"),
        # Parses, but the resolver's own walk spends a frame per level.
        pytest.param(TOO_DEEP_TO_WALK, RecursionError, r"nested too deeply \(maximum recursion depth exceeded\)$", id="too-deep-to-walk"),
    ],
)
@pytest.mark.parametrize(
    ("entry", "match"),
    [
        pytest.param("bad.json", "Failed to load JSON from", id="main-file"),
        pytest.param("main.json", "Failed to load external reference bad.json", id="referenced-file"),
        # The innermost file's error passes through the files that reference it.
        pytest.param("outer.json", "Failed to load external reference bad.json", id="nested-reference"),
    ],
)
def test_unloadable_json_chains_the_cause(tmp_path, entry, match, content, cause, reason):
    """One exception type for every consumer; the validator classifies on the
    chained cause (INVALID_JSON for syntax, PARSE_ERROR for depth), and names
    the file that failed when it is not the scenario: a line and column alone
    do not say which file they are in. Content the reader rejects itself,
    bytes that are not UTF-8 included, is an InvalidJSONError instead
    (`test_unreadable_content_names_its_file`)."""
    (tmp_path / "bad.json").write_bytes(content)
    (tmp_path / "main.json").write_text('{"data": {"$ref": "bad.json"}}')
    (tmp_path / "outer.json").write_text('{"data": {"$include": "main.json"}}')
    with pytest.raises(FileLoadError, match=f"^{match}.*: {reason}") as excinfo:
        on_bounded_stack(load_json, tmp_path / entry)
    assert isinstance(excinfo.value.__cause__, cause)
    assert excinfo.value.path is not None
    assert excinfo.value.path.resolve() == (tmp_path / "bad.json").resolve()


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
def test_reference_path_the_os_rejects_is_a_resolver_error(tmp_path):
    """A lone surrogate, one JSON \\u escape away, is rejected by the OS path
    call with a ValueError outside every caller's except block: a raw
    traceback. The message shows the path's repr, since it does not print as
    itself. (A NUL is refused before the path call: `test_nul_in_reference_path`.)"""
    ref = "\ud800.json"
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
