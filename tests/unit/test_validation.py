"""Unit tests for the shared scenario validator (pytest_httpchain.validation)."""

from pytest_httpchain.validation import validate_scenario


def test_valid_scenario(datadir):
    r = validate_scenario(datadir / "valid_scenario.json")
    assert r.valid is True
    assert r.errors == []
    assert r.scenario_info is not None
    assert r.scenario_info.num_stages == 2
    assert r.scenario_info.stage_names == ["get_user", "update_user"]
    assert "base_url" in r.scenario_info.vars_referenced
    assert "user_name" in r.scenario_info.vars_saved
    assert "base_url" in r.scenario_info.vars_defined
    # everything referenced is defined or saved -> no undefined warnings
    assert not any("undefined" in w.lower() for w in r.warnings), r.warnings


def test_invalid_json(datadir):
    r = validate_scenario(datadir / "invalid_json.json")
    assert r.valid is False
    assert any("JSON" in e for e in r.errors)


def test_missing_file(datadir):
    r = validate_scenario(datadir / "does_not_exist.json")
    assert r.valid is False
    assert any("not found" in e.lower() for e in r.errors)


def test_duplicate_stage_names(datadir):
    r = validate_scenario(datadir / "duplicate_stage_names.json")
    assert r.valid is False
    assert any("Duplicate stage names" in e for e in r.errors)


def test_undefined_variable_warns(datadir):
    r = validate_scenario(datadir / "undefined_variables.json")
    assert r.valid is True  # warning, not error
    assert any("undefined" in w.lower() for w in r.warnings)
    assert "undefined_var" in r.scenario_info.vars_referenced


def test_no_verify_warns(datadir):
    r = validate_scenario(datadir / "no_response_validation.json")
    assert r.valid is True
    assert any("no response validation" in w for w in r.warnings)


# --- Tier 0 regression tests: drift fixes (false positives / false negatives) ---


def test_parametrize_individual_names_not_undefined(datadir):
    """A param name injected by `parametrize.individual` is defined, not undefined."""
    r = validate_scenario(datadir / "parametrize_individual.json")
    assert r.valid is True
    assert "user_id" in r.scenario_info.vars_referenced
    assert "user_id" in r.scenario_info.vars_defined
    assert not any("undefined" in w.lower() for w in r.warnings), r.warnings


def test_parametrize_combinations_names_not_undefined(datadir):
    """Keys of every `parametrize.combinations` entry are defined."""
    r = validate_scenario(datadir / "parametrize_combinations.json")
    assert {"method", "code"} <= set(r.scenario_info.vars_defined)
    assert not any("undefined" in w.lower() for w in r.warnings), r.warnings


def test_parallel_foreach_names_not_undefined(datadir):
    """A param name injected by `parallel.foreach` is defined, not undefined."""
    r = validate_scenario(datadir / "parallel_foreach.json")
    assert "worker_id" in r.scenario_info.vars_defined
    assert not any("undefined" in w.lower() for w in r.warnings), r.warnings


def test_functions_substitution_names_not_undefined(datadir):
    """An alias declared in a `functions` substitution is callable, not undefined."""
    r = validate_scenario(datadir / "functions_substitution.json")
    assert "make_token" in r.scenario_info.vars_defined
    assert not any("undefined" in w.lower() for w in r.warnings), r.warnings


def test_fixture_var_conflict_is_error(datadir):
    r = validate_scenario(datadir / "fixture_var_conflict.json")
    assert r.valid is False
    assert any("Conflicting fixtures and vars" in e for e in r.errors)


def test_schema_error_is_error(datadir):
    r = validate_scenario(datadir / "schema_error.json")
    assert r.valid is False
    assert any("Schema validation failed" in e for e in r.errors)


def test_wrong_extension_warns(datadir):
    r = validate_scenario(datadir / "wrong_extension.txt")
    assert any("extension" in w.lower() for w in r.warnings)


def test_ambient_response_names_flagged(datadir):
    """response/status_code/body/etc. are NOT available to {{ }} templates;
    referencing them must be flagged as undefined (they only reach save/verify
    handlers, never the substitution engine)."""
    r = validate_scenario(datadir / "ambient_response.json")
    assert "status_code" in r.scenario_info.vars_referenced
    assert any("status_code" in w for w in r.warnings), r.warnings
