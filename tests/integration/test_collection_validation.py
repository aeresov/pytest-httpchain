"""Collection-time semantic validation: the plugin runs the shared validator
during pytest collection, failing on semantic errors and warning on issues —
so a plain `pytest` run (even `--collect-only`) is the canonical validation gate.
"""

import json


def _write(pytester, name: str, data: dict) -> None:
    (pytester.path / name).write_text(json.dumps(data))


def _stage(name: str, url: str) -> dict:
    return {"name": name, "request": {"url": url}, "response": [{"verify": {"status": 200}}]}


def test_duplicate_stage_names_fail_collection(pytester):
    _write(
        pytester,
        "test_dup.http.json",
        {"stages": [_stage("s", "https://x.test/a"), _stage("s", "https://x.test/b")]},
    )
    result = pytester.runpytest("--collect-only")
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*Duplicate stage names*"])


def test_fixture_var_conflict_fails_collection(pytester):
    _write(
        pytester,
        "test_conflict.http.json",
        {
            "substitutions": [{"vars": {"token": "abc"}}],
            "stages": [{"name": "s", "fixtures": ["token"], "request": {"url": "https://x.test/a"}, "response": [{"verify": {"status": 200}}]}],
        },
    )
    result = pytester.runpytest("--collect-only")
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*Conflicting fixtures and vars*"])


def test_undefined_variable_warns_at_collection(pytester):
    _write(
        pytester,
        "test_undef.http.json",
        {"stages": [{"name": "s", "request": {"url": "https://x.test/{{ ghost }}"}, "response": [{"verify": {"status": 200}}]}]},
    )
    result = pytester.runpytest("--collect-only")
    assert result.ret == 0
    result.stdout.fnmatch_lines(["*ghost*"])


def test_unknown_key_fails_collection(pytester):
    # Models forbid extra keys: a misspelled field is a collection error
    # naming the key and its location, not a silently wrong request.
    _write(
        pytester,
        "test_typo.http.json",
        {"stages": [{"name": "s", "request": {"url": "https://x.test/a", "headerz": {"X": "1"}}, "response": [{"verify": {"status": 200}}]}]},
    )
    result = pytester.runpytest("--collect-only")
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*headerz*Extra inputs are not permitted*"])


def test_toplevel_schema_key_collects_clean(pytester):
    # The documented editor-integration key is stripped by the loader.
    _write(
        pytester,
        "test_schema_key.http.json",
        {
            "$schema": "https://aeresov.github.io/pytest-httpchain/schema/scenario.schema.json",
            "stages": [_stage("s", "https://x.test/a")],
        },
    )
    result = pytester.runpytest("--collect-only", "-q")
    assert result.ret == 0
    result.stdout.fnmatch_lines(["*test_schema_key*"])


def test_fragment_schema_key_collects_clean(pytester):
    # A fragment's own "$schema" must not leak into the stage dict when the
    # fragment is pulled in via $include.
    _write(
        pytester,
        "stage_fragment.json",
        {
            "$schema": "https://aeresov.github.io/pytest-httpchain/schema/scenario.schema.json",
            "name": "s",
            "request": {"url": "https://x.test/a"},
            "response": [{"verify": {"status": 200}}],
        },
    )
    _write(pytester, "test_fragment.http.json", {"stages": [{"$include": "stage_fragment.json"}]})
    result = pytester.runpytest("--collect-only", "-q")
    assert result.ret == 0
    result.stdout.fnmatch_lines(["*test_fragment*"])


def test_valid_scenario_collects_without_warning(pytester):
    _write(
        pytester,
        "test_ok.http.json",
        {
            "substitutions": [{"vars": {"base": "https://x.test"}}],
            "stages": [{"name": "s", "request": {"url": "{{ base }}/a"}, "response": [{"verify": {"status": 200}}]}],
        },
    )
    result = pytester.runpytest("--collect-only", "-q")
    assert result.ret == 0
    assert "ghost" not in result.stdout.str()
    result.stdout.fnmatch_lines(["*test_ok*"])
