"""The shared scenario validator (pytest_httpchain.validation), file level.

Each file under ``test_validation/`` is a minimal scenario built to trigger
exactly one finding (or none), so the two tables below pin the complete
diagnostic list — code, location and message — rather than the presence of
one code: a check that starts firing on a neighbour's fixture fails here too.
"""

import json
import re
import sys
from pathlib import Path

import pytest

import pytest_httpchain.validation.loader as validation_loader
from pytest_httpchain.models import JMESPathMatcher
from pytest_httpchain.validation import SEVERITY, DiagnosticCode, load_scenario, resolve_root_path, validate_scenario
from tests.unit.helpers import LOADABLE_BUT_DEEP, TOO_DEEP_TO_PARSE, TOO_DEEP_TO_WALK, nested, on_bounded_stack

C = DiagnosticCode
# A stable importable directory so `userfuncs:<name>` refs resolve under --syspath.
USERFUNCS_DIR = Path(__file__).parent / "test_validation_userfuncs"
DOCS_PAGE = Path(__file__).resolve().parents[2] / "docs" / "diagnostics.md"


def _validate(datadir, fixture):
    """Fixtures named ``deep_*`` exercise deep validation (imports, signatures, files)."""
    deep = fixture.startswith("deep_")
    return validate_scenario(datadir / fixture, deep=deep, syspaths=[USERFUNCS_DIR] if deep else None)


def _codes(result):
    return [d.code for d in result.diagnostics]


def _stage(**fields):
    return {"name": "s", "request": {"url": "http://server/x"}, "response": [{"verify": {"status": 200}}], **fields}


def _write(directory, stages, **top):
    path = directory / "test_x.http.json"
    path.write_text(json.dumps({"stages": stages, **top}))
    return path


def _hide_ancestor_project_markers(monkeypatch):
    """Make markerless-root tests independent of TMPDIR's real ancestors."""
    monkeypatch.setattr(validation_loader, "_ROOT_MARKERS", ())
    monkeypatch.setattr(validation_loader, "_holds_pytest_config", lambda directory: False)


@pytest.mark.parametrize(
    "fixture",
    [
        "valid_scenario.json",
        "valid_markers.json",
        "verify_template_expression_ok.json",
        # jmespath alone asserts something (no HTTPCHAIN006), and its values
        # see the response namespace and a prior step's save.
        "verify_jmespath_ok.json",
        "schema_key_tolerated.json",  # the editor-integration "$schema" key is stripped by the loader
        # Names a template may reference without being flagged undefined:
        "parametrize_individual.json",
        "parametrize_combinations.json",
        "parallel_foreach.json",
        "functions_substitution.json",  # a `functions` alias is callable
        "scenario_fixtures_available.json",
        "comprehension_vars.json",  # a comprehension target is a local binding
        "always_run_refs_ok.json",  # fixtures, scenario substitutions, earlier saves
        # foreach values resolve at execution against the full local context,
        # unlike parametrize values, so a stage substitution is in scope.
        "foreach_value_stage_scope.json",
        # `ids` are never substituted: no collection-resolution info, no
        # undefined-variable warning for display-only text.
        "parametrize_ids_template_no_info.json",
        # Steps resolve in order; each may read what an EARLIER step produced.
        "substitution_prior_step_ok.json",
        "response_step_prior_save_ok.json",
        # process_save renders a substitutions save's entries itself, in order.
        "response_save_substitutions_sequential.json",
        # A user_functions save returns arbitrary keys, so nothing after it can
        # be called a forward reference.
        "response_opaque_save_no_false_forward_ref.json",
        "substitution_function_kwargs_literal_ok.json",  # only a template is dead text
        # client.base_url (templated from scenario substitutions) completes a
        # relative URL, literal or after a template; an absolute one ignores it.
        "relative_url_with_base_url.json",
        # $ref/$defs inside verify.body.schema are JSON Schema vocabulary, in
        # every response-step shape and through a spliced-in stage fragment.
        "inline_schema_standard_ref.json",
        "inline_schema_dict_form.json",
        "inline_schema_mapping_form_response.json",
        "inline_schema_fragment_stage.json",
        # An '$include' PROPERTY maps to a schema object, never a string.
        "inline_schema_property_named_include.json",
        "deep_import_ok.json",
        # The built-in schemes import nothing, and their templates are in
        # scope: scenario substitutions at scenario level, an earlier save in
        # a request's.
        "deep_auth_builtins.json",
        "deep_binary_exists.json",
        "deep_templated_func.json",  # a templated reference cannot be resolved statically
        "deep_sig_var_keyword.json",  # **kwargs makes every supplied name fillable
        # Substitution functions are called from templates with arguments the
        # validator cannot see: importing is the whole check.
        "deep_substitution_function_required_arg.json",
        # Some C callables have no retrievable signature; that is not a bad call.
        "deep_non_introspectable_func.json",
    ],
)
def test_clean_fixture_has_no_diagnostics(datadir, fixture):
    result = _validate(datadir, fixture)
    assert result.diagnostics == []
    assert result.valid is True


