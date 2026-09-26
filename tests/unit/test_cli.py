"""Unit tests for the pytest-httpchain CLI."""

import importlib.metadata
import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pytest_httpchain.cli import app
from pytest_httpchain.schema import build_schema

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


def test_graph_of_a_stageless_scenario_is_still_valid_mermaid(tmp_path):
    """An empty `stages` list has no nodes; the output must stay a parseable
    flowchart with a comment rather than a bare header or a crash."""
    scenario = _write(tmp_path / "test_empty.http.json", {"stages": []})
    result = runner.invoke(app, ["graph", str(scenario)])
    assert result.exit_code == 0, result.output
    assert result.output == "flowchart TD\n    %% (no stages)\n"
