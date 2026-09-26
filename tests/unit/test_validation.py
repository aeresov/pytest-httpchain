"""The shared scenario validator (pytest_httpchain.validation), file level.

Each file under ``test_validation/`` is a minimal scenario built to trigger
exactly one finding (or none), so the two tables below pin the complete
diagnostic list — code, location and message — rather than the presence of
one code: a check that starts firing on a neighbour's fixture fails here too.
"""

import json
import re
from pathlib import Path

import pytest

import pytest_httpchain.validation.loader as validation_loader
from pytest_httpchain.validation import SEVERITY, DiagnosticCode, load_scenario, resolve_root_path, validate_scenario

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
        # $ref/$defs inside verify.body.schema are JSON Schema vocabulary, in
        # every response-step shape and through a spliced-in stage fragment.
        "inline_schema_standard_ref.json",
        "inline_schema_dict_form.json",
        "inline_schema_mapping_form_response.json",
        "inline_schema_fragment_stage.json",
        # An '$include' PROPERTY maps to a schema object, never a string.
        "inline_schema_property_named_include.json",
        "deep_import_ok.json",
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
    ("schema_error.json", [(C.SCHEMA, "stages -> 0 -> request", "Field required")]),
    # Models forbid extra keys: a typo fails naming the key and its location.
    ("request_field_typo.json", [(C.SCHEMA, "stages -> 0 -> request -> headerz", "Extra inputs are not permitted")]),
    ("toplevel_vars_unknown_key.json", [(C.SCHEMA, "vars", "Extra inputs are not permitted")]),
    ("duplicate_stage_names.json", [(C.DUPLICATE_STAGE, "stages", r"\['dup'\]")]),
    ("fixture_var_conflict.json", [(C.FIXTURE_CONFLICT, None, r"\['token'\]")]),
    # M6: a malformed marker crashes collection, so the pre-flight gate errors too.
    ("invalid_scenario_marker.json", [(C.INVALID_MARKER, "marks", r"'skip\('")]),
    ("invalid_stage_marker.json", [(C.INVALID_MARKER, "stages[0].marks", "'foo.bar'")]),
    ("contradiction_contains.json", [(C.CONTAINS_CONTRADICTION, "stages[0].response[0].verify.body", r"substring\(s\): \['ERROR'\]")]),
    ("contradiction_matches.json", [(C.MATCHES_CONTRADICTION, "stages[0].response[0].verify.body", r"pattern\(s\): \['\^OK\$'\]")]),
    # The scenario-level context never includes fixture values: a guaranteed
    # crash at scenario initialization.
    (
        "scenario_template_fixture_ref.json",
        [(C.FIXTURE_IN_SCENARIO_TEMPLATE, "substitutions", r"\['srv'\]"), (C.FIXTURE_IN_SCENARIO_TEMPLATE, "ssl", r"\['ca_path'\]")],
    ),
    ("undefined_variables.json", [(C.UNDEFINED_VAR, "stages[0].request", r"\['undefined_var'\]")]),
    # response/status_code/body only reach save/verify handlers, never templates.
    ("ambient_response.json", [(C.UNDEFINED_VAR, "stages[0].response", r"\['status_code'\]")]),
    ("no_response_validation.json", [(C.NO_VERIFY, "stages[0]", "no response validation")]),
    # A no-op verify step still counts as a verify step: NO_VERIFY stays quiet.
    ("noop_verify.json", [(C.NOOP_VERIFY, "stages[0].response[0].verify", "asserts nothing")]),
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


def test_wrong_extension_warns(datadir):
    result = validate_scenario(datadir / "wrong_extension.txt")
    assert _codes(result) == [C.WRONG_EXTENSION]
    assert result.valid is True


def test_directory_is_not_a_file(tmp_path):
    assert _codes(validate_scenario(tmp_path)) == [C.NOT_A_FILE]


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
