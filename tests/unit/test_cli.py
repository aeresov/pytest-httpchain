"""Unit tests for the pytest-httpchain CLI."""

import importlib.metadata
import json
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
    result = runner.invoke(app, ["validate", str(ok_scenario), str(dup_scenario)])
    assert result.exit_code == 1, result.output
    assert result.output.startswith(f"{ok_scenario}: OK\n{dup_scenario}: INVALID\n")


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


@pytest.mark.parametrize(
    ("args", "exit_code", "reports_import"),
    [
        pytest.param([], 0, False, id="without-deep-never-imports"),
        # Deep findings are warnings, so they pass the gate unless --strict.
        pytest.param(["--deep"], 0, True, id="deep"),
        pytest.param(["--deep", "--strict"], 1, True, id="deep-strict"),
    ],
)
def test_validate_deep(tmp_path, args, exit_code, reports_import):
    f = _write(
        tmp_path / "deep.json",
        {"stages": [{"name": "s", "request": {"url": "https://x.test/a"}, "response": [{"verify": {"status": 200, "user_functions": ["userfuncs:does_not_exist"]}}]}]},
    )
    result = runner.invoke(app, ["validate", *args, "--syspath", str(USERFUNCS_DIR), str(f)])
    assert result.exit_code == exit_code, result.output
    assert ("does_not_exist" in result.output) is reports_import


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
