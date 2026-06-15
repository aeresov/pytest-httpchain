"""Unit tests for the pytest-httpchain CLI."""

import json
from pathlib import Path

from typer.testing import CliRunner

from pytest_httpchain.cli import app

runner = CliRunner()

USERFUNCS_DIR = Path(__file__).parent / "test_validation_userfuncs"


def _write(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data))
    return path


def _stage(name: str, url: str) -> dict:
    return {"name": name, "request": {"url": url}, "response": [{"verify": {"status": 200}}]}


def test_validate_ok_exit_zero(tmp_path):
    f = _write(tmp_path / "ok.json", {"stages": [_stage("s", "https://x.test/a")]})
    result = runner.invoke(app, ["validate", str(f)])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_schema_has_no_output_option(tmp_path):
    """The CLI follows the UNIX convention: data goes to stdout and the user
    redirects. The --output/-o option (and its 'Wrote ... to' chatter) is gone."""
    for flag in ("--output", "-o"):
        result = runner.invoke(app, ["schema", flag, str(tmp_path / "x.json")])
        assert result.exit_code == 2, result.output
        assert "No such option" in result.output


def test_resolve_has_no_output_option(tmp_path):
    (tmp_path / "common.json").write_text(json.dumps({"url": "https://x.test/shared"}))
    scenario = tmp_path / "test_x.http.json"
    scenario.write_text(json.dumps({"stages": [{"name": "s", "request": {"$include": "common.json"}, "response": [{"verify": {"status": 200}}]}]}))
    for flag in ("--output", "-o"):
        result = runner.invoke(app, ["resolve", flag, str(tmp_path / "out.json"), str(scenario)])
        assert result.exit_code == 2, result.output
        assert "No such option" in result.output


def test_validate_invalid_exit_one(tmp_path):
    f = _write(
        tmp_path / "bad.json",
        {"stages": [_stage("dup", "https://x.test/a"), _stage("dup", "https://x.test/b")]},
    )
    result = runner.invoke(app, ["validate", str(f)])
    assert result.exit_code == 1, result.output
    assert "INVALID" in result.output
    assert "Duplicate stage names" in result.output


def test_validate_reports_warnings(tmp_path):
    f = _write(
        tmp_path / "warn.json",
        {"stages": [{"name": "s", "request": {"url": "https://x.test/{{ ghost }}"}, "response": [{"verify": {"status": 200}}]}]},
    )
    result = runner.invoke(app, ["validate", str(f)])
    assert result.exit_code == 0, result.output
    assert "ghost" in result.output


def test_validate_multiple_files_one_bad_exits_one(tmp_path):
    good = _write(tmp_path / "good.json", {"stages": [_stage("s", "https://x.test/a")]})
    bad = _write(
        tmp_path / "bad.json",
        {"stages": [_stage("dup", "https://x.test/a"), _stage("dup", "https://x.test/b")]},
    )
    result = runner.invoke(app, ["validate", str(good), str(bad)])
    assert result.exit_code == 1, result.output


