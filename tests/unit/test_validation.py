"""Unit tests for the shared scenario validator (pytest_httpchain.validation)."""

from pathlib import Path

from pytest_httpchain.validation import DiagnosticCode, validate_scenario

# A stable importable directory so `userfuncs:<name>` refs resolve under --syspath.
USERFUNCS_DIR = Path(__file__).parent / "test_validation_userfuncs"


def _codes(result):
    return {d.code for d in result.diagnostics}


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


# --- Tier 1: structured diagnostics with stable codes ---


def test_diagnostics_carry_codes_and_severity(datadir):
    r = validate_scenario(datadir / "duplicate_stage_names.json")
    assert r.diagnostics, "expected structured diagnostics"
    dup = [d for d in r.diagnostics if d.code == DiagnosticCode.DUPLICATE_STAGE]
    assert dup, [d.code for d in r.diagnostics]
    assert dup[0].severity == "error"
    # every diagnostic message is mirrored into errors/warnings
    assert dup[0].message in r.errors


def test_schema_error_diagnostic_has_code_and_location(datadir):
    r = validate_scenario(datadir / "schema_error.json")
    assert any(d.code == DiagnosticCode.SCHEMA and d.severity == "error" for d in r.diagnostics)


# --- Tier 1: no-op verify ---


def test_noop_verify_warns(datadir):
    r = validate_scenario(datadir / "noop_verify.json")
    assert r.valid is True  # warning, not error
    noop = [d for d in r.diagnostics if d.code == DiagnosticCode.NOOP_VERIFY]
    assert noop, [d.code for d in r.diagnostics]
    assert noop[0].severity == "warning"
    # a no-op verify step still counts as a verify step -> NO_VERIFY must NOT fire
    assert not any(d.code == DiagnosticCode.NO_VERIFY for d in r.diagnostics)


# --- Tier 1: contradiction checks ---


def test_contains_not_contains_contradiction_is_error(datadir):
    r = validate_scenario(datadir / "contradiction_contains.json")
    assert r.valid is False
    diag = [d for d in r.diagnostics if d.code == DiagnosticCode.CONTAINS_CONTRADICTION]
    assert diag and diag[0].severity == "error"
    assert "ERROR" in diag[0].message


def test_matches_not_matches_contradiction_is_error(datadir):
    r = validate_scenario(datadir / "contradiction_matches.json")
    assert r.valid is False
    assert any(d.code == DiagnosticCode.MATCHES_CONTRADICTION and d.severity == "error" for d in r.diagnostics)


# --- Tier 1: order-aware dataflow (forward references) ---


def test_cross_stage_forward_reference_warns(datadir):
    """A variable referenced in an earlier stage but only saved in a later stage
    is a forward reference (ordering bug), distinct from a plain undefined typo."""
    r = validate_scenario(datadir / "forward_reference.json")
    assert r.valid is True  # warning, not error
    fwd = [d for d in r.diagnostics if d.code == DiagnosticCode.FORWARD_REF]
    assert fwd, [d.code for d in r.diagnostics]
    assert any("token" in d.message for d in fwd)
    # 'token' is saved somewhere, so it must NOT be reported as plain-undefined
    assert not any(d.code == DiagnosticCode.UNDEFINED_VAR and "token" in d.message for d in r.diagnostics)


def test_same_stage_request_forward_reference_warns(datadir):
    """A request that references a variable saved in its own response is a forward
    reference: the request runs before any save in the same stage."""
    r = validate_scenario(datadir / "same_stage_forward.json")
    assert r.valid is True
    fwd = [d for d in r.diagnostics if d.code == DiagnosticCode.FORWARD_REF]
    assert fwd and any("sid" in d.message for d in fwd)


def test_comprehension_loop_vars_not_flagged(datadir):
    """A comprehension target (`for x in ...`) is a local binding, not a context
    variable — it must not be reported as referenced/undefined."""
    r = validate_scenario(datadir / "comprehension_vars.json")
    assert "items" in r.scenario_info.vars_referenced
    assert "x" not in r.scenario_info.vars_referenced
    assert not any("undefined" in w.lower() for w in r.warnings), r.warnings


def test_parametrize_value_referencing_stage_scope_warns(datadir):
    """parametrize VALUE expressions are resolved at collection time against
    scenario-level substitutions ONLY (carrier.create_test_class). Referencing a
    stage-level substitution there fails at runtime, so it must be flagged."""
    r = validate_scenario(datadir / "parametrize_value_stage_scope.json")
    assert any(d.code == DiagnosticCode.UNDEFINED_VAR and "stage_var" in d.message for d in r.diagnostics), r.diagnostics