DIAGNOSED = [
    ("does_not_exist.json", [(C.FILE_NOT_FOUND, None, "File not found")]),
    ("invalid_json.json", [(C.INVALID_JSON, None, "Invalid JSON syntax")]),
    # A duplicated organizational key is a JSON-content error at load, not
    # a silent last-wins, and not a $ref error.
    ("duplicate_json_key.json", [(C.INVALID_JSON, None, "Duplicate key 'check'")]),
    # Saved as Latin-1. RFC 8259 requires UTF-8, so the file is invalid JSON,
    # like a syntax error, and not an unexplained parse failure.
    ("not_utf8.json", [(C.INVALID_JSON, None, r"^Invalid JSON: .*not_utf8\.json is not valid UTF-8: 'utf-8' codec can't decode byte 0xe9")]),
    ("schema_error.json", [(C.SCHEMA, "stages -> 0 -> request", "Field required")]),
    # Models forbid extra keys: a typo fails naming the key and its location.
    ("request_field_typo.json", [(C.SCHEMA, "stages -> 0 -> request -> headerz", "Extra inputs are not permitted")]),
    ("toplevel_vars_unknown_key.json", [(C.SCHEMA, "vars", "Extra inputs are not permitted")]),
    ("duplicate_stage_names.json", [(C.DUPLICATE_STAGE, "stages", r"\['dup'\]")]),
    ("fixture_var_conflict.json", [(C.FIXTURE_CONFLICT, None, r"\['token'\]")]),
    # M6: a malformed marker crashes collection, so the pre-flight gate errors too.
    ("invalid_scenario_marker.json", [(C.INVALID_MARKER, "marks", r"'skip\('")]),
    ("invalid_stage_marker.json", [(C.INVALID_MARKER, "stages[0].marks", "'foo.bar'")]),
    ("invalid_marker_unpacking.json", [(C.INVALID_MARKER, "stages[0].marks", r"\*\* unpacking")]),
    # xdist joins a stage's own group with the scenario's, so --dist loadgroup
    # ran the stage on another worker than the rest of its chain.
    ("stage_xdist_group_mark.json", [(C.STAGE_XDIST_GROUP, "stages[0].marks", r"marker \"xdist_group\('db'\)\"")]),
    # xdist drops a group whose name has a ']' after its last '@', so every
    # stage became a work unit of its own under --dist loadgroup.
    ("scenario_xdist_group_bracket.json", [(C.UNREADABLE_XDIST_GROUP, "marks", r"Remove the '\]' from 'db\[main\]'")]),
    # The name lands in the node id: pytest cannot select the stage by it, and
    # --dist loadscope cut the chain at its last '::'.
    ("stage_name_node_id_separator.json", [(C.NODE_ID_SEPARATOR_IN_STAGE_NAME, "stages[0].name", r"'Users::list' contains '::'")]),
    ("contradiction_contains.json", [(C.CONTAINS_CONTRADICTION, "stages[0].response[0].verify.body", r"substring\(s\): \['ERROR'\]")]),
    ("contradiction_matches.json", [(C.MATCHES_CONTRADICTION, "stages[0].response[0].verify.body", r"pattern\(s\): \['\^OK\$'\]")]),
    # A jmespath matcher holds one operand per key; contains operands are JSON
    # values, compared as the check compares them (1 is 1.0).
    (
        "contradiction_jmespath_contains.json",
        [
            (
                C.CONTAINS_CONTRADICTION,
                'stages[0].response[0].verify.jmespath["users[*].id"]',
                r"jmespath 'users\[\*\]\.id' verification both requires and forbids 1 \(contains and not_contains\)",
            )
        ],
    ),
    (
        "contradiction_jmespath_matches.json",
        [
            (
                C.MATCHES_CONTRADICTION,
                "stages[0].response[0].verify.jmespath.name",
                r"""jmespath 'name' verification both requires and forbids pattern "\^A" \(matches and not_matches\)""",
            )
        ],
    ),
    # The scenario-level context never includes fixture values: a guaranteed
    # crash at scenario initialization.
    (
        "scenario_template_fixture_ref.json",
        [(C.FIXTURE_IN_SCENARIO_TEMPLATE, "substitutions", r"\['srv'\]"), (C.FIXTURE_IN_SCENARIO_TEMPLATE, "ssl", r"\['ca_path'\]")],
    ),
    # `client` resolves once per scenario, like ssl and auth: no fixtures, and
    # only scenario substitutions.
    ("client_template_fixture_ref.json", [(C.FIXTURE_IN_SCENARIO_TEMPLATE, "client", r"'client' templates: \['token'\]")]),
    ("client_template_undefined.json", [(C.SCENARIO_UNDEFINED_VAR, "client", r"'client' templates: \['api_root'\]")]),
    # A built-in auth's credentials are templates like any other: a scenario's
    # resolve against scenario substitutions, a request's in the request's scope.
    ("scenario_auth_template_undefined.json", [(C.SCENARIO_UNDEFINED_VAR, "auth", r"'auth' templates: \['token'\]")]),
    ("request_auth_forward_reference.json", [(C.FORWARD_REF, "stages[0].request", r"'token' is referenced before it is saved \(saved in stage 'login'\)")]),
    # Nothing to turn off at scenario level: the message says where it belongs.
    ("scenario_auth_false.json", [(C.SCHEMA, "auth", "false turns the scenario's auth off for one stage, so it belongs in a stage's request")]),
    # A relative URL has nowhere to go without client.base_url, whether all of
    # it is literal or only the text before its first template.
    ("relative_url_without_base_url.json", [(C.RELATIVE_URL_WITHOUT_BASE_URL, "stages[0].request.url", r"'/users/1' is relative, but the scenario sets no client.base_url")]),
    (
        "relative_url_template_after_prefix.json",
        [(C.RELATIVE_URL_WITHOUT_BASE_URL, "stages[0].request.url", r"'users/\{\{ user_id \}\}' is relative, but the scenario sets no client.base_url")],
    ),
    ("undefined_variables.json", [(C.UNDEFINED_VAR, "stages[0].request", r"\['undefined_var'\]")]),
    # response/status_code/body only reach save/verify handlers, never templates.
    ("ambient_response.json", [(C.UNDEFINED_VAR, "stages[0].response", r"\['status_code'\]")]),
    ("no_response_validation.json", [(C.NO_VERIFY, "stages[0]", "no response validation")]),
    # A no-op verify step still counts as a verify step: NO_VERIFY stays quiet.
    ("noop_verify.json", [(C.NOOP_VERIFY, "stages[0].response[0].verify", "asserts nothing")]),
    # An object is a matcher, always: one written for equality is told to use eq.
    (
        "verify_jmespath_literal_object.json",
        [(C.SCHEMA, "stages -> 0 -> response -> 0 -> verify -> verify -> jmespath -> meta -> matcher", r"'page' is not one of its keys .*give it as eq")],
    ),
    # M2: a plain-string expression is never the bool an expression must be.
    ("verify_nontemplate_expression.json", [(C.NONTEMPLATE_EXPRESSION, "stages[0].response[0].verify", "is not a template")]),
    # Scenario fixtures sit above the global context: the save is unreadable.
    ("scenario_fixture_shadows_save.json", [(C.FIXTURE_SHADOWS_SAVE, None, r"\['token'\]")]),
    # Saved later is an ordering bug, reported as such rather than as undefined.
    ("forward_reference.json", [(C.FORWARD_REF, "stages[0].request", "'token' is referenced before it is saved")]),
    ("same_stage_forward.json", [(C.FORWARD_REF, "stages[0].request", "'sid', which is only saved in this stage's response")]),
    ("substitution_intra_list_forward.json", [(C.FORWARD_REF, "stages[0].substitutions", "'b' before the substitution step that defines it")]),
    ("response_step_forward.json", [(C.FORWARD_REF, "stages[0].response", "'token' before the save that produces it")]),
    # One unavailable name is one finding, however many steps reference it.
    ("substitution_duplicate_forward_refs.json", [(C.FORWARD_REF, "stages[0].substitutions", "'t' before")]),
    ("response_duplicate_forward_refs.json", [(C.FORWARD_REF, "stages[0].response", "'token' before")]),
    # always_run is evaluated before stage substitutions and before the stage runs.
    (
        "always_run_out_of_scope.json",
        [(C.UNDEFINED_VAR, "stages[1].always_run", "always_run references 'flag'"), (C.UNDEFINED_VAR, "stages[1].always_run", "always_run references 'missing_name'")],
    ),
    (
        "always_run_forward_ref.json",
        [
            (C.FORWARD_REF, "stages[0].always_run", r"'created' before it is saved \(saved in stage 'create'\)"),
            (C.FORWARD_REF, "stages[1].always_run", "'self_saved', which is only saved in this stage's response"),
        ],
    ),
    # Template parametrize VALUES resolve at collection against scenario
    # substitutions only: an info that affects neither validity nor warnings,
    # plus an undefined-variable warning for a stage-scope name.
    ("parametrize_template_values_info.json", [(C.PARAMETRIZE_COLLECTION_RESOLUTION, "stages[0].parametrize", "resolve at collection time")]),
    ("parametrize_value_scenario_scope.json", [(C.PARAMETRIZE_COLLECTION_RESOLUTION, "stages[0].parametrize", "resolve at collection time")]),
    (
        "parametrize_value_stage_scope.json",
        [(C.UNDEFINED_VAR, "stages[0].parametrize", "'stage_var'"), (C.PARAMETRIZE_COLLECTION_RESOLUTION, "stages[0].parametrize", "resolve at collection time")],
    ),
    # $include/$merge are never JSON Schema keywords, and a non-'#' $ref can
    # never resolve at runtime: both are migration leftovers.
    ("inline_schema_scenario_directive.json", [(C.SCHEMA_SCENARIO_DIRECTIVE, "stages[0].response[0].verify.body.schema", r"\['\$include'\]")]),
    ("inline_schema_legacy_file_ref.json", [(C.SCHEMA_SCENARIO_DIRECTIVE, "stages[0].response[0].verify.body.schema", r"\['\$ref'\]")]),
    # functions-substitution kwargs reach wrap_function raw: a template there
    # is dead text (and, not being rendered, not a data-flow reference).
    ("substitution_function_kwargs_template_ok.json", [(C.TEMPLATE_IN_KWARGS, "stages[0].substitutions", "'helper' kwarg 'arg'")]),
    ("scenario_function_kwargs_template.json", [(C.TEMPLATE_IN_KWARGS, "substitutions", "'seed' kwarg 'arg'")]),
    ("response_save_function_kwargs_template.json", [(C.TEMPLATE_IN_KWARGS, "stages[0].response[1].save.substitutions", "'extract' kwarg 'path'")]),
    # Deep findings (opt-in) are warnings, never errors.
    ("deep_import_missing.json", [(C.IMPORT_FAILED, "stages[0].response[0].verify.user_functions[0]", "'does_not_exist' not found")]),
    ("deep_import_bad_module.json", [(C.IMPORT_FAILED, "stages[0].request.auth", "nosuchmodule_xyz")]),
    # M-review: substitutions saves declare functions too.
    ("deep_save_substitutions_missing.json", [(C.IMPORT_FAILED, "stages[0].response[0].save.substitutions.functions.tok", "nosuchmodule_subsave")]),
    # verify and save user_functions get `response` injected; anything else required is missing.
    ("deep_sig_missing_arg.json", [(C.MISSING_ARG, "stages[0].response[0].verify.user_functions[0]", "missing required argument 'x'")]),
    ("deep_save_user_function_sig.json", [(C.MISSING_ARG, "stages[0].response[1].save.user_functions[0]", "missing required argument 'x'")]),
    ("deep_sig_unknown_kwarg.json", [(C.UNKNOWN_ARG, "stages[0].response[0].verify.user_functions[0]", "unexpected argument 'y'")]),
    # auth gets nothing injected, and the keyword-only call convention can
    # never fill a positional-only parameter.
    ("deep_auth_required_missing.json", [(C.MISSING_ARG, "auth", "missing required argument 'token'")]),
    ("deep_auth_posonly.json", [(C.MISSING_ARG, "auth", "missing required argument 'token'")]),
    ("deep_binary_missing.json", [(C.REFERENCED_FILE_NOT_FOUND, "stages[0].request.body.binary", "definitely_missing_file")]),
    # Named per field / per element, not once for the whole value.
    ("deep_files_missing.json", [(C.REFERENCED_FILE_NOT_FOUND, "stages[0].request.body.files.absent", "definitely_missing_upload")]),
    ("deep_ssl_cert_pair_missing.json", [(C.REFERENCED_FILE_NOT_FOUND, "ssl.cert[0]", r"\.crt"), (C.REFERENCED_FILE_NOT_FOUND, "ssl.cert[1]", r"\.key")]),
    ("deep_ssl_verify_missing.json", [(C.REFERENCED_FILE_NOT_FOUND, "ssl.verify", "ca-bundel.pem")]),
    # Three schema-file outcomes: missing, unparseable, parseable but not a schema.
    ("deep_schema_file_missing.json", [(C.REFERENCED_FILE_NOT_FOUND, "stages[0].response[0].verify.body.schema", "Schema file not found")]),
    ("deep_schema_invalid.json", [(C.SCHEMA_FILE_INVALID, "stages[0].response[0].verify.body.schema", "is not valid JSON")]),
    ("deep_schema_not_a_schema.json", [(C.SCHEMA_FILE_INVALID, "stages[0].response[0].verify.body.schema", "not a valid JSON Schema")]),
]