def test_validate_json_format_ok(tmp_path):
    f = _write(tmp_path / "ok.json", {"stages": [_stage("s", "https://x.test/a")]})
    result = runner.invoke(app, ["validate", "--format", "json", str(f)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["valid"] is True
    assert data["files"][0]["path"].endswith("ok.json")
    assert data["files"][0]["result"]["valid"] is True


def test_validate_json_format_reports_codes(tmp_path):
    f = _write(
        tmp_path / "bad.json",
        {"stages": [_stage("dup", "https://x.test/a"), _stage("dup", "https://x.test/b")]},
    )
    result = runner.invoke(app, ["validate", "--format", "json", str(f)])
    assert result.exit_code == 1, result.output
    data = json.loads(result.output)
    assert data["valid"] is False
    codes = [d["code"] for d in data["files"][0]["result"]["diagnostics"]]
    assert "HTTPCHAIN001" in codes


def _deep_scenario(tmp_path) -> Path:
    return _write(
        tmp_path / "deep.json",
        {"stages": [{"name": "s", "request": {"url": "https://x.test/a"}, "response": [{"verify": {"status": 200, "user_functions": ["userfuncs:does_not_exist"]}}]}]},
    )


def test_validate_deep_reports_import_warning_but_exits_zero(tmp_path):
    f = _deep_scenario(tmp_path)
    result = runner.invoke(app, ["validate", "--deep", "--syspath", str(USERFUNCS_DIR), str(f)])
    assert result.exit_code == 0, result.output  # deep findings are warnings
    assert "does_not_exist" in result.output


def test_validate_deep_strict_exits_one_on_warning(tmp_path):
    f = _deep_scenario(tmp_path)
    result = runner.invoke(app, ["validate", "--deep", "--strict", "--syspath", str(USERFUNCS_DIR), str(f)])
    assert result.exit_code == 1, result.output


def test_validate_without_deep_ignores_imports(tmp_path):
    f = _deep_scenario(tmp_path)
    result = runner.invoke(app, ["validate", str(f)])
    assert result.exit_code == 0, result.output
    assert "does_not_exist" not in result.output


SCHEMA_PATH = Path(__file__).resolve().parents[2] / "docs" / "schema" / "scenario.schema.json"


def test_schema_emits_valid_json():
    result = runner.invoke(app, ["schema"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["$schema"].startswith("https://json-schema.org")
    assert "JsonRef" in data["$defs"]


def test_build_schema_matches_committed():
    from pytest_httpchain.schema import build_schema

    committed = json.loads(SCHEMA_PATH.read_text())
    assert build_schema() == committed


def test_schema_rejects_typos_accepts_documented_keys():
    """The editor schema must catch misspelled keys while accepting the
    documented $schema key, reference directives, and ordinary scenarios."""
    import jsonschema

    from pytest_httpchain.schema import build_schema

    validator = jsonschema.Draft202012Validator(build_schema())

    # documented patterns stay valid
    validator.validate({"$schema": "https://aeresov.github.io/pytest-httpchain/schema/scenario.schema.json", "stages": []})
    validator.validate({"$include": "base.json"})
    validator.validate({"stages": [{"name": "s", "request": {"$include": "common.json"}}]})
    validator.validate({"stages": [{"$ref": "stage.json", "name": "override"}]})

    # reference objects are accepted at tagged-union positions too (these are
    # anyOf in the emitted schema; under oneOf a reference matched the JsonRef
    # branch of every member and was rejected as ambiguous)
    stage = {"name": "s", "request": {"url": "https://x.test/"}}
    validator.validate({"stages": [{**stage, "request": {"url": "https://x.test/", "body": {"$include": "body.json"}}}]})
    validator.validate({"stages": [{**stage, "response": [{"save": {"$merge": "save.json"}}]}]})
    validator.validate({"stages": [{**stage, "response": [{"$include": "step.json"}]}]})
    validator.validate({"stages": [{**stage, "substitutions": [{"$ref": "vars.json"}]}]})
    validator.validate({"stages": [{**stage, "parallel": {"$include": "parallel.json"}}]})
    validator.validate({"stages": [{**stage, "parametrize": [{"$include": "params.json"}]}]})

    # typos fail: misspelled keys no longer slip through the JsonRef branch
    assert not validator.is_valid({"stages": [{"naem": "s", "requst": {"url": "https://x.test/"}}]})
    assert not validator.is_valid({"stages": [{"name": "s", "request": {"url": "https://x.test/", "headerz": {}}}]})
    # stage-level typo with an otherwise-complete stage, so additionalProperties
    # on Stage (not a missing required "request") is what rejects it
    assert not validator.is_valid({"stages": [{**stage, "alwaysrun": True}]})
    assert not validator.is_valid({"stages": [{**stage, "response": [{"save": {"jmespth": {"x": "y"}}}]}]})
    assert not validator.is_valid({"stagez": []})


def test_schema_rejects_type_typos_in_template_fields():
    """L7: fields that accept a template should not accept arbitrary strings.
    A non-template string that is also not a valid value for the field's concrete
    type is rejected, while templates, concrete values, and the stringified
    concretes the runtime coerces are all still accepted (no false positives)."""
    import jsonschema

    from pytest_httpchain.schema import build_schema

    v = jsonschema.Draft202012Validator(build_schema())

    def req(**kw):
        return {"stages": [{"name": "s", "request": {"url": "https://x.test/", **kw}}]}

    def verify(**kw):
        return {"stages": [{"name": "s", "request": {"url": "https://x.test/"}, "response": [{"verify": kw}]}]}

    # type-mismatched non-template strings are now flagged
    assert not v.is_valid(req(timeout="abc"))
    assert not v.is_valid(req(method="FOOBAR"))
    assert not v.is_valid(verify(status="not-a-status"))

    # templates remain valid
    assert v.is_valid(req(timeout="{{ t }}"))
    assert v.is_valid(req(method="{{ m }}"))
    assert v.is_valid(verify(status="{{ s }}"))

    # concrete values remain valid
    assert v.is_valid(req(timeout=30))
    assert v.is_valid(req(method="GET"))
    assert v.is_valid(verify(status=200))

    # stringified concretes the runtime coerces must NOT become false positives
    assert v.is_valid(req(timeout="30"))
    assert v.is_valid(verify(status="200"))


def test_resolve_inlines_include(tmp_path):
    (tmp_path / "common.json").write_text(json.dumps({"url": "https://x.test/shared"}))
    scenario = tmp_path / "test_x.http.json"
    scenario.write_text(json.dumps({"stages": [{"name": "s", "request": {"$include": "common.json"}, "response": [{"verify": {"status": 200}}]}]}))
    result = runner.invoke(app, ["resolve", str(scenario)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["stages"][0]["request"]["url"] == "https://x.test/shared"


def test_resolve_missing_ref_exits_one(tmp_path):
    scenario = tmp_path / "test_x.http.json"
    scenario.write_text(json.dumps({"stages": [{"name": "s", "request": {"$include": "nope.json"}, "response": [{"verify": {"status": 200}}]}]}))
    result = runner.invoke(app, ["resolve", str(scenario)])
    assert result.exit_code == 1


def _chain_scenario(tmp_path) -> Path:
    scenario = tmp_path / "test_chain.http.json"
    scenario.write_text(
        json.dumps(
            {
                "stages": [
                    {
                        "name": "create",
                        "request": {"url": "https://x.test/u", "method": "POST"},
                        "response": [{"save": {"jmespath": {"user_id": "id"}}}, {"verify": {"status": 201}}],
                    },
                    {"name": "get", "request": {"url": "https://x.test/u/{{ user_id }}"}, "response": [{"verify": {"status": 200}}]},
                ]
            }
        )
    )
    return scenario


def test_show_text_reports_dataflow(tmp_path):
    result = runner.invoke(app, ["show", str(_chain_scenario(tmp_path))])
    assert result.exit_code == 0, result.output
    assert "consumes" in result.output
    assert "from #1" in result.output


def test_show_json_exposes_edges(tmp_path):
    result = runner.invoke(app, ["show", "--format", "json", str(_chain_scenario(tmp_path))])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert any(e["vars"] == ["user_id"] for e in data["edges"])


def test_show_invalid_scenario_exits_one(tmp_path):
    scenario = tmp_path / "bad.json"
    scenario.write_text(json.dumps({"stages": [{"name": "s", "request": {}}]}))
    result = runner.invoke(app, ["show", str(scenario)])
    assert result.exit_code == 1


def test_graph_emits_mermaid(tmp_path):
    result = runner.invoke(app, ["graph", str(_chain_scenario(tmp_path))])
    assert result.exit_code == 0, result.output
    assert "flowchart TD" in result.output
    assert "-->|user_id|" in result.output


def test_graph_direction_lr(tmp_path):
    result = runner.invoke(app, ["graph", "--direction", "LR", str(_chain_scenario(tmp_path))])
    assert result.exit_code == 0, result.output
    assert "flowchart LR" in result.output


def test_show_reports_scenario_fixtures_and_vars(tmp_path):
    scenario = tmp_path / "test_meta.http.json"
    scenario.write_text(
        json.dumps(
            {
                "fixtures": ["server"],
                "substitutions": [{"vars": {"base_url": "https://x.test", "api_key": "k"}}],
                "stages": [{"name": "s", "request": {"url": "{{ base_url }}/u"}, "response": [{"verify": {"status": 200}}]}],
            }
        )
    )
    result = runner.invoke(app, ["show", str(scenario)])
    assert result.exit_code == 0, result.output
    assert "server" in result.output
    assert "base_url" in result.output
    assert "api_key" in result.output

    rj = runner.invoke(app, ["show", "--format", "json", str(scenario)])
    data = json.loads(rj.output)
    assert data["scenario_fixtures"] == ["server"]
    assert data["scenario_vars"] == ["api_key", "base_url"]