def test_parametrize_value_referencing_scenario_scope_ok(datadir):
    """A parametrize value referencing a scenario-level substitution is valid."""
    r = validate_scenario(datadir / "parametrize_value_scenario_scope.json")
    assert not any("sv" in w for w in r.warnings), r.warnings


def test_foreach_value_referencing_stage_scope_ok(datadir):
    """Unlike parametrize, parallel.foreach values are resolved at stage execution
    against the full local context, so referencing a stage substitution is valid."""
    r = validate_scenario(datadir / "foreach_value_stage_scope.json")
    assert not any("stage_n" in w for w in r.warnings), r.warnings


# --- Tier 2: deep validation (opt-in: imports, signatures, referenced files) ---


def test_deep_disabled_does_not_check_imports(datadir):
    """Without deep=True the validator never imports user code."""
    r = validate_scenario(datadir / "deep_import_missing.json")
    assert r.valid is True
    assert DiagnosticCode.IMPORT_FAILED not in _codes(r)


def test_deep_import_missing_function_warns(datadir):
    r = validate_scenario(datadir / "deep_import_missing.json", deep=True, syspaths=[USERFUNCS_DIR])
    assert r.valid is True  # deep findings are warnings, never errors
    assert DiagnosticCode.IMPORT_FAILED in _codes(r)


def test_deep_import_bad_module_warns(datadir):
    r = validate_scenario(datadir / "deep_import_bad_module.json", deep=True, syspaths=[USERFUNCS_DIR])
    assert any(d.code == DiagnosticCode.IMPORT_FAILED and "nosuchmodule_xyz" in d.message for d in r.diagnostics), r.diagnostics


def test_deep_import_ok_no_warning(datadir):
    r = validate_scenario(datadir / "deep_import_ok.json", deep=True, syspaths=[USERFUNCS_DIR])
    assert _codes(r).isdisjoint({DiagnosticCode.IMPORT_FAILED, DiagnosticCode.UNKNOWN_ARG, DiagnosticCode.MISSING_ARG}), r.diagnostics


def test_deep_signature_missing_required_arg(datadir):
    """A verify user_function gets `response` injected; an extra required arg is missing."""
    r = validate_scenario(datadir / "deep_sig_missing_arg.json", deep=True, syspaths=[USERFUNCS_DIR])
    assert any(d.code == DiagnosticCode.MISSING_ARG and "x" in d.message for d in r.diagnostics), r.diagnostics


def test_deep_signature_unknown_kwarg(datadir):
    r = validate_scenario(datadir / "deep_sig_unknown_kwarg.json", deep=True, syspaths=[USERFUNCS_DIR])
    assert any(d.code == DiagnosticCode.UNKNOWN_ARG and "y" in d.message for d in r.diagnostics), r.diagnostics


def test_deep_auth_required_missing(datadir):
    """auth functions are called with no injected args, so a required param is missing."""
    r = validate_scenario(datadir / "deep_auth_required_missing.json", deep=True, syspaths=[USERFUNCS_DIR])
    assert any(d.code == DiagnosticCode.MISSING_ARG and "token" in d.message for d in r.diagnostics), r.diagnostics


def test_deep_auth_positional_only_required_missing(datadir):
    """A required positional-only param can never be filled by the framework's
    keyword-only call convention, so it must be flagged (not silently passed)."""
    r = validate_scenario(datadir / "deep_auth_posonly.json", deep=True, syspaths=[USERFUNCS_DIR])
    assert any(d.code == DiagnosticCode.MISSING_ARG and "token" in d.message for d in r.diagnostics), r.diagnostics


def test_deep_templated_function_ref_skipped(datadir):
    """A function reference that is a template ({{ }}) cannot be resolved statically."""
    r = validate_scenario(datadir / "deep_templated_func.json", deep=True, syspaths=[USERFUNCS_DIR])
    assert DiagnosticCode.IMPORT_FAILED not in _codes(r), r.diagnostics


def test_deep_binary_file_exists_ok(datadir):
    r = validate_scenario(datadir / "deep_binary_exists.json", deep=True)
    assert DiagnosticCode.REFERENCED_FILE_NOT_FOUND not in _codes(r), r.diagnostics


def test_deep_binary_file_missing_warns(datadir):
    r = validate_scenario(datadir / "deep_binary_missing.json", deep=True)
    assert r.valid is True
    assert DiagnosticCode.REFERENCED_FILE_NOT_FOUND in _codes(r)


def test_deep_schema_file_invalid_warns(datadir):
    r = validate_scenario(datadir / "deep_schema_invalid.json", deep=True)
    assert DiagnosticCode.SCHEMA_FILE_INVALID in _codes(r), r.diagnostics