@pytest.mark.parametrize(("fixture", "expected"), [pytest.param(*row, id=row[0].removesuffix(".json")) for row in DIAGNOSED])
def test_fixture_diagnostics(datadir, fixture, expected):
    result = _validate(datadir, fixture)

    assert [(d.code, d.location) for d in result.diagnostics] == [(code, location) for code, location, _ in expected]
    for diagnostic, (_, _, pattern) in zip(result.diagnostics, expected, strict=True):
        assert re.search(pattern, diagnostic.message), diagnostic.message
    # Every message is mirrored into errors/warnings by severity; info is neither.
    assert result.errors == [d.message for d in result.diagnostics if d.severity == "error"]
    assert result.warnings == [d.message for d in result.diagnostics if d.severity == "warning"]
    assert result.valid is not any(SEVERITY[code] == "error" for code, _, _ in expected)


def test_valid_scenario_info(datadir):
    info = validate_scenario(datadir / "valid_scenario.json").scenario_info
    assert info.num_stages == 2
    assert info.stage_names == ["get_user", "update_user"]
    assert "base_url" in info.vars_referenced
    assert "base_url" in info.vars_defined
    assert "user_name" in info.vars_saved


def test_deep_disabled_does_not_check_imports(datadir):
    """Without deep=True the validator never imports user code."""
    assert validate_scenario(datadir / "deep_import_missing.json").diagnostics == []


