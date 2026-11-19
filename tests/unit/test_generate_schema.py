"""Unit tests for scripts/generate_schema.py: the files a plain run writes, and
`--check`, which writes nothing and names each file a plain run would change.

The script's file handling is under test, not the schema (`test_schema` pins
that), so `build_schema` is stood in for by a small schema whose `$id` has the
`/schema/` segment the versioned copy's `$id` is derived from.
"""

import copy
import json
import os
from pathlib import Path

import pytest

from scripts import generate_schema

SCHEMA = {"$id": "https://example.test/schema/scenario.schema.json", "$defs": {"Stage": {"type": "object"}}, "type": "object"}
VERSION = "1.2.3"
UNVERSIONED = "docs/schema/scenario.schema.json"
VERSIONED = f"docs/schema/v{VERSION}/scenario.schema.json"
REGENERATE = "Regenerate with `uv run python scripts/generate_schema.py` and commit the result.\n"


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    """A project root holding only the pyproject.toml the version is read from."""
    (tmp_path / "pyproject.toml").write_text(f'[project]\nversion = "{VERSION}"\n', encoding="utf-8")
    monkeypatch.setattr(generate_schema, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(generate_schema, "SCHEMA_PATH", tmp_path / UNVERSIONED)
    monkeypatch.setattr(generate_schema, "build_schema", lambda: copy.deepcopy(SCHEMA))
    return tmp_path


def _check(capsys) -> tuple[int, str, str]:
    capsys.readouterr()
    code = generate_schema.main(["--check"])
    out, err = capsys.readouterr()
    return code, out, err


def test_plain_run_writes_both_files_as_text_mode_writes_them(root):
    """The bytes `Path.write_text` writes, the platform's newlines included
    (CRLF on Windows): what a plain run wrote before it shared them with `--check`."""
    assert generate_schema.main([]) == 0

    reference = root / "reference.json"
    reference.write_text(json.dumps(SCHEMA, indent=2) + "\n", encoding="utf-8")
    assert (root / UNVERSIONED).read_bytes() == reference.read_bytes()
    versioned = {**SCHEMA, "$id": f"https://example.test/schema/v{VERSION}/scenario.schema.json"}
    reference.write_text(json.dumps(versioned, indent=2) + "\n", encoding="utf-8")
    assert (root / VERSIONED).read_bytes() == reference.read_bytes()


def test_check_passes_right_after_a_plain_run(root, capsys):
    """On every platform, the Windows CI leg included: comparing with LF bytes
    where a plain run writes CRLF would fail every run there."""
    generate_schema.main([])

    assert _check(capsys) == (0, "Schema files are up to date.\n", "")


def test_check_expects_the_newlines_a_plain_run_writes(root, capsys, monkeypatch):
    """Windows' CRLF, on any platform: the check expects what the writer wrote."""
    monkeypatch.setattr(os, "linesep", "\r\n")
    generate_schema.main([])

    data = (root / UNVERSIONED).read_bytes()
    assert data.count(b"\n") > 1
    assert data.count(b"\r\n") == data.count(b"\n")
    assert _check(capsys) == (0, "Schema files are up to date.\n", "")


def test_check_writes_nothing_and_names_each_missing_file(root, capsys):
    """Each file a plain run would write, in the order it writes them."""
    assert _check(capsys) == (1, "", f"{UNVERSIONED}: missing\n{VERSIONED}: missing\n{REGENERATE}")
    assert not (root / "docs").exists()


def test_check_names_the_versioned_copy_a_version_bump_adds(root, capsys):
    generate_schema.main([])
    (root / VERSIONED).unlink()
    (root / VERSIONED).parent.rmdir()

    assert _check(capsys) == (1, "", f"{VERSIONED}: missing\n{REGENERATE}")


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda data: data.replace(b'"object"', b'"array"', 1), id="content"),
        # Byte for byte, as `git diff` compared it: the same JSON with the other
        # platform's newlines (on Linux and macOS, a file committed with CRLF).
        pytest.param(lambda data: data.replace(os.linesep.encode(), b"\n" if os.linesep == "\r\n" else b"\r\n"), id="newlines"),
        pytest.param(lambda data: data.rstrip(), id="trailing-newline"),
    ],
)
def test_check_names_a_file_that_differs(root, capsys, edit):
    generate_schema.main([])
    path = root / UNVERSIONED
    path.write_bytes(edit(path.read_bytes()))

    assert _check(capsys) == (1, "", f"{UNVERSIONED}: out of date\n{REGENERATE}")


def test_an_existing_versioned_copy_is_never_compared_or_rewritten(root, capsys):
    """It pins a released schema URL, so neither mode touches it, whatever it holds."""
    generate_schema.main([])
    (root / VERSIONED).write_bytes(b"released\n")

    assert _check(capsys) == (0, "Schema files are up to date.\n", "")
    assert generate_schema.main([]) == 0
    assert (root / VERSIONED).read_bytes() == b"released\n"
