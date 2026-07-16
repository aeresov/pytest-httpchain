"""Unit tests for the shared scenario validator (pytest_httpchain.validation)."""

import json
from pathlib import Path

from pytest_httpchain.validation import DiagnosticCode, resolve_root_path, validate_scenario

# A stable importable directory so `userfuncs:<name>` refs resolve under --syspath.
USERFUNCS_DIR = Path(__file__).parent / "test_validation_userfuncs"


def _codes(result):
    return {d.code for d in result.diagnostics}


def test_template_builtins_is_single_source():
    """M14: the reference extractor (scoping, consumed by the validator) uses the
    canonical TEMPLATE_BUILTINS from the templates package, not a private copy
    that could drift out of sync with the engine."""
    from pytest_httpchain.scoping import TEMPLATE_BUILTINS as s
    from pytest_httpchain.templates import TEMPLATE_BUILTINS as t

    assert s is t


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


def test_nontemplate_verify_expression_warns(datadir):
    """M2: a verify expression that is a plain string (no {{ }}) is always truthy
    and asserts nothing — the validator must warn."""
    r = validate_scenario(datadir / "verify_nontemplate_expression.json")
    assert DiagnosticCode.NONTEMPLATE_EXPRESSION in _codes(r)


def test_template_verify_expression_no_warn(datadir):
    """A proper {{ }} verify expression does not trigger the no-op-expression warning."""
    r = validate_scenario(datadir / "verify_template_expression_ok.json")
    assert DiagnosticCode.NONTEMPLATE_EXPRESSION not in _codes(r)


def test_invalid_scenario_marker_is_error(datadir):
    """M6: a malformed scenario-level marker crashes collection at runtime, so the
    validator (the pre-flight CI gate) must report it as an error too."""
    r = validate_scenario(datadir / "invalid_scenario_marker.json")
    assert r.valid is False
    assert DiagnosticCode.INVALID_MARKER in _codes(r)


def test_invalid_stage_marker_is_error(datadir):
    r = validate_scenario(datadir / "invalid_stage_marker.json")
    assert r.valid is False
    assert DiagnosticCode.INVALID_MARKER in _codes(r)


def test_valid_markers_no_error(datadir):
    r = validate_scenario(datadir / "valid_markers.json")
    assert DiagnosticCode.INVALID_MARKER not in _codes(r)


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


def test_scenario_fixtures_available_to_stages(datadir):
    r = validate_scenario(datadir / "scenario_fixtures_available.json")
    assert r.valid is True
    assert DiagnosticCode.UNDEFINED_VAR not in _codes(r)
    assert "server" in r.scenario_info.fixtures


def test_toplevel_vars_key_is_rejected(datadir):
    # "vars" is not a Scenario field and the runtime never reads it; the model
    # forbids extra keys, so the file fails schema validation outright.
    r = validate_scenario(datadir / "toplevel_vars_unknown_key.json")
    assert r.valid is False
    assert DiagnosticCode.SCHEMA in _codes(r)
    assert any("vars" in e and "Extra inputs are not permitted" in e for e in r.errors)


def test_misspelled_request_field_is_rejected(datadir):
    # The flagship typo case: "headerz" must fail validation naming the key
    # and its location, instead of sending a request with no headers.
    r = validate_scenario(datadir / "request_field_typo.json")
    assert r.valid is False
    assert DiagnosticCode.SCHEMA in _codes(r)
    assert any("headerz" in e and "Extra inputs are not permitted" in e for e in r.errors)


def test_toplevel_schema_key_tolerated(datadir):
    # The documented editor-integration key is stripped by the loader before
    # the model (which forbids extras) ever sees it.
    r = validate_scenario(datadir / "schema_key_tolerated.json")
    assert r.valid is True
    assert r.errors == []


def test_scenario_fixture_shadows_save_warns(datadir):
    # Scenario fixtures are injected into every stage above the global context,
    # so a save under the same name can never be read back.
    r = validate_scenario(datadir / "scenario_fixture_shadows_save.json")
    assert r.valid is True
    assert DiagnosticCode.FIXTURE_SHADOWS_SAVE in _codes(r)
    assert any("token" in w for w in r.warnings)


def test_fixture_in_scenario_level_template_is_error(datadir):
    # Scenario-level substitutions/auth/ssl resolve against a context that never
    # includes fixture values — referencing one there is a guaranteed crash at
    # scenario initialization.
    r = validate_scenario(datadir / "scenario_template_fixture_ref.json")
    assert r.valid is False
    assert DiagnosticCode.FIXTURE_IN_SCENARIO_TEMPLATE in _codes(r)
    locations = {d.location for d in r.diagnostics if d.code == DiagnosticCode.FIXTURE_IN_SCENARIO_TEMPLATE}
    assert locations == {"substitutions", "ssl"}