@pytest.mark.parametrize(
    ("content", "message"),
    [
        # The decoder's RecursionError is not a ValueError.
        pytest.param(TOO_DEEP_TO_PARSE, "Schema file is not valid JSON: .*while decoding a JSON array", id="too-deep-to-parse"),
        pytest.param(b'{"items": ' * 1_000 + b"{}" + b"}" * 1_000, "not a valid JSON Schema", id="too-deep-to-check"),
        # The meta-check fails cleanly, but the error's str() pretty-prints the
        # deep `type` value, inside the except clause.
        pytest.param(b'{"type": ' + TOO_DEEP_TO_WALK + b"}", "not a valid JSON Schema: .* is not valid under any of the given schemas", id="too-deep-to-describe"),
    ],
)
def test_deep_schema_file_nested_too_deeply(tmp_path, content, message):
    """Generated, not a ``test_validation/`` fixture: the payloads are too big to commit."""
    (tmp_path / "schema.json").write_bytes(content)
    result = on_bounded_stack(validate_scenario, _write(tmp_path, [_stage(response=[{"verify": {"body": {"schema": "schema.json"}}}])]), deep=True)

    assert [(d.code, d.location) for d in result.diagnostics] == [(C.SCHEMA_FILE_INVALID, "stages[0].response[0].verify.body.schema")]
    assert re.search(message, result.diagnostics[0].message)


def test_wrong_extension_warns(datadir):
    result = validate_scenario(datadir / "wrong_extension.txt")
    assert _codes(result) == [C.WRONG_EXTENSION]
    assert result.valid is True


def test_directory_is_not_a_file(tmp_path):
    assert _codes(validate_scenario(tmp_path)) == [C.NOT_A_FILE]


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        # The decoder's RecursionError is not a ValueError.
        pytest.param(TOO_DEEP_TO_PARSE, r"\(.*while decoding a JSON array", id="too-deep-to-parse"),
        # Parses, but the resolver's own walk spends a frame per level.
        pytest.param(TOO_DEEP_TO_WALK, r"\(maximum recursion depth exceeded\)$", id="too-deep-to-walk"),
    ],
)
def test_file_nested_too_deeply_is_a_parse_error(tmp_path, content, reason):
    """Valid JSON, but deeper than the parser or the resolver can go: PARSE_ERROR,
    not INVALID_JSON. Generated, not a ``test_validation/`` fixture: the payloads
    are too big to commit."""
    path = tmp_path / "test_x.http.json"
    path.write_bytes(b'{"stages": ' + content + b"}")
    result = on_bounded_stack(validate_scenario, path)

    assert [(d.code, d.location) for d in result.diagnostics] == [(C.PARSE_ERROR, None)]
    assert re.search(f"^Failed to parse JSON file: nested too deeply {reason}", result.diagnostics[0].message)


@pytest.mark.parametrize(
    ("fixture", "step"),
    [
        ("inline_schema_standard_ref.json", lambda raw: raw["stages"][0]["response"][0]),
        # The name-keyed `response` form sits one level deeper in the raw JSON,
        # and the opacity check addresses raw paths: it must know both shapes, or
        # the resolver loads the schema's own `#/$defs/item` as a file.
        ("inline_schema_mapping_form_response.json", lambda raw: raw["stages"][0]["response"]["checks"][0]),
    ],
    ids=["list-form", "mapping-form"],
)
def test_inline_schema_reaches_the_model_untouched(datadir, fixture, step):
    _, raw = load_scenario(datadir / fixture)
    schema = step(raw)["verify"]["body"]["schema"]
    assert schema["$defs"] == {"item": {"type": "string"}}
    assert schema["properties"]["item"] == {"$ref": "#/$defs/item"}


