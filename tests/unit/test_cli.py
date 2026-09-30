"""Unit tests for the pytest-httpchain CLI."""

import importlib.metadata
import json
import os
import re
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pytest_httpchain.cli import app
from pytest_httpchain.schema import build_schema
from tests.unit.helpers import TOO_DEEP_TO_PARSE, TOO_DEEP_TO_WALK, on_bounded_stack

runner = CliRunner()
# typer renders usage errors through rich, which colours them whenever
# GITHUB_ACTIONS (or FORCE_COLOR) is set and splits `--output` across styles.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

USERFUNCS_DIR = Path(__file__).parent / "test_validation_userfuncs"


def _write(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data))
    return path


def _stage(name: str, url: str, **fields) -> dict:
    return {"name": name, "request": {"url": url}, "response": [{"verify": {"status": 200}}], **fields}


@pytest.fixture
def project(tmp_path) -> Path:
    """A project root: its pytest.ini ends the search for the configured suffix."""
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    return tmp_path


@pytest.fixture
def ok_scenario(tmp_path) -> Path:
    return _write(tmp_path / "ok.json", {"stages": [_stage("s", "https://x.test/a")]})


@pytest.fixture
def dup_scenario(tmp_path) -> Path:
    return _write(tmp_path / "bad.json", {"stages": [_stage("dup", "https://x.test/a"), _stage("dup", "https://x.test/b")]})


@pytest.fixture
def warn_scenario(tmp_path) -> Path:
    return _write(tmp_path / "warn.json", {"stages": [_stage("s", "https://x.test/{{ ghost }}")]})


@pytest.fixture
def include_scenario(tmp_path) -> Path:
    _write(tmp_path / "common.json", {"url": "https://x.test/shared"})
    return _write(tmp_path / "test_x.http.json", {"stages": [{"name": "s", "request": {"$include": "common.json"}, "response": [{"verify": {"status": 200}}]}]})


@pytest.fixture
def chain_scenario(tmp_path) -> Path:
    return _write(
        tmp_path / "test_chain.http.json",
        {
            "stages": [
                {
                    "name": "create",
                    "request": {"url": "https://x.test/u", "method": "POST"},
                    "response": [{"save": {"jmespath": {"user_id": "id"}}}, {"verify": {"status": 201}}],
                },
                _stage("get", "https://x.test/u/{{ user_id }}"),
            ]
        },
    )


@pytest.fixture
def meta_scenario(tmp_path) -> Path:
    return _write(
        tmp_path / "test_meta.http.json",
        {
            "fixtures": ["server", "db"],
            "substitutions": [{"vars": {"base_url": "https://x.test", "api_key": "k"}}],
            "stages": [_stage("s", "{{ base_url }}/u")],
        },
    )


def test_version_prints_installed_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.output == importlib.metadata.version("pytest-httpchain") + "\n"


@pytest.mark.parametrize("command", ["schema", "resolve"])
@pytest.mark.parametrize("flag", ["--output", "-o"])
def test_no_output_option(command, flag):
    """The CLI follows the UNIX convention: data goes to stdout and the user
    redirects. The --output/-o option (and its 'Wrote ... to' chatter) is gone."""
    result = runner.invoke(app, [command, flag, "x.json"])
    assert result.exit_code == 2, result.output
    assert f"No such option: {flag}" in _ANSI.sub("", result.stderr)


# --- validate ---


def test_validate_ok(ok_scenario):
    result = runner.invoke(app, ["validate", str(ok_scenario)])
    assert result.exit_code == 0, result.output
    assert result.output == f"{ok_scenario}: OK\n"


def test_validate_invalid(dup_scenario):
    result = runner.invoke(app, ["validate", str(dup_scenario)])
    assert result.exit_code == 1, result.output
    assert result.output == f"{dup_scenario}: INVALID\n  error [HTTPCHAIN001]: Duplicate stage names found: ['dup'] (at stages)\n"


@pytest.mark.parametrize(
    ("args", "exit_code", "status"),
    [
        pytest.param([], 0, "OK with warnings", id="default"),
        pytest.param(["--strict"], 1, "FAILED (warnings)", id="strict"),
    ],
)
def test_validate_warnings(warn_scenario, args, exit_code, status):
    """Warnings alone pass the gate unless --strict, and the status line says which."""
    result = runner.invoke(app, ["validate", *args, str(warn_scenario)])
    assert result.exit_code == exit_code, result.output
    assert result.output == (
        f"{warn_scenario}: {status}\n  warning [HTTPCHAIN003]: Stage 's': request references potentially undefined variable(s): ['ghost'] (at stages[0].request)\n"
    )


@pytest.mark.parametrize(
    ("top", "stage_url", "exit_code", "line"),
    [
        # A stage's: the stages before it run, so a warning.
        pytest.param(
            {},
            "https://x.test/{{ 'a\ud800' }}",
            0,
            "  warning [HTTPCHAIN037]: Stage 's': request has an invalid expression '{{ 'a\\ud800' }}', and rendering it fails the stage: "
            "the expression holds '\\ud800', which is not valid text (surrogates not allowed) (at stages[0].request)\n",
            id="stage-level-lone-surrogate",
        ),
        # A scenario-level one fails every stage, and fails the gate.
        pytest.param(
            {"substitutions": [{"vars": {"token": "{{ t = 'x' }}"}}]},
            "https://x.test/",
            1,
            "  error [HTTPCHAIN038]: Scenario-level 'substitutions' has an invalid expression '{{ t = 'x' }}', and rendering it crashes scenario initialization: "
            "a template holds one expression, not an assignment; to compare two values, write '=='\n",
            id="scenario-level",
        ),
    ],
)
def test_validate_invalid_expression(tmp_path, top, stage_url, exit_code, line):
    """A lone surrogate is printed escaped: the parser's UnicodeEncodeError
    crashed validation, and the character itself crashed printing."""
    scenario = _write(tmp_path / "x.json", {**top, "stages": [_stage("s", stage_url)]})
    result = runner.invoke(app, ["validate", str(scenario)])
    assert result.exit_code == exit_code, result.output
    assert result.output.splitlines(keepends=True)[1:] == [line]