def test_always_run_in_scope_refs_ok(datadir):
    # always_run may reference fixtures, scenario substitutions, and earlier saves.
    r = validate_scenario(datadir / "always_run_refs_ok.json")
    assert r.valid is True
    assert DiagnosticCode.UNDEFINED_VAR not in _codes(r)
    assert DiagnosticCode.FORWARD_REF not in _codes(r)


def test_always_run_out_of_scope_refs_warn(datadir):
    # always_run is evaluated before stage substitutions are processed, so a
    # stage-substitution name there is as unavailable as a plain typo.
    r = validate_scenario(datadir / "always_run_out_of_scope.json")
    assert r.valid is True
    messages = [d.message for d in r.diagnostics if d.code == DiagnosticCode.UNDEFINED_VAR]
    assert any("always_run references 'flag'" in m for m in messages), messages
    assert any("always_run references 'missing_name'" in m for m in messages), messages


def test_always_run_forward_refs_warn(datadir):
    # Saves from this or later stages don't exist yet when always_run resolves.
    r = validate_scenario(datadir / "always_run_forward_ref.json")
    assert r.valid is True
    messages = [d.message for d in r.diagnostics if d.code == DiagnosticCode.FORWARD_REF]
    assert any("'created' before it is saved (saved in stage 'create')" in m for m in messages), messages
    assert any("'self_saved', which is only saved in this stage's response" in m for m in messages), messages


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
    """parametrize VALUE expressions are resolved at collection time (pytest
    needs concrete parameter values) against scenario-level substitutions ONLY
    (carrier.create_test_class). Referencing a stage-level substitution there
    fails, so it must be flagged."""
    r = validate_scenario(datadir / "parametrize_value_stage_scope.json")
    assert any(d.code == DiagnosticCode.UNDEFINED_VAR and "stage_var" in d.message for d in r.diagnostics), r.diagnostics


def test_parametrize_value_referencing_scenario_scope_ok(datadir):
    """A parametrize value referencing a scenario-level substitution is valid."""
    r = validate_scenario(datadir / "parametrize_value_scenario_scope.json")
    assert not any("sv" in w for w in r.warnings), r.warnings


def test_parametrize_template_values_emit_phase_info(datadir):
    """Template parametrize VALUES opt the scenario into collection-time
    substitution resolution — surfaced as the HTTPCHAIN025 info diagnostic,
    which must not affect validity or the warning list (exempt from --strict)."""
    r = validate_scenario(datadir / "parametrize_template_values_info.json")
    infos = [d for d in r.diagnostics if d.code == DiagnosticCode.PARAMETRIZE_COLLECTION_RESOLUTION]
    assert len(infos) == 1, r.diagnostics
    assert infos[0].severity == "info"
    assert infos[0].location == "stages[0].parametrize"
    assert r.valid is True
    assert r.warnings == []


def test_duplicate_json_key_fails_validation(datadir):
    """A duplicated organizational key (dict-form response steps) is an error
    at load, not a silent last-wins that deletes the first verify step. It is
    reported as a JSON-content problem (HTTPCHAIN014), not a $ref error."""
    r = validate_scenario(datadir / "duplicate_json_key.json")
    assert r.valid is False
    assert any("Duplicate key 'check'" in e for e in r.errors), r.errors
    assert any(d.code == DiagnosticCode.INVALID_JSON for d in r.diagnostics), r.diagnostics
    assert not any(d.code == DiagnosticCode.REF_ERROR for d in r.diagnostics), r.diagnostics


def test_parametrize_template_ids_do_not_emit_phase_info(datadir):
    """`ids` are never substituted, so a template-looking string there must not
    trigger the HTTPCHAIN025 collection-time-resolution info."""
    r = validate_scenario(datadir / "parametrize_ids_template_no_info.json")
    assert not any(d.code == DiagnosticCode.PARAMETRIZE_COLLECTION_RESOLUTION for d in r.diagnostics), r.diagnostics


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


def test_deep_ssl_verify_ca_bundle_missing_warns(datadir):
    """A Path-valued ssl.verify (CA bundle) gets the same deep existence check
    as ssl.cert — the runtime resolves it scenario-relatively too."""
    r = validate_scenario(datadir / "deep_ssl_verify_missing.json", deep=True)
    assert r.valid is True
    assert DiagnosticCode.REFERENCED_FILE_NOT_FOUND in _codes(r)


def test_deep_schema_file_invalid_warns(datadir):
    r = validate_scenario(datadir / "deep_schema_invalid.json", deep=True)
    assert DiagnosticCode.SCHEMA_FILE_INVALID in _codes(r), r.diagnostics