class TestStatusListMerge:
    """A verify.status list holds alternatives, so a sibling list merged onto a
    fragment's is not concatenated, which would widen the check (a negative
    test's [404] beside a shared ["2xx"] passed on a 200): it merges as a
    scalar does, equal keeps and different is a merge conflict, in every shape
    a step can take."""

    OK = {"verify": {"status": ["2xx"]}}

    @pytest.fixture
    def write(self, tmp_path):
        (tmp_path / "common.json").write_text(json.dumps({"ok": self.OK, "stage": _stage(response={"checks": self.OK})}))
        return lambda stages: _write(tmp_path, stages)

    @pytest.mark.parametrize(
        ("stages", "where"),
        [
            pytest.param([_stage(response=[{"$merge": "common.json#/ok", "verify": {"status": [404]}}])], "verify.status", id="list-form-response"),
            pytest.param([_stage(response={"checks": {"$merge": "common.json#/ok", "verify": {"status": [404]}}})], "verify.status", id="mapping-form-response"),
            pytest.param([_stage(response={"checks": [{"$merge": "common.json#/ok", "verify": {"status": [404]}}]})], "verify.status", id="mapping-form-response-list"),
            pytest.param([_stage(response=[{"verify": {"$merge": "common.json#/ok/verify", "status": [404]}}])], "status", id="merge-into-verify"),
            pytest.param([{"$merge": "common.json#/stage", "response": {"checks": {"verify": {"status": [404]}}}}], "response.checks.verify.status", id="merge-into-stage"),
            pytest.param({"s": _stage(response=[{"$merge": "common.json#/ok", "verify": {"status": [404]}}])}, "verify.status", id="mapping-form-stages"),
            pytest.param([_stage(response=[{"$merge": "common.json#/ok", "verify": {"status": 404}}])], "verify.status", id="scalar-sibling"),
        ],
    )
    def test_different_status_is_a_merge_conflict(self, write, stages, where):
        result = validate_scenario(write(stages))
        assert [(d.code, d.message) for d in result.diagnostics] == [(C.REF_ERROR, f"JSON reference resolution error: Merge conflict at {where}")]

    @pytest.mark.parametrize(
        ("step", "expected"),
        [
            pytest.param({"$merge": "common.json#/ok", "verify": {"status": ["2xx"]}}, {"status": ["2xx"]}, id="equal-list-keeps"),
            pytest.param({"$merge": "common.json#/ok", "verify": {"expressions": ["{{ true }}"]}}, {"status": ["2xx"], "expressions": ["{{ true }}"]}, id="other-keys-still-merge"),
            pytest.param({"verify": {"status": {"$include": "common.json#/ok/verify/status"}}}, {"status": ["2xx"]}, id="include-inside-status-resolves"),
        ],
    )
    def test_merges_that_do_not_widen(self, write, step, expected):
        scenario, raw = load_scenario(write([_stage(response=[step])]))
        assert raw["stages"][0]["response"][0]["verify"] == expected
        assert scenario.stages[0].response[0].verify.status == ["2xx"]


