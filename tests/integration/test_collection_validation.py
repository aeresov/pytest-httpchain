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


def test_ambiguous_ref_surfaces_as_diagnostic_warning_at_collection(pytester):
    """An ambiguous $ref (file exists under both lookup bases) collects with a
    [HTTPCHAIN026] ScenarioValidationWarning — and under `filterwarnings =
    error` it must surface as that accurate message, not be swallowed into the
    misleading 'Failed to parse JSON file' collection error."""
    (pytester.path / "fragment.json").write_text(json.dumps({"url": "http://server/root"}))
    sub = pytester.path / "sub"
    sub.mkdir()
    (sub / "fragment.json").write_text(json.dumps({"url": "http://server/local"}))
    (sub / "test_amb.http.json").write_text(
        json.dumps(
            {
                "stages": [
                    {
                        "name": "s",
                        "request": {"$ref": "fragment.json"},
                        "response": [{"verify": {"status": 200}}],
                    }
                ]
            }
        )
    )

    result = pytester.runpytest("--collect-only")
    result.stdout.fnmatch_lines(["*HTTPCHAIN026*"])
    assert result.ret == 0

    pytester.makeini("[pytest]\nfilterwarnings = error\n")
    result_strict = pytester.runpytest("--collect-only")
    assert result_strict.ret != 0
    output = result_strict.stdout.str()
    assert "HTTPCHAIN026" in output
    assert "Failed to parse JSON file" not in output


def test_load_failures_are_coded_the_same_way_as_the_cli(pytester):
    """Collection and `validate` share one load-failure taxonomy.

    They used to have two: the same malformed file produced
    `[HTTPCHAIN014] Invalid JSON syntax` from the CLI and an uncoded
    "Cannot load JSON file" from pytest, contradicting docs/diagnostics.md,
    which carves out only 020-024 as CLI-only.
    """
    from pytest_httpchain.validation import validate_scenario

    path = pytester.path / "test_broken.http.json"
    path.write_text('{"stages": {"s": {"request": {"url": "http://x"}},}}')

    cli = validate_scenario(path)
    assert [(d.code, d.message) for d in cli.diagnostics] == [("HTTPCHAIN014", cli.diagnostics[0].message)]

    result = pytester.runpytest("--collect-only")

    assert result.ret != 0
    result.stdout.fnmatch_lines([f"*[[]{cli.diagnostics[0].code}[]]*"])


def test_schema_failures_are_coded_at_collection(pytester):
    """A model-validation failure is HTTPCHAIN000 in both surfaces, and still
    names the offending key (the `headerz` typo case)."""
    _write(pytester, "test_typo.http.json", {"stages": [{"name": "s", "request": {"url": "https://x.test"}, "response": [{"verify": {"headerz": {}}}]}]})

    result = pytester.runpytest("--collect-only")

    assert result.ret != 0
    result.stdout.fnmatch_lines(["*HTTPCHAIN000*headerz*Extra inputs are not permitted*"])


def test_stages_collected_under_narrowed_python_functions(pytester):
    """Stage methods are named "test NN - <stage>", which satisfies pytest's
    default python_functions only via its bare "test" prefix rule — the space
    defeats every glob. Without __test__ = True a narrowed (and very common)
    `python_functions = test_*` collected ZERO stages and left CI green."""
    pytester.copy_example("conftest.py")
    pytester.copy_example("save/test_save_jmespath.http.json")
    pytester.makeini("[pytest]\npython_functions = test_*\n")

    result = pytester.runpytest("--collect-only", "-q")

    result.stdout.fnmatch_lines(["*test_save_jmespath*"])
    assert "no tests ran" not in result.stdout.str()