def test_validate_multiple_files_one_bad_exits_one(ok_scenario, dup_scenario):
    """Sorted by path (bad.json before ok.json), not in the order given, and a
    summary closing a run over more than one file."""
    result = runner.invoke(app, ["validate", str(ok_scenario), str(dup_scenario)])
    assert result.exit_code == 1, result.output
    assert result.output == (
        f"{dup_scenario}: INVALID\n  error [HTTPCHAIN001]: Duplicate stage names found: ['dup'] (at stages)\n{ok_scenario}: OK\n2 files checked, 1 with errors, 0 with warnings\n"
    )


def test_validate_json_format_ok(ok_scenario):
    result = runner.invoke(app, ["validate", "--format", "json", str(ok_scenario)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["valid"] is True
    assert data["files"][0]["path"] == str(ok_scenario)
    assert data["files"][0]["result"]["valid"] is True


def test_validate_json_format_reports_codes(dup_scenario):
    result = runner.invoke(app, ["validate", "--format", "json", str(dup_scenario)])
    assert result.exit_code == 1, result.output
    data = json.loads(result.output)
    assert data["valid"] is False
    assert [d["code"] for d in data["files"][0]["result"]["diagnostics"]] == ["HTTPCHAIN001"]


@pytest.mark.parametrize(("args", "exit_code"), [pytest.param([], 0, id="default"), pytest.param(["--strict"], 1, id="strict")])
def test_validate_json_payload_reports_strictness(warn_scenario, args, exit_code):
    """The top-level `valid` reflects the gate (including --strict), while each
    file's `result.valid` is pure validity — the payload must say which gate
    was applied so consumers can tell the two apart."""
    result = runner.invoke(app, ["validate", *args, "--format", "json", str(warn_scenario)])
    assert result.exit_code == exit_code, result.output
    payload = json.loads(result.output)
    assert payload["strict"] is bool(args)
    assert payload["valid"] is (exit_code == 0)
    assert payload["files"][0]["result"]["valid"] is True


@pytest.mark.parametrize("given", ["file", "directory"])
@pytest.mark.parametrize(
    ("args", "exit_code", "reports_import"),
    [
        pytest.param([], 0, False, id="without-deep-never-imports"),
        # Deep findings are warnings, so they pass the gate unless --strict.
        pytest.param(["--deep"], 0, True, id="deep"),
        pytest.param(["--deep", "--strict"], 1, True, id="deep-strict"),
    ],
)
def test_validate_deep(project, args, exit_code, reports_import, given):
    f = _write(
        project / "test_deep.http.json",
        {"stages": [{"name": "s", "request": {"url": "https://x.test/a"}, "response": [{"verify": {"status": 200, "user_functions": ["userfuncs:does_not_exist"]}}]}]},
    )
    result = runner.invoke(app, ["validate", *args, "--syspath", str(USERFUNCS_DIR), str(f if given == "file" else project)])
    assert result.exit_code == exit_code, result.output
    assert result.output.startswith(f"{f}: ")
    assert ("does_not_exist" in result.output) is reports_import


# --- validate over directories ---


def _tree(root: Path, scenarios: dict[str, dict]) -> None:
    """Scenario files under ``root``, keyed by their path relative to it."""
    for name, data in scenarios.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        _write(root / name, data)


OK = {"stages": [_stage("s", "https://x.test/a")]}
WARNS = {"stages": [_stage("s", "https://x.test/{{ ghost }}")]}
DUPLICATE = {"stages": [_stage("dup", "https://x.test/a"), _stage("dup", "https://x.test/b")]}


def test_validate_directory_checks_the_files_pytest_collects(project):
    """In pytest's order, each file as when named alone, then the summary: a
    file with errors counted once, under errors. `build/` is a directory
    pytest skips, and `notes.json` no scenario name."""
    tests = project / "tests"
    _tree(
        tests,
        {"test_a.http.json": OK, "api/test_b.http.jsonc": WARNS, "api/test_c.http.json": DUPLICATE, "build/test_d.http.json": DUPLICATE, "notes.json": DUPLICATE},
    )
    result = runner.invoke(app, ["validate", str(tests)])
    assert result.exit_code == 1, result.output
    assert result.output == (
        f"{tests / 'api' / 'test_b.http.jsonc'}: OK with warnings\n"
        "  warning [HTTPCHAIN003]: Stage 's': request references potentially undefined variable(s): ['ghost'] (at stages[0].request)\n"
        f"{tests / 'api' / 'test_c.http.json'}: INVALID\n"
        "  error [HTTPCHAIN001]: Duplicate stage names found: ['dup'] (at stages)\n"
        f"{tests / 'test_a.http.json'}: OK\n"
        "3 files checked, 1 with errors, 1 with warnings\n"
    )


@pytest.mark.parametrize(("args", "exit_code"), [pytest.param([], 0, id="default"), pytest.param(["--strict"], 1, id="strict")])
def test_validate_directory_passes_on_warnings_unless_strict(project, args, exit_code):
    _tree(project, {"test_a.http.json": OK, "test_b.http.json": WARNS})
    result = runner.invoke(app, ["validate", *args, str(project)])
    assert result.exit_code == exit_code, result.output
    assert result.output.endswith("\n2 files checked, 0 with errors, 1 with warnings\n")


def test_validate_file_reached_twice_is_checked_once(project):
    """Once, where first reached; one file, so no summary."""
    _tree(project, {"test_a.http.json": OK})
    result = runner.invoke(app, ["validate", str(project), str(project / "test_a.http.json"), str(project)])
    assert result.exit_code == 0, result.output
    assert result.output == f"{project / 'test_a.http.json'}: OK\n"


@pytest.mark.parametrize(
    "scenarios",
    [
        pytest.param({}, id="empty"),
        # Only a name pytest does not collect, and a file in a directory it skips.
        pytest.param({"login.http.json": OK, "build/test_a.http.json": OK}, id="nothing-pytest-collects"),
    ],
)
def test_validate_directory_without_scenario_files_fails(project, scenarios):
    """A typo'd path must not pass CI as an empty run: a missing path is
    HTTPCHAIN010 as before, and a directory holding nothing to check is
    HTTPCHAIN039, whatever else it holds."""
    suite = project / "suite"
    suite.mkdir()
    _tree(suite, scenarios)
    result = runner.invoke(app, ["validate", str(suite), str(project / "suiet")])
    assert result.exit_code == 1, result.output
    assert result.output == (
        f"{project / 'suiet'}: INVALID\n"
        f"  error [HTTPCHAIN010]: File not found: {project / 'suiet'}\n"
        f"{suite}: INVALID\n"
        f"  error [HTTPCHAIN039]: No scenario files named test_<name>.http.json or test_<name>.http.jsonc in directory: {suite}\n"
    )


@pytest.mark.parametrize(
    ("extra", "counted_apart"),
    [
        pytest.param(["empty"], ", 1 directory without scenario files", id="empty-directory"),
        pytest.param(["missing"], ", 1 path not found", id="missing-path"),
        pytest.param(
            ["missing", "empty", "gone", "void"],
            ", 2 paths not found, 2 directories without scenario files",
            id="several",
        ),
    ],
)
def test_validate_summary_counts_paths_without_a_file_to_check_apart(project, extra, counted_apart):
    """A path that names nothing to check is no file checked: the count is of
    the files there are, and the rest is named for what it is."""
    _tree(project, {"suite/test_a.http.json": OK, "suite/test_b.http.json": OK})
    (project / "empty").mkdir()
    (project / "void").mkdir()
    result = runner.invoke(app, ["validate", str(project / "suite"), *(str(project / name) for name in extra)])
    assert result.exit_code == 1, result.output
    assert result.output.splitlines()[-1] == f"2 files checked, 0 with errors, 0 with warnings{counted_apart}"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a FIFO")
def test_validate_summary_counts_a_path_that_is_not_a_file_apart(project):
    _tree(project, {"test_a.http.json": OK, "test_b.http.json": OK})
    os.mkfifo(project / "pipe")
    result = runner.invoke(app, ["validate", str(project / "test_a.http.json"), str(project / "test_b.http.json"), str(project / "pipe")])
    assert result.exit_code == 1, result.output
    assert result.output.splitlines()[-1] == "2 files checked, 0 with errors, 0 with warnings, 1 path that is not a file"


def test_validate_summary_needs_two_files_checked(project):
    """One file checked and a mistyped path: no summary, the two lines say it all."""
    _tree(project, {"test_a.http.json": OK})
    result = runner.invoke(app, ["validate", str(project / "test_a.http.json"), str(project / "test_b.http.json")])
    assert result.exit_code == 1, result.output
    assert result.output == (
        f"{project / 'test_a.http.json'}: OK\n{project / 'test_b.http.json'}: INVALID\n  error [HTTPCHAIN010]: File not found: {project / 'test_b.http.json'}\n"
    )


def test_validate_sorts_the_files_of_every_argument_together(project):
    """By path, name by name, whatever order the arguments come in: the report
    does not depend on how a shell or `find` listed them. Within a directory
    that is pytest's order."""
    _tree(project, {"c/test_c.http.json": OK, "a/b/test_b.http.json": OK, "a/test_a.http.jsonc": OK, "test_z.http.json": OK})
    result = runner.invoke(app, ["validate", str(project / "test_z.http.json"), str(project / "c"), str(project / "a")])
    assert result.exit_code == 0, result.output
    assert result.output == (
        f"{project / 'a' / 'b' / 'test_b.http.json'}: OK\n"
        f"{project / 'a' / 'test_a.http.jsonc'}: OK\n"
        f"{project / 'c' / 'test_c.http.json'}: OK\n"
        f"{project / 'test_z.http.json'}: OK\n"
        "4 files checked, 0 with errors, 0 with warnings\n"
    )


def test_validate_json_lists_the_files_found_and_the_empty_directory(project):
    """The payload's shape is unchanged: one entry per path reported."""
    _tree(project, {"suite/test_a.http.json": OK, "suite/test_b.http.json": DUPLICATE})
    (project / "empty").mkdir()
    result = runner.invoke(app, ["validate", "--format", "json", str(project / "suite"), str(project / "empty")])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["valid"] is False
    assert [(entry["path"], [d["code"] for d in entry["result"]["diagnostics"]]) for entry in payload["files"]] == [
        (str(project / "empty"), ["HTTPCHAIN039"]),
        (str(project / "suite" / "test_a.http.json"), []),
        (str(project / "suite" / "test_b.http.json"), ["HTTPCHAIN001"]),
    ]


@pytest.mark.parametrize(
    ("ini", "args", "found"),
    [
        pytest.param("[pytest]\n", [], "test_a.http.json", id="default"),
        # The suffix pytest's configuration sets, as pytest would find it.
        pytest.param("[pytest]\nhttpchain_suffix = api\n", [], "test_b.api.json", id="configured"),
        pytest.param("[pytest]\n", ["--suffix", "api"], "test_b.api.json", id="option"),
        pytest.param("[pytest]\nhttpchain_suffix = api\n", ["--suffix", "http"], "test_a.http.json", id="option-over-configured"),
    ],
)
def test_validate_directory_by_suffix(tmp_path, ini, args, found):
    (tmp_path / "pytest.ini").write_text(ini)
    _tree(tmp_path, {"test_a.http.json": OK, "test_b.api.json": OK})
    result = runner.invoke(app, ["validate", *args, str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert result.output == f"{tmp_path / found}: OK\n"


@pytest.mark.parametrize("suffix", ["a.b", "", "a" * 33])
def test_validate_rejects_a_suffix_pytest_would(suffix):
    result = runner.invoke(app, ["validate", "--suffix", suffix, "."])
    assert result.exit_code == 2, result.output
    assert "Invalid value for '--suffix': must contain only alphanumeric characters, underscores, hyphens, and be ≤32 chars" in " ".join(
        _ANSI.sub("", result.stderr).replace("│", "").split()
    )


def test_validate_directory_with_a_configuration_pytest_rejects_exits_one(tmp_path):
    """Before any file is checked, one `error:` line: pytest would not run
    either. Files named alone need no suffix, and do not read it."""
    (tmp_path / "pytest.ini").write_text("[pytest]\nhttpchain_suffix = a.b\n")
    _tree(tmp_path, {"test_a.http.json": OK})
    result = runner.invoke(app, ["validate", str(tmp_path / "test_a.http.json"), str(tmp_path)])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == f"error: {tmp_path / 'pytest.ini'}: httpchain_suffix must contain only alphanumeric characters, underscores, hyphens, and be ≤32 chars\n"
    assert runner.invoke(app, ["validate", str(tmp_path / "test_a.http.json")]).exit_code == 0


# --- schema / resolve ---


def test_schema_emits_build_schema():
    result = runner.invoke(app, ["schema"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == build_schema()


def test_resolve_inlines_include(include_scenario):
    result = runner.invoke(app, ["resolve", str(include_scenario)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["stages"][0]["request"] == {"url": "https://x.test/shared"}


def test_resolve_missing_ref_exits_one(include_scenario, tmp_path):
    (tmp_path / "common.json").unlink()
    result = runner.invoke(app, ["resolve", str(include_scenario)])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.startswith("error: Reference path 'common.json' not found.")


@pytest.mark.parametrize(
    ("shared", "sibling", "where"),
    [
        pytest.param({"status": ["2xx"]}, {"status": [404]}, "verify.status", id="status"),
        pytest.param({"jmespath": {"tags": ["a"]}}, {"jmespath": {"tags": ["b"]}}, "verify.jmespath.tags", id="jmespath-expectation"),
    ],
)
def test_resolve_merges_whole_values_as_collection_does(tmp_path, shared, sibling, where):
    """A sibling status list is not concatenated onto a fragment's (which would
    widen the check), nor a jmespath expectation onto one (which would assert
    what neither wrote), in the printed document as at collection."""
    _write(tmp_path / "common.json", {"ok": {"verify": shared}})
    step = {"$merge": "common.json#/ok", "verify": sibling}
    scenario = _write(tmp_path / "test_x.http.json", {"stages": [{**_stage("s", "https://x.test/a"), "response": [step]}]})
    result = runner.invoke(app, ["resolve", str(scenario)])
    assert result.exit_code == 1
    assert result.stderr == f"error: Merge conflict at {where}\n"


@pytest.mark.parametrize("command", ["resolve", "show", "graph"])
@pytest.mark.parametrize(
    ("content", "message"),
    [
        pytest.param('{"url": "https://x.test/café"}'.encode("latin-1"), "common.json is not valid UTF-8", id="not-utf8"),
        pytest.param(b'{"timeout": ' + b"1" * 5000 + b"}", "common.json cannot be parsed", id="int-too-long"),
    ],
)
def test_unreadable_include_exits_one_naming_the_file(include_scenario, tmp_path, command, content, message):
    """Undecodable bytes and an int past the conversion limit raise plain
    ValueErrors, outside the load errors these catch, so the fragment printed a
    raw traceback."""
    (tmp_path / "common.json").write_bytes(content)
    result = runner.invoke(app, [command, str(include_scenario)])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert message in result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="Windows' non-strict realpath passes such a path through, so it is reported as not found")
@pytest.mark.parametrize("command", ["resolve", "show", "graph"])
def test_include_path_the_os_rejects_exits_one(tmp_path, command):
    """The OS path call's ValueError on a lone surrogate escaped as a raw traceback."""
    scenario = _write(tmp_path / "test_x.http.json", {"stages": [{"name": "s", "$include": "\ud800.json"}]})
    result = runner.invoke(app, [command, str(scenario)])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert r"Reference path '\ud800.json' is not a valid file path" in result.stderr


JSONC_SCENARIO = """// Comments and trailing commas, here and in the file included.
{
    "stages": [
        {
            "name": "s", /* the only stage */
            "request": { "$include": "request.jsonc" },
            "response": [{ "verify": { "status": 200 } },],
        },
    ],
}
"""


@pytest.fixture
def jsonc_scenario(tmp_path) -> Path:
    (tmp_path / "request.jsonc").write_text('{\n  "url": "https://x.test/a", // shared\n}\n')
    scenario = tmp_path / "test_x.http.jsonc"
    scenario.write_text(JSONC_SCENARIO)
    return scenario


def test_resolve_prints_strict_json(jsonc_scenario):
    """The comments are gone from the printed document, and so are the trailing commas."""
    result = runner.invoke(app, ["resolve", str(jsonc_scenario)])
    assert result.exit_code == 0, result.output
    expected = {"stages": [{"name": "s", "request": {"url": "https://x.test/a"}, "response": [{"verify": {"status": 200}}]}]}
    assert result.output == json.dumps(expected, indent=2) + "\n"


def test_resolve_refuses_numbers_json_cannot_write(tmp_path):
    """``NaN`` and ``Infinity``, which ``json.loads`` reads, are no JSON: a
    file holding one fails to load rather than print them as strict JSON."""
    scenario = tmp_path / "test_x.http.json"
    scenario.write_text('{"stages": [{"name": "s", "request": {"url": "https://x.test/a", "timeout": Infinity}}]}')
    result = runner.invoke(app, ["resolve", str(scenario)])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Infinity is not valid JSON: line 1 column 77 (char 76)" in result.stderr


@pytest.mark.parametrize(("command", "line"), [("validate", "test_x.http.jsonc: OK"), ("show", "1 · s    GET https://x.test/a"), ("graph", 'S0["1 · s"]')])
def test_every_command_reads_jsonc(jsonc_scenario, command, line):
    result = runner.invoke(app, [command, str(jsonc_scenario)])
    assert result.exit_code == 0, result.output
    assert line in result.output


@pytest.mark.parametrize("command", ["resolve", "show", "graph"])
def test_unterminated_comment_exits_one(jsonc_scenario, command):
    (jsonc_scenario.parent / "request.jsonc").write_text('{"url": "https://x.test/a"}\n/* never closed')
    result = runner.invoke(app, [command, str(jsonc_scenario)])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.endswith("Failed to load external reference request.jsonc: Unterminated comment: line 2 column 1 (char 28)\n")


# --- show / graph ---


def test_show_text_reports_dataflow(chain_scenario):
    result = runner.invoke(app, ["show", str(chain_scenario)])
    assert result.exit_code == 0, result.output
    assert result.output == (
        "test_chain.http.json\n"
        "2 stage(s)\n"
        "\n"
        "1 · create    POST https://x.test/u\n"
        "    saves:    user_id\n"
        "2 · get    GET https://x.test/u/{{ user_id }}\n"
        "    consumes: user_id (from #1 create)\n"
    )


def test_show_text_reports_marks(tmp_path):
    scenario = _write(tmp_path / "test_marks.http.json", {"stages": [_stage("only", "https://x.test/a", marks=["skip", 'xfail(reason="flaky")'])]})
    result = runner.invoke(app, ["show", str(scenario)])
    assert result.exit_code == 0, result.output
    assert result.output == 'test_marks.http.json\n1 stage(s)\n\n1 · only    GET https://x.test/a\n    marks:    skip, xfail(reason="flaky")\n'


@pytest.fixture
def skipping_scenario(tmp_path) -> Path:
    """The chain scenario with a skip_if on its producer, and a stage skipped outright."""
    create = {
        "name": "create",
        "skip_if": " {{ exists('user_id') }} ",
        "request": {"url": "https://x.test/u", "method": "POST"},
        "response": [{"save": {"jmespath": {"user_id": "id"}}}, {"verify": {"status": 201}}],
    }
    return _write(tmp_path / "test_skip.http.json", {"stages": [create, _stage("get", "https://x.test/u/{{ get('user_id') }}", skip_if=True)]})


def test_show_text_reports_skip_if(skipping_scenario):
    result = runner.invoke(app, ["show", str(skipping_scenario)])
    assert result.exit_code == 0, result.output
    assert result.output == (
        "test_skip.http.json\n"
        "2 stage(s)\n"
        "\n"
        "1 · create    POST https://x.test/u\n"
        "    skip_if:  {{ exists('user_id') }}\n"
        "    saves:    user_id\n"
        "2 · get    GET https://x.test/u/{{ get('user_id') }}\n"
        "    skip_if:  true\n"
    )


def test_show_text_reports_scenario_fixtures_and_vars(meta_scenario):
    """The rendered form, not just the names: `show` exists to be read, so the
    summary joins these rather than printing the Python list repr."""
    result = runner.invoke(app, ["show", str(meta_scenario)])
    assert result.exit_code == 0, result.output
    assert result.output == "test_meta.http.json\n1 stage(s) · fixtures: db, server · vars: api_key, base_url\n\n1 · s    GET {{ base_url }}/u\n"


def test_show_json_exposes_edges(chain_scenario):
    result = runner.invoke(app, ["show", "--format", "json", str(chain_scenario)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["edges"] == [{"producer": 0, "consumer": 1, "vars": ["user_id"]}]


def test_show_json_reports_scenario_fixtures_and_vars(meta_scenario):
    result = runner.invoke(app, ["show", "--format", "json", str(meta_scenario)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert (data["scenario_fixtures"], data["scenario_vars"]) == (["db", "server"], ["api_key", "base_url"])


@pytest.mark.parametrize("command", ["show", "graph"])
def test_inspection_of_unloadable_file_exits_one(tmp_path, command):
    missing = tmp_path / "missing.json"
    result = runner.invoke(app, [command, str(missing)])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.startswith(f"error: cannot load {missing}: ")


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        pytest.param(b'{"stages": ' + TOO_DEEP_TO_PARSE + b"}", "nested too deeply (", id="too-deep-to-parse"),
        pytest.param(b'{"stages": ' + TOO_DEEP_TO_WALK + b"}", "nested too deeply (", id="too-deep-to-walk"),
    ],
)
@pytest.mark.parametrize("command", ["resolve", "show", "graph"])
def test_file_nested_too_deeply_exits_one_with_an_error_line(tmp_path, command, content, reason):
    """One `error:` line, not a traceback: a RecursionError is not even a
    ValueError. (A file that is not UTF-8: `test_unreadable_include_exits_one_naming_the_file`.)"""
    scenario = tmp_path / "test_x.http.json"
    scenario.write_bytes(content)
    result = on_bounded_stack(runner.invoke, app, [command, str(scenario)])
    assert result.exit_code == 1
    assert result.stdout == ""
    prefix = "error: " if command == "resolve" else f"error: cannot load {scenario}: "
    assert result.stderr.startswith(f"{prefix}Failed to load JSON from {scenario}: {reason}")
    assert result.stderr.count("\n") == 1


def test_show_invalid_scenario_exits_one(tmp_path):
    scenario = _write(tmp_path / "bad.json", {"stages": [{"name": "s", "request": {}}]})
    result = runner.invoke(app, ["show", str(scenario)])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == f"error: {scenario} is not a valid scenario — run `pytest-httpchain validate {scenario}` for details\n"


@pytest.mark.parametrize(("args", "direction"), [pytest.param([], "TD", id="default"), pytest.param(["--direction", "LR"], "LR", id="LR")])
def test_graph_emits_mermaid(chain_scenario, args, direction):
    result = runner.invoke(app, ["graph", *args, str(chain_scenario)])
    assert result.exit_code == 0, result.output
    assert result.output == f'flowchart {direction}\n    S0["1 · create"]\n    S1["2 · get"]\n    S0 -->|user_id| S1\n'


def test_graph_dots_the_edges_out_of_a_stage_that_may_skip(tmp_path):
    """Its saves may never be made: the edge is drawn dotted."""
    maybe = {**_stage("maybe", "https://x.test/a", skip_if="{{ flag }}"), "response": [{"save": {"jmespath": {"x": "x"}}}]}
    sure = {**_stage("sure", "https://x.test/b"), "response": [{"save": {"jmespath": {"y": "y"}}}]}
    scenario = _write(tmp_path / "test_x.http.json", {"stages": [maybe, sure, _stage("reader", "https://x.test/{{ x }}/{{ y }}")]})
    result = runner.invoke(app, ["graph", str(scenario)])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-2:] == ["    S0 -.->|x| S2", "    S1 -->|y| S2"]


@pytest.fixture
def refresh_scenario(tmp_path) -> Path:
    """A token saved by `login`, re-saved by a `refresh` that may skip, read by `use`."""
    saves_token = [{"save": {"jmespath": {"token": "t"}}}]
    stages = [
        {**_stage("login", "https://x.test/login"), "response": saves_token},
        {**_stage("refresh", "https://x.test/refresh", skip_if="{{ not stale }}"), "response": saves_token},
        _stage("use", "https://x.test/{{ token }}"),
    ]
    return _write(tmp_path / "test_refresh.http.json", {"substitutions": [{"vars": {"stale": False}}], "stages": stages})


def test_show_text_lists_every_stage_a_name_may_come_from(refresh_scenario):
    """When `refresh` skips, `use` runs on the login's token: `show` names
    both, nearest first."""
    result = runner.invoke(app, ["show", str(refresh_scenario)])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1] == "    consumes: token (from #2 refresh, else #1 login)"


def test_graph_draws_an_edge_from_the_producer_a_skipped_stage_falls_back_to(refresh_scenario):
    """The login's edge is solid, the refresh's dotted: dropping the login's
    drew `use` as depending on a stage that did not run."""
    result = runner.invoke(app, ["graph", str(refresh_scenario)])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-2:] == ["    S0 -->|token| S2", "    S1 -.->|token| S2"]


def test_graph_of_a_stageless_scenario_is_still_valid_mermaid(tmp_path):
    """An empty `stages` list has no nodes; the output must stay a parseable
    flowchart with a comment rather than a bare header or a crash."""
    scenario = _write(tmp_path / "test_empty.http.json", {"stages": []})
    result = runner.invoke(app, ["graph", str(scenario)])
    assert result.exit_code == 0, result.output
    assert result.output == "flowchart TD\n    %% (no stages)\n"


# --- import ---

HAR_EXAMPLE = Path(__file__).parent / "importers" / "example.har"


def _imported(result) -> dict:
    """The scenario an import printed, checked to be what ``validate`` passes."""
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_import_curl_prints_the_scenario_and_its_placeholders():
    result = runner.invoke(app, ["import", "curl", "curl -H 'Authorization: Bearer s3cret' 'https://api.test/v1/me?page=2'"])
    assert _imported(result) == {
        "description": "Imported from a curl command",
        "substitutions": [{"vars": {"api_token": "{{ env('API_TOKEN') }}"}}],
        "client": {"base_url": "https://api.test", "follow_redirects": False},
        "stages": [
            {
                "name": "get_v1_me",
                "request": {"url": "/v1/me", "params": {"page": "2"}, "auth": {"bearer": "{{ api_token }}"}},
                "response": [{"verify": {"status": "2xx"}}],
            }
        ],
    }
    assert result.stderr == ("note: secrets were left out of the scenario, which reads them from these environment variables:\n  API_TOKEN  the bearer token (stage get_v1_me)\n")


def test_import_curl_takes_the_words_a_shell_split():
    """Several arguments are the command's words; everything from the first
    on is the command's, its own -o included, which a warning names, since
    it may have been meant as the import's."""
    result = runner.invoke(app, ["import", "curl", "curl", "-o", "response.json", "-X", "POST", "https://api.test/items", "--json", '{"name": "a b"}'])
    stage = _imported(result)["stages"][0]
    assert stage["request"] == {"method": "POST", "url": "/items", "headers": {"Accept": "application/json"}, "body": {"json": {"name": "a b"}}}
    assert result.stderr == ("warning: Ignored -o 'response.json', the file curl writes its answer to: to write the scenario to a file, give import's -o before the command\n")


@pytest.mark.parametrize("first", ["-sSL", "-k", "--compressed"])
def test_import_curl_options_end_at_the_commands_first_word(first, tmp_path, monkeypatch):
    """The command's words may start with a curl flag the import does not
    have: a curl -o after it is still the command's, never the import's, so
    the scenario is not written where curl was to write its answer."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["import", "curl", first, "-o", "page.html", "https://api.test/a"])
    assert _imported(result)["stages"][0]["request"] == {"url": "/a"}
    assert "warning: Ignored -o 'page.html', the file curl writes its answer to" in result.stderr
    assert not (tmp_path / "page.html").exists()
    # The import's own, given first, are its own.
    result = runner.invoke(app, ["import", "curl", "--force", "-o", "out.json", first, "-o", "page.html", "https://api.test/a"])
    assert result.exit_code == 0
    assert json.loads((tmp_path / "out.json").read_text())["stages"][0]["request"] == {"url": "/a"}


def test_import_curl_options_go_before_a_whole_command():
    """A whole command in one argument, then more: the rest is refused, saying
    where it goes, rather than the command's words read as a URL."""
    result = runner.invoke(app, ["import", "curl", "curl https://api.test/a", "-o", "x.json"])
    assert result.exit_code == 2
    assert "the first argument holds a whole command, so nothing may follow it; give '-o x.json' before it, or the command as its words" in re.sub(
        r"[\s│]+", " ", _ANSI.sub("", result.stderr)
    )


def test_import_curl_reads_data_files_where_it_runs(tmp_path, monkeypatch):
    """curl reads a -d @file from the directory it runs in, and strips its
    line breaks: so does the import."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "form.txt").write_text("a=1&b=2\n")
    result = runner.invoke(app, ["import", "curl", "curl https://api.test/a -d @form.txt"])
    assert _imported(result)["stages"][0]["request"]["body"] == {"form": {"a": "1", "b": "2"}}
    assert result.stderr == ""


def test_import_writes_what_passes_validate_as_a_file(tmp_path):
    """What the import checks is the file as written, read back as
    ``validate`` and collection read it: a recorded body with a ``$ref``
    member, which the file's loader would resolve in a json body, passes
    ``validate`` (it is text), and the placeholders it needs are listed."""
    output = tmp_path / "test_schema.http.json"
    body = '{"properties": {"a": {"$ref": "#/definitions/a"}}, "definitions": {"a": {"type": "string"}}, "token": "s3cret"}'
    result = runner.invoke(app, ["import", "curl", "-o", str(output), f"curl https://api.test/schemas --json '{body}'"])
    assert result.exit_code == 0, result.output
    assert "note: Stage 'post_schemas': its JSON body is written as text: a json body could not hold its member '$ref'" in result.stderr
    assert "  TOKEN  JSON body member 'token' (stage post_schemas)\n" in result.stderr
    assert "s3cret" not in output.read_text()
    assert runner.invoke(app, ["validate", "--strict", str(output)]).output == f"{output}: OK\n"


@pytest.mark.parametrize(
    ("args", "stdin"),
    [
        pytest.param(["curl", "curl -d $'bad \\ud800' https://api.test/a"], None, id="curl"),
        pytest.param(
            ["har", "-"],
            '{"log": {"entries": [{"request": {"method": "POST", "url": "https://a.test/x", "postData": {"mimeType": "text/plain", "text": "bad \\ud800"}},'
            ' "response": {"status": 200}}]}}',
            id="har",
        ),
    ],
)
def test_import_of_a_lone_surrogate_exits_one(tmp_path, args, stdin):
    """A lone surrogate is no text, which no request sends and no UTF-8 file
    holds: a clean error, and no file."""
    output = tmp_path / "test_x.http.json"
    result = runner.invoke(app, ["import", args[0], "-o", str(output), *args[1:]], input=stdin)
    assert result.exit_code == 1
    assert re.search(r"error: What was recorded holds '\\ud800', a lone surrogate, which is no text \(line \d+ of the scenario\)\n$", result.stderr), result.stderr
    assert not output.exists()


def test_import_curl_lists_the_files_the_scenario_reads():
    result = runner.invoke(app, ["import", "curl", "curl -F 'doc=@report.pdf' --data-binary @body.bin https://api.test/a"])
    assert _imported(result)["stages"][0]["request"]["body"] == {"multipart": {"files": {"doc": "report.pdf"}}}
    assert result.stderr.endswith("note: the scenario reads these files, a relative path from the scenario's own directory: report.pdf\n")


def test_import_curl_reads_stdin():
    result = runner.invoke(app, ["import", "curl", "-"], input="curl https://api.test/a \\\n  -H 'X-Trace: 1'\ncurl https://api.test/b\n")
    scenario = _imported(result)
    assert scenario["description"] == "Imported from 2 curl requests"
    assert [stage["name"] for stage in scenario["stages"]] == ["get_a", "get_b"]


def test_import_curl_options_go_before_stdin():
    """Everything from the command's first word on is the command's, so an
    option after ``-`` would be a curl word: refused, saying where it goes."""
    result = runner.invoke(app, ["import", "curl", "-", "-o", "x.json"], input="curl https://api.test/a")
    assert result.exit_code == 2
    # typer boxes a usage error, wrapping its lines inside the box's borders.
    assert "'-' reads the command from stdin, so nothing may follow it; give '-o x.json' before it" in re.sub(r"[\s│]+", " ", _ANSI.sub("", result.stderr))


def test_import_curl_takes_the_curl_command_of_a_pipeline(tmp_path, monkeypatch):
    """A command piped into curl is not taken for URLs: a plain cat's file is
    what -d @- reads, and what curl's output goes to is named."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "order.json").write_text('{"sku": "A-1"}\n')
    command = "cat order.json | curl -X POST https://api.test/orders -H 'Content-Type: application/json' -d @- | jq .id"
    result = runner.invoke(app, ["import", "curl", command])
    assert _imported(result)["stages"][0]["request"] == {"method": "POST", "url": "/orders", "body": {"json": {"sku": "A-1"}}}
    assert result.stderr == "warning: Ignored what curl's output is piped to: jq .id\n"


def test_import_curl_warns_of_what_it_does_not_map():
    result = runner.invoke(app, ["import", "curl", "curl --retry 3 --frobnicate https://api.test/a"])
    _imported(result)
    assert result.stderr == "warning: Ignored the curl options the import does not map: --retry\nwarning: Ignored what is not a curl option: --frobnicate\n"


def test_import_reports_the_validators_warnings():
    """The scenario is validated as written; a warning is reported, and the
    scenario still written."""
    result = runner.invoke(app, ["import", "curl", """curl -H 'Content-Type: application/json' -d '{"{{k}}": 1}' https://api.test/a"""])
    _imported(result)
    assert "warning [HTTPCHAIN029]: " in result.stderr


@pytest.mark.parametrize(
    ("args", "error"),
    [
        pytest.param(["curl -sS"], "error: The curl command has no URL\n", id="no-url"),
        pytest.param(["curl 'https://api.test/a"], "error: The curl command has a single quote (') that is never closed\n", id="unclosed-quote"),
        pytest.param(["curl", "ftp://x.test/"], "error: The curl command's URL 'ftp://x.test/' is not http or https\n", id="not-http"),
        # Another program's command, whose words would be taken for URLs.
        pytest.param(["wget https://x.test/a"], "error: The command is not a curl command: 'wget https://x.test/a'\n", id="not-curl"),
        pytest.param(["wget", "https://x.test/a"], "error: The command is not a curl command: 'wget https://x.test/a'\n", id="not-curl-words"),
        # What no URL parser reads, and a file no file name can be.
        pytest.param(["curl -g 'http://[::1/x'"], "error: The curl command's URL 'http://[::1/x' is no URL: Invalid IPv6 URL\n", id="malformed-url"),
        pytest.param(
            ["curl 'http://[::1/x'"],
            "error: curl refuses the URL 'http://[::1/x' ([ is no range): curl reads {a,b} and [1-3] in a URL as sets and ranges, and -g sends the URL as written\n",
            id="malformed-glob",
        ),
        pytest.param(
            ["curl -d $'@a\\x00b' https://x.test/"],
            "error: The curl command reads the file 'a\\x00b', which cannot be: no file name holds a NUL character\n",
            id="nul-in-file-name",
        ),
    ],
)
def test_import_curl_of_what_is_no_command_exits_one(args, error):
    result = runner.invoke(app, ["import", "curl", *args])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == error


def test_import_of_what_would_not_validate_writes_nothing(tmp_path):
    """A scenario the validator refuses is not written, and the command
    fails saying why."""
    output = tmp_path / "test_x.http.json"
    result = runner.invoke(app, ["import", "curl", "-o", str(output), "curl -X 'NOT A TOKEN' https://api.test/a"])
    assert result.exit_code == 1
    assert "error [HTTPCHAIN000]: Schema validation failed: stages -> 0 -> request -> method" in result.stderr
    assert result.stderr.endswith("error: the imported scenario would not pass validate, so it was not written\n")
    assert not output.exists()


def test_import_writes_the_output_file(tmp_path):
    output = tmp_path / "test_imported.http.json"
    result = runner.invoke(app, ["import", "curl", "--output", str(output), "curl https://api.test/a"])
    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert runner.invoke(app, ["validate", "--strict", str(output)]).output == f"{output}: OK\n"


@pytest.mark.parametrize(("args", "exit_code", "content"), [pytest.param([], 1, "keep", id="refused"), pytest.param(["--force"], 0, None, id="forced")])
def test_import_overwrites_only_with_force(tmp_path, args, exit_code, content):
    output = tmp_path / "test_imported.http.json"
    output.write_text("keep")
    result = runner.invoke(app, ["import", "curl", "-o", str(output), *args, "curl https://api.test/a"])
    assert result.exit_code == exit_code, result.output
    if content is None:
        assert json.loads(output.read_text())["stages"][0]["name"] == "get_a"
    else:
        assert output.read_text() == content
        assert result.stderr == f"error: {output} exists; pass --force to overwrite it\n"


def test_import_into_a_missing_directory_exits_one(tmp_path):
    output = tmp_path / "missing" / "test_x.http.json"
    result = runner.invoke(app, ["import", "curl", "-o", str(output), "curl https://api.test/a"])
    assert result.exit_code == 1
    assert result.stderr.startswith(f"error: cannot write {output}: ")


def test_import_har():
    result = runner.invoke(app, ["import", "har", str(HAR_EXAMPLE)])
    scenario = _imported(result)
    assert scenario["description"] == "Imported from example.har"
    assert len(scenario["stages"]) == 7
    assert "note: Skipped 5 static assets: images, stylesheets, fonts and scripts the page loaded (--all keeps them)\n" in result.stderr
    assert "  COOKIE     the cookies prefs (stage get_root)\n" in result.stderr


def test_import_har_filters():
    result = runner.invoke(app, ["import", "har", "--all", "--include", "/api/", "--include", "static", "--exclude", r"\.png$", str(HAR_EXAMPLE)])
    assert [stage["name"] for stage in _imported(result)["stages"]] == [
        "get_static_app_css",
        "get_static_app_js",
        "get_static_vendor_js",
        "post_api_login",
        "get_api_me",
        "get_api_avatar_1",
        "post_api_search",
        "post_api_photos",
    ]


def test_import_har_reads_stdin():
    result = runner.invoke(app, ["import", "har", "-"], input=HAR_EXAMPLE.read_text())
    assert _imported(result)["description"] == "Imported from a HAR file"


def test_import_har_reads_stdin_with_a_byte_order_mark():
    """As a file may start with one, so may what stdin reads."""
    result = runner.invoke(app, ["import", "har", "-"], input=b"\xef\xbb\xbf" + HAR_EXAMPLE.read_bytes())
    assert _imported(result)["description"] == "Imported from a HAR file"


@pytest.mark.parametrize("option", ["--include", "--exclude"])
def test_import_har_refuses_a_pattern_that_is_no_regex(option):
    result = runner.invoke(app, ["import", "har", option, "(", str(HAR_EXAMPLE)])
    assert result.exit_code == 2
    assert "'(' is not a regular expression" in _ANSI.sub("", result.stderr)


@pytest.mark.parametrize(
    ("content", "error"),
    [
        pytest.param(b"{}", "error: {path} is not a HAR file: it has no log.entries list\n", id="not-a-har"),
        pytest.param(b"\xff\xfe", "error: cannot read {path}: ", id="not-utf8"),
        pytest.param(
            b'{"log": {"entries": [{"request": {"method": "GET", "url": "http://[::1/x"}, "response": {"status": 200}}]}}',
            "error: {path} is not a HAR file: its entry 1 has the URL 'http://[::1/x', which is no URL (Invalid IPv6 URL)\n",
            id="malformed-url",
        ),
    ],
)
def test_import_har_of_what_is_no_har_exits_one(tmp_path, content, error):
    path = tmp_path / "x.har"
    path.write_bytes(content)
    result = runner.invoke(app, ["import", "har", str(path)])
    assert result.exit_code == 1
    assert result.stderr.startswith(error.format(path=path))


def test_import_har_of_a_missing_file_exits_one(tmp_path):
    result = runner.invoke(app, ["import", "har", str(tmp_path / "missing.har")])
    assert result.exit_code == 1
    assert result.stderr.startswith(f"error: cannot read {tmp_path / 'missing.har'}: ")