class TestJmespathExpectationMerge:
    """What one verify.jmespath expression must be is one value: a sibling's
    array concatenated onto a fragment's asserted an array neither wrote, and
    two objects under eq blended into a third. An expectation merges as a
    scalar does, equal keeps and different is a merge conflict; different
    expressions still merge key by key."""

    CHECKS = {"verify": {"jmespath": {"tags": ["a"], "meta": {"eq": {"page": 1}}, "price": {"gt": 0}}}}

    @pytest.fixture
    def write(self, tmp_path):
        (tmp_path / "common.json").write_text(json.dumps({"checks": self.CHECKS}))
        return lambda step: _write(tmp_path, [_stage(response=[step])])

    @pytest.mark.parametrize(
        ("jmespath", "where"),
        [
            pytest.param({"tags": ["b"]}, "tags", id="array-not-concatenated"),
            pytest.param({"meta": {"eq": {"size": 2}}}, "meta", id="eq-object-not-blended"),
            # A matcher is one expectation too: keys of two are not combined.
            pytest.param({"price": {"lt": 100}}, "price", id="matcher-keys-not-combined"),
            pytest.param({"price": 5}, "price", id="value-beside-a-matcher"),
        ],
    )
    def test_different_expectation_is_a_merge_conflict(self, write, jmespath, where):
        result = validate_scenario(write({"$merge": "common.json#/checks", "verify": {"jmespath": jmespath}}))
        assert [(d.code, d.message) for d in result.diagnostics] == [(C.REF_ERROR, f"JSON reference resolution error: Merge conflict at verify.jmespath.{where}")]

    @pytest.mark.parametrize(
        ("jmespath", "where"),
        [
            # JSON equality at any depth: Python's [True] == [1] kept the
            # fragment's and dropped the sibling's without a conflict.
            pytest.param({"flags": [1]}, "flags", id="bool-vs-int-in-array"),
            pytest.param({"meta": {"eq": {"active": 1}}}, "meta", id="bool-vs-int-in-object"),
        ],
    )
    def test_true_is_not_one_when_merging(self, tmp_path, jmespath, where):
        (tmp_path / "flags.json").write_text(json.dumps({"verify": {"jmespath": {"flags": [True], "meta": {"eq": {"active": True}}}}}))
        result = validate_scenario(_write(tmp_path, [_stage(response=[{"$merge": "flags.json", "verify": {"jmespath": jmespath}}])]))
        assert [(d.code, d.message) for d in result.diagnostics] == [(C.REF_ERROR, f"JSON reference resolution error: Merge conflict at verify.jmespath.{where}")]

    @pytest.mark.parametrize(
        ("expectation", "merged"),
        [
            pytest.param({"$merge": "common.json#/checks/verify/jmespath/price", "lt": 100}, JMESPathMatcher(gt=0, lt=100), id="matcher"),
            pytest.param({"eq": {"$merge": "common.json#/checks/verify/jmespath/meta/eq", "size": 2}}, JMESPathMatcher(eq={"page": 1, "size": 2}), id="inside-eq"),
        ],
    )
    def test_merge_written_at_an_expectation_composes_it(self, write, expectation, merged):
        """A ``$merge`` written at the expectation itself is one value composed
        on purpose, not two written for it: its siblings merge key by key, as
        they do one level down, inside eq."""
        scenario, _ = load_scenario(write({"verify": {"jmespath": {"price": expectation}}}))
        assert scenario.stages[0].response[0].verify.jmespath["price"] == merged

    @pytest.mark.parametrize(
        ("fragment", "sibling"),
        [
            pytest.param({"gt": 0}, {"gt": 1}, id="number"),
            # Each operand is one value, as the expectation is: the arrays were
            # concatenated into eq [1, 2] and the objects blended into
            # {"page": 1, "size": 5}, which neither side wrote, without a conflict.
            pytest.param({"eq": [1]}, {"eq": [2]}, id="eq-array-not-concatenated"),
            pytest.param({"eq": {"page": 1}}, {"eq": {"size": 5}}, id="eq-object-not-blended"),
            pytest.param({"ne": [1]}, {"ne": [2]}, id="ne-array-not-concatenated"),
            pytest.param({"contains": ["a"]}, {"contains": ["b"]}, id="contains-element-not-concatenated"),
            pytest.param({"not_contains": {"a": 1}}, {"not_contains": {"b": 1}}, id="not-contains-element-not-blended"),
            pytest.param({"eq": [True]}, {"eq": [1]}, id="true-is-not-one"),
        ],
    )
    def test_an_operand_both_sides_give_must_agree_whole(self, tmp_path, fragment, sibling):
        """A ``$merge`` at the expectation composes the matcher key by key, but
        a key both sides give is one operand: equal keeps, different conflicts,
        an array or object as much as a number."""
        (tmp_path / "matcher.json").write_text(json.dumps(fragment))
        key = next(iter(sibling))
        result = validate_scenario(_write(tmp_path, [_stage(response=[{"verify": {"jmespath": {"x": {"$merge": "matcher.json", **sibling}}}}])]))
        assert [(d.code, d.message) for d in result.diagnostics] == [(C.REF_ERROR, f"JSON reference resolution error: Merge conflict at {key}")]

    @pytest.mark.parametrize(
        ("operand", "equal"),
        [
            pytest.param({"eq": {"page": 1, "tags": ["a"]}}, {"eq": {"page": 1.0, "tags": ["a"]}}, id="equal-object"),
            pytest.param({"contains": ["a"]}, {"contains": ["a"]}, id="equal-array"),
        ],
    )
    def test_an_equal_operand_keeps(self, tmp_path, operand, equal):
        (tmp_path / "matcher.json").write_text(json.dumps(operand))
        scenario, _ = load_scenario(_write(tmp_path, [_stage(response=[{"verify": {"jmespath": {"x": {"$merge": "matcher.json", "lt": 100, **equal}}}}])]))
        assert scenario.stages[0].response[0].verify.jmespath["x"] == JMESPathMatcher(lt=100, **operand)

    def test_equal_expectations_keep_and_other_expressions_merge(self, write):
        scenario, raw = load_scenario(write({"$merge": "common.json#/checks", "verify": {"jmespath": {"tags": ["a"], "count": 3}}}))
        assert raw["stages"][0]["response"][0]["verify"]["jmespath"] == {**self.CHECKS["verify"]["jmespath"], "count": 3}
        assert scenario.stages[0].response[0].verify.jmespath["tags"] == ["a"]

    def test_include_inside_an_expectation_resolves(self, write):
        scenario, _ = load_scenario(write({"verify": {"jmespath": {"meta": {"$include": "common.json#/checks/verify/jmespath/meta"}}}}))
        assert scenario.stages[0].response[0].verify.jmespath["meta"].eq == {"page": 1}