class TestRootPathDefault:
    """The CLI's default $ref root (resolve_root_path) approximates pytest's
    rootpath: nearest ancestor with a project marker, else the file's parent."""

    def test_finds_project_marker(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")
        nested = tmp_path / "suites" / "api"
        nested.mkdir(parents=True)
        scenario = nested / "test_x.http.json"
        scenario.write_text("{}")

        assert resolve_root_path(scenario) == tmp_path

    def test_falls_back_to_file_parent(self, tmp_path):
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        scenario = nested / "test_x.http.json"
        scenario.write_text("{}")

        assert resolve_root_path(scenario) == nested

    def test_ref_above_scenario_dir_resolves_within_project_root(self, tmp_path):
        """A $ref reaching above the scenario's own tree but inside the project
        resolves by default — matching what pytest collection accepts."""
        (tmp_path / ".git").mkdir()
        shared = tmp_path / "shared"
        shared.mkdir()
        (shared / "common.json").write_text(json.dumps({"url": "http://server/x", "method": "GET"}))
        suite = tmp_path / "tests" / "api"
        suite.mkdir(parents=True)
        scenario_path = suite / "test_a.http.json"
        scenario_path.write_text(
            json.dumps(
                {
                    "stages": [
                        {
                            "name": "s",
                            "request": {"$ref": "../../shared/common.json"},
                            "response": [{"verify": {"status": 200}}],
                        }
                    ]
                }
            )
        )

        result = validate_scenario(scenario_path)

        assert result.valid is True
        assert result.errors == []


def test_ambiguous_ref_reported_as_diagnostic(tmp_path):
    """HTTPCHAIN026: a $ref matching files under both lookup bases (scenario
    dir and root path) is surfaced as a warning diagnostic, not a bare
    Python warning."""
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "fragment.json").write_text(json.dumps({"url": "http://server/root"}))
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "fragment.json").write_text(json.dumps({"url": "http://server/local"}))
    scenario_path = suite / "test_a.http.json"
    scenario_path.write_text(
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

    result = validate_scenario(scenario_path)

    assert result.valid is True  # a warning, not an error
    assert DiagnosticCode.AMBIGUOUS_REF in {d.code for d in result.diagnostics}
    assert any("file-relative wins" in w for w in result.warnings)


class TestResponseMetadataNamespace:
    """The reserved `response` namespace (task 25): in scope for response
    steps, out of scope elsewhere, and shadowing user names is warned about."""

    def _scenario(self, tmp_path, stages):
        path = tmp_path / "test_x.http.json"
        path.write_text(json.dumps({"stages": stages}))
        return path

    def test_response_refs_accepted_in_response_steps(self, tmp_path):
        path = self._scenario(
            tmp_path,
            [
                {
                    "name": "s",
                    "request": {"url": "http://server/x"},
                    "response": [
                        {"save": {"substitutions": [{"vars": {"req_id": "{{ response.headers['x-request-id'] }}"}}]}},
                        {"verify": {"expressions": ["{{ response.status == 200 }}"]}},
                    ],
                }
            ],
        )
        result = validate_scenario(path)
        assert DiagnosticCode.UNDEFINED_VAR not in _codes(result)

    def test_response_ref_in_request_still_flagged(self, tmp_path):
        path = self._scenario(
            tmp_path,
            [
                {
                    "name": "s",
                    "request": {"url": "http://server/x", "headers": {"x": "{{ response.status }}"}},
                    "response": [{"verify": {"status": 200}}],
                }
            ],
        )
        result = validate_scenario(path)
        assert DiagnosticCode.UNDEFINED_VAR in _codes(result)

    def test_user_name_shadowed_by_namespace_warns(self, tmp_path):
        path = self._scenario(
            tmp_path,
            [
                {
                    "name": "s",
                    "substitutions": [{"vars": {"response": "mine"}}],
                    "request": {"url": "http://server/x"},
                    "response": [{"verify": {"status": 200}}],
                }
            ],
        )
        result = validate_scenario(path)
        assert DiagnosticCode.RESERVED_NAME in _codes(result)

    def test_header_matcher_contradiction_is_error(self, tmp_path):
        path = self._scenario(
            tmp_path,
            [
                {
                    "name": "s",
                    "request": {"url": "http://server/x"},
                    "response": [{"verify": {"headers": {"x-h": {"contains": "a", "not_contains": "a"}}}}],
                }
            ],
        )
        result = validate_scenario(path)
        assert result.valid is False
        assert DiagnosticCode.CONTAINS_CONTRADICTION in _codes(result)