@pytest.mark.parametrize(
    ("stages", "top", "expected"),
    [
        pytest.param(
            [
                _stage(
                    response=[
                        {"save": {"substitutions": [{"vars": {"req_id": "{{ response.headers['x-request-id'] }}"}}]}},
                        {"verify": {"expressions": ["{{ response.status == 200 }}"]}},
                    ]
                )
            ],
            {},
            [],
            id="response-namespace-in-response-steps",
        ),
        pytest.param([_stage(request={"url": "http://server/x", "headers": {"x": "{{ response.status }}"}})], {}, [C.UNDEFINED_VAR], id="response-namespace-in-request"),
        pytest.param([_stage(substitutions=[{"vars": {"response": "mine"}}])], {}, [C.RESERVED_NAME], id="user-name-shadowed-by-response-namespace"),
        pytest.param(
            [_stage(response=[{"verify": {"headers": {"x-h": {"contains": "a", "not_contains": "a"}}}}])], {}, [C.CONTAINS_CONTRADICTION], id="header-matcher-contradiction"
        ),
        # Unset not_matches used to be conflated with the empty pattern.
        pytest.param([_stage(response=[{"verify": {"headers": {"x-h": {"matches": ""}}}}])], {}, [], id="empty-matches-is-no-contradiction"),
        # `name` is optional; two stages omitting it are not duplicates.
        pytest.param([_stage(name=""), _stage(name="")], {}, [], id="unnamed-stages-not-duplicates"),
        # Only '::' is pytest's node-id separator.
        pytest.param([_stage(name="Users: list")], {}, [], id="single-colon-in-stage-name"),
        # A scenario-level group is inherited by every stage, so the chain stays together.
        pytest.param([_stage()], {"marks": ["xdist_group('db')"]}, [], id="scenario-level-xdist-group"),
        # xdist keeps one copy of each group name, so a stage repeating the
        # scenario's group is in the same group as its siblings.
        pytest.param([_stage(marks=["xdist_group(name='db')"])], {"marks": ["xdist_group('db')"]}, [], id="stage-repeats-scenario-xdist-group"),
        # ... but a second name of its own is a group apart.
        pytest.param([_stage(marks=["xdist_group('other')"])], {"marks": ["xdist_group('db')"]}, [C.STAGE_XDIST_GROUP], id="stage-adds-xdist-group"),
        # xdist reads the group back from after the node id's last '@'.
        pytest.param([_stage()], {"marks": ["xdist_group('db[1]@main')"]}, [], id="bracket-before-at-in-xdist-group"),
        # Loads fine, and the name at the bottom is still reported. The checks
        # walked it recursively and crashed with a RecursionError instead.
        pytest.param([_stage(substitutions=[{"vars": {"deep": nested("{{ nowhere }}", LOADABLE_BUT_DEEP)}}])], {}, [C.UNDEFINED_VAR], id="value-nested-hundreds-deep"),
        # Only values are substituted: a templated key reaches the wire verbatim.
        pytest.param(
            [_stage(request={"url": "http://server/x", "headers": {"{{ hname }}": "v"}})],
            {"substitutions": [{"vars": {"hname": "X-Trace"}}]},
            [C.TEMPLATE_IN_KEY],
            id="templated-dict-key",
        ),
        # An absolute-URI $ref is JSON Schema vocabulary the registry resolves.
        pytest.param(
            [
                _stage(
                    response=[
                        {
                            "verify": {
                                "body": {
                                    "schema": {
                                        "$id": "https://example.com/s",
                                        "type": "object",
                                        "properties": {"a": {"$ref": "https://example.com/s#/$defs/a"}},
                                        "$defs": {"a": {"type": "string"}},
                                    }
                                }
                            }
                        }
                    ]
                )
            ],
            {},
            [],
            id="absolute-uri-ref-in-inline-schema",
        ),
    ],
)
def test_inline_scenario_diagnostics(tmp_path, stages, top, expected):
    assert _codes(validate_scenario(_write(tmp_path, stages, **top))) == expected


class TestAmbiguousRef:
    """HTTPCHAIN026: a $ref matching files under both lookup bases (scenario
    dir and root path) is a warning diagnostic, not a bare Python warning."""

    @pytest.fixture
    def suite(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")
        (tmp_path / "fragment.json").write_text(json.dumps({"url": "http://server/root"}))
        suite = tmp_path / "suite"
        suite.mkdir()
        (suite / "fragment.json").write_text(json.dumps({"url": "http://server/local"}))
        return suite

    def test_reported_as_warning(self, suite):
        result = validate_scenario(_write(suite, [_stage(request={"$ref": "fragment.json"})]))
        assert _codes(result) == [C.AMBIGUOUS_REF]
        assert "file-relative wins" in result.warnings[0]

    def test_survives_a_later_load_failure(self, suite):
        result = validate_scenario(_write(suite, [_stage(request={"$ref": "fragment.json"}, response=[{"verify": {"$ref": "missing.json"}}])]))
        assert sorted(_codes(result)) == [C.REF_ERROR, C.AMBIGUOUS_REF]


class TestFileContent:
    """Scenario files and the files they pull in are UTF-8, with or without the
    byte-order mark editors on Windows write. Content the reader cannot parse
    is invalid JSON naming the file, not HTTPCHAIN015's catch-all "Failed to
    parse"."""

    @pytest.mark.parametrize("include", [False, True], ids=["scenario", "included-file"])
    @pytest.mark.parametrize(
        ("content", "message"),
        [
            pytest.param('{"name": "café"}'.encode("latin-1"), "is not valid UTF-8", id="not-utf8"),
            # Well-formed JSON past CPython's default int-string conversion limit (4300 digits).
            pytest.param(b'{"timeout": ' + b"1" * 5000 + b"}", "cannot be parsed: Exceeds the limit", id="int-too-long"),
        ],
    )
    def test_unreadable_file_is_invalid_json(self, tmp_path, include, content, message):
        # Reading fails before model validation, so the content need not be a scenario.
        bad = tmp_path / ("stage.json" if include else "test_x.http.json")
        bad.write_bytes(content)
        scenario = _write(tmp_path, [{"$include": "stage.json"}]) if include else bad

        result = validate_scenario(scenario)

        assert _codes(result) == [C.INVALID_JSON]
        assert f"{bad.name} {message}" in result.errors[0]

    @pytest.mark.skipif(sys.platform == "win32", reason="Windows' non-strict realpath passes such a path through, so it is reported as not found")
    def test_reference_path_the_os_rejects_is_a_ref_error(self, tmp_path):
        """A path the OS path call rejects (a lone surrogate) fell through to
        HTTPCHAIN015, not naming the reference."""
        result = validate_scenario(_write(tmp_path, [{"$include": "\ud800.json"}]))
        assert _codes(result) == [C.REF_ERROR]
        assert r"Reference path '\ud800.json' is not a valid file path" in result.errors[0]

    def test_byte_order_mark_is_accepted(self, tmp_path):
        scenario = _write(tmp_path, [_stage()])
        scenario.write_text("\ufeff" + scenario.read_text(), encoding="utf-8")
        assert validate_scenario(scenario).diagnostics == []

    def test_schema_file_with_byte_order_mark_passes_deep_checks(self, tmp_path):
        (tmp_path / "schema.json").write_text("\ufeff" + json.dumps({"type": "object"}), encoding="utf-8")
        scenario = _write(tmp_path, [_stage(response=[{"verify": {"body": {"schema": "schema.json"}}}])])
        assert validate_scenario(scenario, deep=True).diagnostics == []


class TestRootPathDefault:
    """The CLI's default $ref root (resolve_root_path) approximates pytest's
    rootpath: nearest ancestor with a project marker, else the file's parent."""

    @staticmethod
    def _scenario_in(directory):
        directory.mkdir(parents=True, exist_ok=True)
        scenario = directory / "test_x.http.json"
        scenario.write_text("{}")
        return scenario

    def test_finds_project_marker(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")
        assert resolve_root_path(self._scenario_in(tmp_path / "suites" / "api")) == tmp_path

    def test_falls_back_to_file_parent(self, tmp_path, monkeypatch):
        _hide_ancestor_project_markers(monkeypatch)
        assert resolve_root_path(self._scenario_in(tmp_path / "a" / "b")) == tmp_path / "a" / "b"

    def test_markerless_tree_falls_back_to_tests_ancestor(self, tmp_path, monkeypatch):
        """Without any project marker, the pre-marker default (nearest tests/
        ancestor) still applies, so exported bundles keep their sandbox."""
        _hide_ancestor_project_markers(monkeypatch)
        assert resolve_root_path(self._scenario_in(tmp_path / "bundle" / "tests" / "api")) == tmp_path / "bundle" / "tests"

    def test_bare_marker_used_when_no_pytest_config_anywhere(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "solo"\n')
        assert resolve_root_path(self._scenario_in(tmp_path)) == tmp_path

    def test_ref_above_scenario_dir_resolves_within_project_root(self, tmp_path):
        """A $ref reaching above the scenario's own tree but inside the project
        resolves by default — matching what pytest collection accepts."""
        (tmp_path / ".git").mkdir()
        (tmp_path / "shared").mkdir()
        (tmp_path / "shared" / "common.json").write_text(json.dumps({"url": "http://server/x", "method": "GET"}))
        suite = tmp_path / "tests" / "api"
        suite.mkdir(parents=True)

        result = validate_scenario(_write(suite, [_stage(request={"$ref": "../../shared/common.json"})]))

        assert result.diagnostics == []

    @pytest.mark.parametrize(
        ("filename", "content"),
        [
            ("pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'),
            # pytest.ini counts unconditionally — it exists only for pytest, so
            # it needs no section to prove intent.
            ("pytest.ini", "[pytest]\n"),
            ("pytest.ini", ""),
            ("tox.ini", "[pytest]\ntestpaths = tests\n"),
            ("setup.cfg", "[tool:pytest]\ntestpaths = tests\n"),
        ],
        ids=["pyproject.toml", "pytest.ini", "pytest.ini-empty", "tox.ini", "setup.cfg"],
    )
    def test_pytest_config_beats_a_nearer_bare_marker(self, tmp_path, filename, content):
        """A sub-package's plain pyproject.toml is not a pytest rootdir.
        Preferring it would shrink the CLI's root below pytest's, so `validate`
        would reject $ref targets that collection resolves fine — for every
        file pytest accepts as an inifile, not just pyproject.toml."""
        (tmp_path / filename).write_text(content)
        package = tmp_path / "packages" / "api"
        package.mkdir(parents=True)
        (package / "pyproject.toml").write_text('[project]\nname = "api"\n')

        assert resolve_root_path(self._scenario_in(package)) == tmp_path

    @pytest.mark.parametrize(
        ("filename", "content"),
        [
            ("tox.ini", "[tox]\nenvlist = py313\n"),
            ("setup.cfg", "[metadata]\nname = api\n"),
            ("tox.ini", "this is not ini syntax at all\x00"),
            ("pyproject.toml", "this is not [ valid toml"),
        ],
        ids=["tox-without-pytest-section", "setup.cfg-without-pytest-section", "unparseable-ini", "unparseable-toml"],
    )
    def test_shared_inifile_without_a_pytest_section_is_not_a_rootdir(self, tmp_path, filename, content):
        """pyproject.toml, tox.ini and setup.cfg belong to other tools too.
        Treating one as a pytest rootdir on sight — or crashing on a file the
        parser cannot read — would move the root for projects that never
        configured pytest there, so all of it degrades to the bare-marker
        fallback."""
        (tmp_path / filename).write_text(content)
        package = tmp_path / "packages" / "api"
        package.mkdir(parents=True)
        (package / "pyproject.toml").write_text('[project]\nname = "api"\n')

        assert resolve_root_path(self._scenario_in(package)) == package


class TestDiagnosticRegistry:
    """README promises stable HTTPCHAINxxx codes: the docs page, SEVERITY and
    DiagnosticCode must not drift apart in any direction."""

    def test_every_code_declares_a_severity(self):
        """`diag()` looks the severity up by code, so a newly appended code must
        fail here rather than at its first call site."""
        assert set(SEVERITY) == set(DiagnosticCode)

    def test_docs_table_matches_registry(self):
        documented = dict(re.findall(r"^\| `(HTTPCHAIN\d{3})` \| (\w+) \|", DOCS_PAGE.read_text(), re.M))
        assert documented == {str(code): SEVERITY[code] for code in DiagnosticCode}

    def test_docs_mention_only_known_codes(self):
        assert set(re.findall(r"HTTPCHAIN\d{3}", DOCS_PAGE.read_text())) <= {str(code) for code in DiagnosticCode}

    def test_deep_findings_are_warnings(self):
        """The docs page's promise: deep validation never fails a scenario."""
        deep = (C.REFERENCED_FILE_NOT_FOUND, C.SCHEMA_FILE_INVALID, C.IMPORT_FAILED, C.UNKNOWN_ARG, C.MISSING_ARG)
        assert {SEVERITY[code] for code in deep} == {"warning"}
