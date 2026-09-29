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

import pytest_httpchain.validation.discovery as discovery
import pytest_httpchain.validation.validate as validation_validate
from pytest_httpchain.body_schema import BodySchema
from pytest_httpchain.models import JMESPathMatcher
from pytest_httpchain.validation import SEVERITY, DiagnosticCode, DiscoveryError, load_scenario, resolve_root_path, validate_paths, validate_scenario
from tests.unit.helpers import LOADABLE_BUT_DEEP, TOO_DEEP_TO_PARSE, TOO_DEEP_TO_WALK, nested, on_bounded_stack

C = DiagnosticCode
# A stable importable directory so `userfuncs:<name>` refs resolve under --syspath.
USERFUNCS_DIR = Path(__file__).parent / "test_validation_userfuncs"
DOCS_PAGE = Path(__file__).resolve().parents[2] / "docs" / "diagnostics.md"


def _validate(datadir, fixture):
    """Through `validate_paths`, as `validate` validates, so a fixture may be a
    directory. Fixtures named ``deep_*`` exercise deep validation (imports,
    signatures, files)."""
    deep = fixture.startswith("deep_")
    [(_, result)] = validate_paths([datadir / fixture], suffix="http", deep=deep, syspaths=[USERFUNCS_DIR] if deep else None)
    return result


def _codes(result):
    return [d.code for d in result.diagnostics]


def _stage(**fields):
    return {"name": "s", "request": {"url": "http://server/x"}, "response": [{"verify": {"status": 200}}], **fields}


def _write(directory, stages, **top):
    path = directory / "test_x.http.json"
    path.write_text(json.dumps({"stages": stages, **top}))
    return path


def _skip_if_ancestors_set_a_rootdir(tmp_path):
    """For a test of the rootdir pytest falls back to without a configuration
    file: skip it when a directory above tmp_path holds one, or a setup.py."""
    for directory in tmp_path.parents:
        if any((directory / name).is_file() for name in ("pytest.toml", ".pytest.toml", "pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg", "setup.py")):
            pytest.skip(f"{directory} holds a configuration file or a setup.py")


@pytest.mark.parametrize(
    "fixture",
    [
        "valid_scenario.json",
        "valid_markers.json",
        "verify_template_expression_ok.json",
        # jmespath alone asserts something (no HTTPCHAIN006), and its values
        # see the response namespace and a prior step's save.
        "verify_jmespath_ok.json",
        # Comments and trailing commas, in a .jsonc file (no HTTPCHAIN013) and
        # the file it includes, and in a .json file whose body schema file the
        # deep checks read as the runtime does.
        "jsonc_ok.jsonc",
        "deep_jsonc_schema_ok.json",
        "schema_key_tolerated.json",  # the editor-integration "$schema" key is stripped by the loader
        # Names a template may reference without being flagged undefined:
        "parametrize_individual.json",
        "parametrize_combinations.json",
        "parallel_foreach.json",
        "functions_substitution.json",  # a `functions` alias is callable
        "scenario_fixtures_available.json",
        "comprehension_vars.json",  # a comprehension target is a local binding
        # A `vars` object, a saved one and `response` read by key, `in`, len(),
        # iteration and their methods: the name is the reference, never a key
        # or a method (`trace['X-Request-Id']`, `trace.get(...)`, `trace.items`).
        "vars_mapping_access_ok.json",
        "always_run_refs_ok.json",  # fixtures, scenario substitutions, earlier saves
        # skip_if sees the stage's own substitutions and parametrize
        # parameters too. A name a stage that may skip saves is always there
        # where a stage without skip_if (or with skip_if: false) saves it as
        # well, and get() and exists() read one that may not be.
        "skip_if_refs_ok.json",
        # foreach values resolve at execution against the full local context,
        # unlike parametrize values, so a stage substitution is in scope.
        "foreach_value_stage_scope.json",
        # collect_saves resolves with the rest of the parallel config (a stage
        # substitution is in scope), and a collecting stage saves its names as
        # any stage does: a later stage's foreach iterates over them.
        "parallel_collect_saves_ok.json",
        # retry resolves once per stage, against what skip_if sees: the
        # stage's substitutions and parametrize parameters, the scenario's
        # and an earlier stage's saves.
        "retry_refs_ok.json",
        # thresholds resolve with the rest of the parallel config (a stage
        # substitution, a parametrize parameter, an earlier save and a
        # scenario variable are in scope), and stats_as is a save of its
        # stage that the stages after it read, by attribute or by key.
        "parallel_stats_refs_ok.json",
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
        # A regex save's names are known, to later steps and later stages, and
        # its pattern sees a prior step's save.
        "save_regex_ok.json",
        # The built-in helpers are engine-provided in every phase, scenario
        # level included; calling one whose name a stage saves (timestamp())
        # reaches the built-in, and reading it after the save reads the save.
        "template_helpers_ok.json",
        "substitution_function_kwargs_literal_ok.json",  # only a template is dead text
        # An `=` that assigns nothing (a keyword argument, a comparison, one in
        # a string), a `;` in a string or ending the one statement, and a dict
        # literal spaced from the closing braces: each template is one expression.
        "template_equals_signs_ok.json",
        # client.base_url (templated from scenario substitutions) completes a
        # relative URL, literal or after a template; an absolute one ignores it.
        "relative_url_with_base_url.json",
        # An escaped `\{{` is text wherever it is rendered, and so is what
        # follows it up to its `}}`, template syntax included (a Handlebars raw
        # block, a Jinja `{{ '{{' }}`, a nested `{{ }}`): no name it holds is
        # read, and no expression in it is parsed, in a value, a parametrize
        # value (no collection-time resolution), a pattern or a file path. A doubled backslash is one before a real
        # template, which is read: the `vars` step reading `greeting` comes
        # after the one defining it. A literal is judged as it renders: a
        # GraphQL or JMESPath string holding one, whose own grammar has no
        # `\{` escape, is valid.
        "escaped_braces_ok.json",
        # $ref/$defs inside verify.body.schema are JSON Schema vocabulary, in
        # every response-step shape and through a spliced-in stage fragment.
        "inline_schema_standard_ref.json",
        "inline_schema_dict_form.json",
        "inline_schema_mapping_form_response.json",
        "inline_schema_fragment_stage.json",
        # An '$include' PROPERTY maps to a schema object, never a string.
        "inline_schema_property_named_include.json",
        # A $ref to a file is JSON Schema's own, resolved at runtime (HTTPCHAIN028
        # flagged it when it could not be).
        "inline_schema_file_ref.json",
        "deep_import_ok.json",
        # The built-in schemes import nothing, and their templates are in
        # scope: scenario substitutions at scenario level, an earlier save in
        # a request's.
        "deep_auth_builtins.json",
        "deep_binary_exists.json",
        # Every form of multipart file whose path is there, content given
        # inline (no file to look for), and a path a template completes.
        "deep_multipart_files_exist.json",
        "deep_templated_func.json",  # a templated reference cannot be resolved statically
        "deep_sig_var_keyword.json",  # **kwargs makes every supplied name fillable
        # Substitution functions are called from templates with arguments the
        # validator cannot see: importing is the whole check.
        "deep_substitution_function_required_arg.json",
        # Some C callables have no retrievable signature; that is not a bad call.
        "deep_non_introspectable_func.json",
        # A pointer into an OpenAPI document, whose schema references another
        # schema of the document and one in a file beside it; an inline
        # schema's reference to that file, relative to the scenario.
        "deep_schema_pointer_ok.json",
        "deep_inline_schema_ref_ok.json",
        # A reference a template completes is known only once rendered.
        "deep_inline_schema_ref_template.json",
        # A pointer into a Draft 7 document, from a 2020-12 schema: the target
        # is checked in its document's dialect, as the runtime validates it.
        "deep_inline_schema_ref_other_dialect.json",
        # An `$id` is the base of the references inside it, so a root-relative
        # `$ref` under an `https:` one names an embedded resource, not a file:
        # at an inline schema's root, and in an OpenAPI component, where JSON
        # Schema itself does not look for one.
        "deep_inline_schema_bundled_id.json",
        "deep_schema_component_id.json",
        # A pointer past a component into it: the component's `$id` is on its
        # way, so it is the base there too.
        "deep_schema_component_id_pointer_into.json",
        # Draft 3 to 7 ignore a `$ref`'s siblings, at runtime, so a missing
        # file referenced beside one is never looked for.
        "deep_inline_schema_ref_siblings_draft7.json",
        # ssl.verify's path a template completes is the one the runtime
        # renders and opens, known only then.
        "deep_ssl_verify_templated.json",
    ],
)
def test_clean_fixture_has_no_diagnostics(datadir, fixture):
    result = _validate(datadir, fixture)
    assert result.diagnostics == []
    assert result.valid is True


DIAGNOSED = [
    ("does_not_exist.json", [(C.FILE_NOT_FOUND, None, "File not found")]),
    # A directory holding no file pytest would collect: its one scenario is
    # not named test_<name>.http.json.
    (
        "no_scenario_files",
        [(C.NO_SCENARIO_FILES, None, r"^No scenario files named test_<name>\.http\.json or test_<name>\.http\.jsonc in directory: \S*no_scenario_files$")],
    ),
    ("invalid_json.json", [(C.INVALID_JSON, None, "Invalid JSON syntax")]),
    # A duplicated organizational key is a JSON-content error at load, not
    # a silent last-wins, and not a $ref error.
    ("duplicate_json_key.json", [(C.INVALID_JSON, None, "Duplicate key 'check'")]),
    # Saved as Latin-1. RFC 8259 requires UTF-8, so the file is invalid JSON,
    # like a syntax error, and not an unexplained parse failure.
    ("not_utf8.json", [(C.INVALID_JSON, None, r"^Invalid JSON: .*not_utf8\.json is not valid UTF-8: 'utf-8' codec can't decode byte 0xe9")]),
    # A comment never closed is a syntax error at its opening, and one after
    # comments is at its line and column in the file as written. Only one
    # trailing comma is accepted: the second is where the error is.
    ("jsonc_unterminated_comment.json", [(C.INVALID_JSON, None, r"^Invalid JSON syntax: Unterminated comment: line 6 column 1 \(char \d+\)$")]),
    ("jsonc_double_comma.json", [(C.INVALID_JSON, None, r"^Invalid JSON syntax: Expecting value: line 5 column 113 \(char \d+\)$")]),
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
    # A call under a built-in's name that the scenario defines as a fixture or
    # function substitution, made where no scenario-level template can see
    # that definition, runs the built-in: nothing fails, so it is a warning,
    # not the 016/017 error a fixture or undefined READ there is. In the stage,
    # where the definition is in scope, the call reaches it.
    (
        "scenario_builtin_named_fixture_call.json",
        [
            (
                C.BUILTIN_STANDS_IN,
                "substitutions",
                r"^Scenario-level 'substitutions' uses the function now\(\), but the scenario's own definition of 'now' is not in scope there "
                r"\(a scenario-level substitution step sees only the steps before it: no fixture, and nothing a stage defines\), "
                r"so the template built-in runs instead$",
            )
        ],
    ),
    # A stage faking the clock leaves the built-in to the scenario level.
    (
        "scenario_builtin_named_call_stage_function.json",
        [(C.BUILTIN_STANDS_IN, "substitutions", r"uses the function timestamp\(\), but the scenario's own definition of 'timestamp'")],
    ),
    # A step before the one that defines the function runs the built-in, where
    # a read of a name defined later crashes (017).
    ("scenario_builtin_named_call_later_step.json", [(C.BUILTIN_STANDS_IN, "substitutions", r"uses the function now\(\), but the scenario's own definition of 'now'")]),
    # Where the function substitution IS in scope, no 036 anywhere: a later
    # scenario-level step, client, a parametrize value (resolved against the
    # scenario substitutions) and always_run each call the user's timestamp().
    # Only the parametrize timing note remains.
    (
        "scenario_builtin_named_function_in_scope.json",
        [(C.PARAMETRIZE_COLLECTION_RESOLUTION, "stages[0].parametrize", "resolve at collection time")],
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
    # Rendered, an escape would leave literal braces, which the stage fails
    # as template text left in the client's URL: refused as it loads instead.
    ("escaped_braces_client_url.json", [(C.SCHEMA, "client -> base_url", r"base_url cannot hold a backslash before '\{\{'")]),
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
    # stats_as is a save: the same name checks apply to it.
    ("parallel_stats_as_fixture_shadow.json", [(C.FIXTURE_SHADOWS_SAVE, None, r"\['load_stats'\]")]),
    ("parallel_stats_as_reserved_name.json", [(C.RESERVED_NAME, None, r"^Name\(s\) \['response'\] are shadowed by the reserved response metadata namespace")]),
    # Saved later is an ordering bug, reported as such rather than as undefined.
    ("forward_reference.json", [(C.FORWARD_REF, "stages[0].request", "'token' is referenced before it is saved")]),
    # A save named like a built-in helper: reading it ahead of the save gets
    # the built-in function, which is no value, so it is still an ordering
    # bug, and the stage fails. The call (timestamp()) beside it reaches the
    # built-in and is not.
    (
        "builtin_name_forward_ref.json",
        [
            (
                C.FORWARD_REF,
                "stages[0].request",
                r"'timestamp' is referenced before it is saved \(saved in stage 'clock'\); where no definition of 'timestamp' is in scope, "
                r"the name is the template built-in function, not a value, and a template that renders to it fails the stage$",
            )
        ],
    ),
    # A fixture may hold a function, which a call reaches first: outside the
    # fixture's stage the call silently runs the built-in timestamp() instead.
    (
        "builtin_named_fixture_call_out_of_scope.json",
        [
            (
                C.BUILTIN_STANDS_IN,
                "stages[0].request",
                r"^Stage 'early': request uses the function timestamp\(\), but the scenario's own definition of 'timestamp' is not in scope there, "
                r"so the template built-in runs instead$",
            )
        ],
    ),
    # A helper without its parentheses gets the function, not its value; one
    # handed to a key= stays a function, as it should.
    (
        "uncalled_helper.json",
        [
            (
                C.UNCALLED_BUILTIN,
                "stages[0].request",
                r"^Stage 'search': request uses the built-in functions \['now', 'timestamp_ms'\] without calling them: .* Write now\(\), timestamp_ms\(\)$",
            )
        ],
    ),
    # A template that holds no single expression fails wherever it renders,
    # with the reason given here, and that is its one finding: read as a
    # regex's identifiers, `True`, `and` and attribute names were reported
    # undefined besides (HTTPCHAIN003, at scenario level the error 017).
    (
        "template_assignment.json",
        [
            (
                C.INVALID_EXPRESSION,
                "stages[0].response",
                r"^Stage 'profile': response has an invalid expression '\{\{ active = True \}\}', and rendering it fails the stage: "
                r"a template holds one expression, not an assignment; to compare two values, write '=='$",
            )
        ],
    ),
    (
        "template_syntax_error.json",
        [
            (
                C.INVALID_EXPRESSION,
                "stages[0].response",
                r"^Stage 'health': response has an invalid expression '\{\{ response\.status == 200 and True\) \}\}', and rendering it fails the stage: unmatched '\)'$",
            )
        ],
    ),
    # At scenario level it fails initialization, and every stage with it: an
    # error, as the 017 it used to get was, so `validate` exits 1 on it.
    (
        "template_multiple_statements.json",
        [
            (
                C.SCENARIO_INVALID_EXPRESSION,
                "substitutions",
                r"^Scenario-level 'substitutions' has an invalid expression '\{\{ env\('TOKEN', 'dev'\); 'fallback' \}\}', and rendering it crashes "
                r"scenario initialization: a template holds one expression, not 2 statements separated by ';'$",
            )
        ],
    ),
    # The dict literal's `}` ran into the template's `}}`, which ended the
    # template a brace early.
    (
        "template_dict_literal_truncated.json",
        [
            (
                C.INVALID_EXPRESSION,
                "stages[0].request",
                r"^Stage 'search': request has an invalid expression '\{\{ \{'status': status \}\}', and rendering it fails the stage: '\{' was never closed$",
            )
        ],
    ),
    # The engine evaluates no lambda. simpleeval refused it only once
    # evaluation reached it, so `validate` passed it, and its parameter was
    # read as a local binding.
    (
        "template_lambda.json",
        [
            (
                C.INVALID_EXPRESSION,
                "stages[0].request",
                r"^Stage 'sorted': request has an invalid expression '\{\{ sorted\(ids, key=lambda i: -i\) \}\}', and rendering it fails the stage: "
                r"the template engine does not evaluate a lambda$",
            )
        ],
    ),
    # A lone surrogate, a JSON \u escape: written escaped, as the file writes
    # it, where the parser's UnicodeEncodeError crashed validation.
    (
        "template_lone_surrogate.json",
        [
            (
                C.INVALID_EXPRESSION,
                "stages[0].request",
                r"^Stage 'odd': request has an invalid expression '\{\{ 'a\\ud800' \}\}', and rendering it fails the stage: "
                r"the expression holds '\\ud800', which is not valid text \(surrogates not allowed\)$",
            )
        ],
    ),
    ("same_stage_forward.json", [(C.FORWARD_REF, "stages[0].request", "'sid', which is only saved in this stage's response")]),
    ("substitution_intra_list_forward.json", [(C.FORWARD_REF, "stages[0].substitutions", "'b' before the substitution step that defines it")]),
    ("response_step_forward.json", [(C.FORWARD_REF, "stages[0].response", "'token' before the save that produces it")]),
    # One unavailable name is one finding, however many steps reference it.
    ("substitution_duplicate_forward_refs.json", [(C.FORWARD_REF, "stages[0].substitutions", "'t' before")]),
    ("response_duplicate_forward_refs.json", [(C.FORWARD_REF, "stages[0].response", "'token' before")]),
    # A regex save's names are known one by one, like a JMESPath save's: after
    # an earlier regex save, a pattern reading a later one's name is still
    # a forward reference, and not a name that may already exist.
    ("save_regex_forward_ref.json", [(C.FORWARD_REF, "stages[0].response", "'customer' before the save that produces it")]),
    # A group a literal pattern does not have fails at load, as at runtime.
    (
        "save_regex_group_unknown.json",
        [
            (
                C.SCHEMA,
                "stages -> 0 -> response -> 0 -> save -> save -> regex -> regex -> order_id -> capture",
                r"regex 'Order #\(\?P<id>\\d\+\)' has no group named 'order' \(its named groups: 'id'\)",
            )
        ],
    ),
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
    # skip_if decides once per stage, before any foreach iteration and before
    # the stage's own response.
    ("skip_if_out_of_scope.json", [(C.UNDEFINED_VAR, "stages[0].skip_if", r"^Stage 'fan_out': skip_if references potentially undefined variable\(s\): \['item'\]$")]),
    ("skip_if_forward_ref.json", [(C.FORWARD_REF, "stages[0].skip_if", r"^Stage 'create': skip_if references 'sid', which is only saved in this stage's response$")]),
    # So does the parallel config, collect_saves included: one list shape for
    # the whole stage, decided before its iterations exist.
    (
        "parallel_collect_saves_out_of_scope.json",
        [(C.UNDEFINED_VAR, "stages[0].parallel", r"^Stage 'create': parallel references potentially undefined variable\(s\): \['name'\]$")],
    ),
    # So does retry: the attempts of every iteration follow one schedule,
    # resolved before the first attempt and its response exist.
    ("retry_out_of_scope.json", [(C.UNDEFINED_VAR, "stages[0].retry", r"^Stage 'fan_out': retry references potentially undefined variable\(s\): \['polls'\]$")]),
    ("retry_forward_ref.json", [(C.FORWARD_REF, "stages[0].retry", r"^Stage 'poll': retry references 'retry_after', which is only saved in this stage's response$")]),
    # thresholds render with the parallel config, before any iteration.
    (
        "parallel_thresholds_out_of_scope.json",
        [(C.UNDEFINED_VAR, "stages[0].parallel", r"^Stage 'fan_out': parallel references potentially undefined variable\(s\): \['budget'\]$")],
    ),
    # A stage's stats exist once every iteration has ended: none of its own
    # phases reads them, its response steps and always_run included.
    (
        "parallel_stats_as_read_in_own_stage.json",
        [
            (
                C.FORWARD_REF,
                "stages[0].response",
                r"^Stage 'load': response step references 'load_stats', which is only saved as this stage's parallel\.stats_as, once every iteration has ended$",
            ),
            (
                C.FORWARD_REF,
                "stages[1].always_run",
                r"^Stage 'cleanup': always_run references 'cleanup_stats', which is only saved as this stage's parallel\.stats_as — always_run is evaluated before the stage runs$",
            ),
        ],
    ),
    # Saved after the response steps' saves, the stats replace one of the same name.
    (
        "parallel_stats_as_replaces_save.json",
        [
            (
                C.STATS_REPLACE_SAVE,
                "stages[0].parallel.stats_as",
                r"^Stage 'load': parallel\.stats_as 'result' is also a name its response saves: the stats are saved after the response steps' saves "
                r"and replace that one, which no later stage can read\. Rename one of them\.$",
            )
        ],
    ),
    # A skipped stage saves nothing and the chain goes on: a later stage then
    # reads a name only it saves, and fails.
    (
        "skip_if_skippable_save.json",
        [
            (
                C.UNDEFINED_VAR,
                "stages[1].request",
                r"^Stage 'profile': request references 'token', which only stage 'login' saves, and it has skip_if: when it skips, 'token' is undefined "
                r"here — read it with get\('token', <default>\)$",
            )
        ],
    ),
    # So does a stage its own skip, skipif or xfail mark skips: pytest reports
    # it skipped, which leaves the chain going.
    (
        "skip_mark_skippable_save.json",
        [
            (
                C.UNDEFINED_VAR,
                "stages[1].request",
                r"^Stage 'profile': request references 'token', which only stage 'login' saves, and it has the mark \"skip\(reason='login is not "
                r"deployed yet'\)\": when it skips, 'token' is undefined here — read it with get\('token', <default>\)$",
            )
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
    # $include/$merge are never JSON Schema keywords: a migration leftover.
    ("inline_schema_scenario_directive.json", [(C.SCHEMA_SCENARIO_DIRECTIVE, "stages[0].response[0].verify.body.schema", r"\['\$include'\]")]),
    # functions-substitution kwargs reach wrap_function raw: a template there
    # is dead text (and, not being rendered, not a data-flow reference).
    ("substitution_function_kwargs_template_ok.json", [(C.TEMPLATE_IN_KWARGS, "stages[0].substitutions", "'helper' kwarg 'arg'")]),
    ("scenario_function_kwargs_template.json", [(C.TEMPLATE_IN_KWARGS, "substitutions", "'seed' kwarg 'arg'")]),
    ("response_save_function_kwargs_template.json", [(C.TEMPLATE_IN_KWARGS, "stages[0].response[1].save.substitutions", "'extract' kwarg 'path'")]),
    # Never rendered, a kwarg or a key keeps an escape's backslash, which was
    # meant to be removed: said so, as a template there is.
    (
        "escaped_braces_in_kwargs.json",
        [(C.TEMPLATE_IN_KWARGS, "substitutions", r"^Function 'render' kwarg 'template' has a backslash before '\{\{', but .* unrendered, backslash included")],
    ),
    (
        "escaped_braces_in_key.json",
        [(C.TEMPLATE_IN_KEY, "stages[0].request.headers", r"^Key '\\\\\{\{ name \}\}' has a backslash before '\{\{', but .* the key is sent as written")],
    ),
    (
        "escaped_braces_in_jmespath_key.json",
        [(C.TEMPLATE_IN_KEY, "stages[0].response[0].verify.jmespath", r"but a verify.jmespath key is never rendered, so JMESPath evaluates it as written")],
    ),
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
    # A path with only an escape names the file it renders to: the one the
    # stage opens, braces and all.
    ("deep_binary_escaped_missing.json", [(C.REFERENCED_FILE_NOT_FOUND, "stages[0].request.body.binary", r"^Referenced file not found: definitely_missing_\{\{name\}\}\.bin$")]),
    # Named per field / per element, not once for the whole value.
    ("deep_files_missing.json", [(C.REFERENCED_FILE_NOT_FOUND, "stages[0].request.body.files.absent", "definitely_missing_upload")]),
    # A file object's path, and one in a list, named where it is written.
    ("deep_files_object_missing.json", [(C.REFERENCED_FILE_NOT_FOUND, "stages[0].request.body.files.absent.path", "definitely_missing_upload")]),
    ("deep_multipart_file_missing.json", [(C.REFERENCED_FILE_NOT_FOUND, "stages[0].request.body.multipart.files.images[1].path", "definitely_missing_image")]),
    ("deep_ssl_cert_pair_missing.json", [(C.REFERENCED_FILE_NOT_FOUND, "ssl.cert[0]", r"\.crt"), (C.REFERENCED_FILE_NOT_FOUND, "ssl.cert[1]", r"\.key")]),
    ("deep_ssl_verify_missing.json", [(C.REFERENCED_FILE_NOT_FOUND, "ssl.verify", "ca-bundel.pem")]),
    # Three schema-file outcomes: missing, unparseable, parseable but not a schema.
    ("deep_schema_file_missing.json", [(C.REFERENCED_FILE_NOT_FOUND, "stages[0].response[0].verify.body.schema", "Schema file not found")]),
    ("deep_schema_invalid.json", [(C.SCHEMA_FILE_INVALID, "stages[0].response[0].verify.body.schema", "is not valid JSON")]),
    ("deep_schema_not_a_schema.json", [(C.SCHEMA_FILE_INVALID, "stages[0].response[0].verify.body.schema", "not a valid JSON Schema")]),
    # A JSON pointer into the file: it leads nowhere, or to what is not a schema.
    (
        "deep_schema_pointer_nowhere.json",
        [
            (
                C.SCHEMA_FILE_INVALID,
                "stages[0].response[0].verify.body.schema",
                r"Schema pointer '#/components/schemas/Nobody' leads nowhere in .*openapi\.json: '#/components/schemas' has no key 'Nobody'$",
            )
        ],
    ),
    (
        "deep_schema_pointer_not_a_schema.json",
        [
            (
                C.SCHEMA_FILE_INVALID,
                "stages[0].response[0].verify.body.schema",
                r"^Schema file is not a valid JSON Schema: .*openapi\.json#/components/schemas/Invalid: 12 is not valid",
            )
        ],
    ),
    # The references the schema reaches, followed as the runtime follows them:
    # a file that is not there is a missing file, anything else an invalid schema.
    (
        "deep_schema_ref_file_missing.json",
        [
            (
                C.REFERENCED_FILE_NOT_FOUND,
                "stages[0].response[0].verify.body.schema",
                r"RefsMissingFile': \$ref 'missing\.json' names .*schema_refs[/\\]missing\.json, which does not exist$",
            )
        ],
    ),
    (
        "deep_schema_ref_remote.json",
        [(C.SCHEMA_FILE_INVALID, "stages[0].response[0].verify.body.schema", r"\$ref 'https://schemas\.example\.com/user\.json' .* remote references are not fetched")],
    ),
    # Only deep checks a reference's target against its meta-schema: the
    # runtime meta-checks the schema the pointer selects.
    (
        "deep_schema_ref_invalid_target.json",
        [(C.SCHEMA_FILE_INVALID, "stages[0].response[0].verify.body.schema", r"\$ref '#/components/schemas/Invalid' points to an invalid JSON Schema: 12 is not valid")],
    ),
    # An inline schema's references resolve against the scenario's directory.
    (
        "deep_inline_schema_ref_file_missing.json",
        [
            (
                C.REFERENCED_FILE_NOT_FOUND,
                "stages[0].response[0].verify.body.schema",
                r"^Inline body schema: \$ref 'shared_schema_that_does_not_exist\.json' names .*test_validation[/\\]shared_schema",
            )
        ],
    ),
    (
        "deep_inline_schema_ref_pointer_nowhere.json",
        [
            (
                C.SCHEMA_FILE_INVALID,
                "stages[0].response[0].verify.body.schema",
                r"\$ref 'schema_refs/common\.json#/\$defs/Phone' points to nothing: '#/\$defs/Phone' is not in .*common\.json$",
            )
        ],
    ),
    # Only an inline schema is rendered before it is used: a file's template
    # is resolved as the text it is, at runtime and here.
    (
        "deep_schema_file_ref_template.json",
        [
            (
                C.REFERENCED_FILE_NOT_FOUND,
                "stages[0].response[0].verify.body.schema",
                r"templated_ref\.json': \$ref '\{\{ which \}\}\.json#/\$defs/Email' names .*schema_refs[/\\]\{\{ which \}\}\.json, which does not exist$",
            )
        ],
    ),
    # The runtime resolves a $dynamicRef through the same registry as a $ref.
    (
        "deep_inline_schema_dynamic_ref_missing.json",
        [
            (
                C.REFERENCED_FILE_NOT_FOUND,
                "stages[0].response[0].verify.body.schema",
                r"^Inline body schema: \$dynamicRef 'shared_schema_that_does_not_exist\.json#/\$defs/Email' names ",
            )
        ],
    ),
    # An $id JSON Schema reads on the pointer's way that is not a string: it
    # crashed the whole `validate --deep` run with a TypeError.
    (
        "deep_schema_pointer_id_not_a_string.json",
        [
            (
                C.SCHEMA_FILE_INVALID,
                "stages[0].response[0].verify.body.schema",
                r"^Body schema file '.*id_not_a_string\.json#/\$defs/a': \$id 5 is not a string: an \$id is a URI reference$",
            )
        ],
    ),
    # urljoin refuses the $id, where it crashed the whole `validate` run.
    (
        "deep_inline_schema_bad_id.json",
        [(C.SCHEMA_FILE_INVALID, "stages[0].response[0].verify.body.schema", r"^Inline body schema: \$id 'http://\[x' cannot be resolved: Invalid IPv6 URL$")],
    ),
    # A reference to a file keeps the rules a scenario's $include path keeps.
    (
        "deep_inline_schema_ref_absolute.json",
        [(C.SCHEMA_FILE_INVALID, "stages[0].response[0].verify.body.schema", r"\$ref '/schemas/common\.json#/\$defs/Email' is an absolute path, which is not allowed")],
    ),
    (
        "deep_inline_schema_ref_too_deep.json",
        [
            (
                C.SCHEMA_FILE_INVALID,
                "stages[0].response[0].verify.body.schema",
                r"\$ref '\.\./\.\./\.\./\.\./schemas/common\.json#/\$defs/Email' exceeds the maximum parent traversal depth of 3 \(httpchain_ref_parent_traversal_depth\)",
            )
        ],
    ),
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


def test_deep_schema_check_that_crashes_is_one_finding(datadir, monkeypatch):
    """Whatever a body schema's walk raises is that schema's finding, not a
    traceback that ends the run, and every other file's findings with it."""

    def crash(self):
        raise TypeError("Cannot mix str and non-str arguments")
        yield

    monkeypatch.setattr(BodySchema, "unresolvable", crash)
    [diagnostic] = _validate(datadir, "deep_schema_pointer_ok.json").diagnostics
    assert (diagnostic.code, diagnostic.location) == (C.SCHEMA_FILE_INVALID, "stages[0].response[0].verify.body.schema")
    assert re.search(r"^Body schema file '.*openapi\.json#/components/schemas/User': its references cannot be checked: Cannot mix str and non-str arguments$", diagnostic.message)


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


@pytest.mark.parametrize(
    ("root", "found"),
    [
        # The root the load held the scenario's own references to: the nearest
        # pytest config, as the runtime's is pytest's rootdir.
        pytest.param(None, True, id="resolved-root"),
        pytest.param("explicit", True, id="explicit-root"),
        pytest.param("wider", False, id="root-that-holds-it"),
    ],
)
def test_deep_schema_reference_outside_the_root(tmp_path, root, found):
    """Generated, not a ``test_validation/`` fixture: the file must sit outside
    the root, which a fixture directory copied as the root cannot hold."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "shared.json").write_text(json.dumps({"$defs": {"Id": {"type": "integer"}}}))
    scenario = _write(project, [_stage(response=[{"verify": {"body": {"schema": {"$ref": "../shared.json#/$defs/Id"}}}}])])
    root_path = {None: None, "explicit": project, "wider": tmp_path}[root]
    result = validate_scenario(scenario, root_path=root_path, deep=True)

    if not found:
        assert result.diagnostics == []
        return
    assert [(d.code, d.location) for d in result.diagnostics] == [(C.SCHEMA_FILE_INVALID, "stages[0].response[0].verify.body.schema")]
    assert result.diagnostics[0].message == (
        f"Inline body schema: $ref '../shared.json#/$defs/Id' names {tmp_path / 'shared.json'}, outside the reference root {project.resolve()}: "
        f"a schema's references must stay within it, as a scenario's $include must"
    )


def test_deep_schema_reference_depth_is_the_load_s(datadir):
    """``validate --ref-parent-traversal-depth`` bounds a body schema's
    references as it bounds the scenario's own."""
    fixture = datadir / "deep_inline_schema_ref_too_deep.json"
    assert "exceeds the maximum parent traversal depth" in validate_scenario(fixture, deep=True).diagnostics[0].message
    assert not any("exceeds" in d.message for d in validate_scenario(fixture, deep=True, ref_parent_traversal_depth=4).diagnostics)


def test_wrong_extension_warns(datadir):
    """A .jsonc file does not (`jsonc_ok.jsonc`): pytest collects both."""
    result = validate_scenario(datadir / "wrong_extension.txt")
    assert _codes(result) == [C.WRONG_EXTENSION]
    assert result.warnings == ["File has extension '.txt' but expected '.json' or '.jsonc'. Consider renaming to use one of these extensions."]
    assert result.valid is True


def test_directory_is_not_a_file(tmp_path):
    assert _codes(validate_scenario(tmp_path)) == [C.NOT_A_FILE]


class TestValidatePaths:
    """`validate_paths`, what `validate` runs on its paths: the files each one
    stands for, in what order, and the finding for a directory without any.
    (The search itself: test_discovery.py.)"""

    @staticmethod
    def _scenarios(root, *names):
        for name in names:
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            _write((root / name).parent, [_stage()]).rename(root / name)

    def test_each_file_once_sorted_by_path(self, tmp_path):
        """Sorted by path, whatever order the arguments come in (a directory's
        files so in pytest's order), a file reached twice once, under the path
        first reached by. `other` holds a scenario, so it is no empty
        directory, though its one file was reached before it."""
        self._scenarios(tmp_path, "suite/test_b.http.json", "suite/api/test_a.http.json", "other/test_c.http.json")
        paths = [
            tmp_path / "suite" / "api",
            tmp_path / "suite",
            tmp_path / "other" / "test_c.http.json",
            tmp_path / "other",
            tmp_path / "suite" / ".." / "suite" / "api" / "test_a.http.json",
        ]
        results = validate_paths(paths, suffix="http")
        assert [path.relative_to(tmp_path).as_posix() for path, _ in results] == ["other/test_c.http.json", "suite/api/test_a.http.json", "suite/test_b.http.json"]
        assert all(result.diagnostics == [] for _, result in results)

    def test_a_path_that_is_no_directory_is_validated_as_a_file(self, tmp_path):
        """Whatever its name, or whether it exists: named, it is meant."""
        self._scenarios(tmp_path, "scenario.txt")
        results = validate_paths([tmp_path / "scenario.txt", tmp_path / "missing"], suffix="http")
        assert [(path.name, _codes(result)) for path, result in results] == [("missing", [C.FILE_NOT_FOUND]), ("scenario.txt", [C.WRONG_EXTENSION])]

    def test_a_directory_without_scenario_files_is_one_finding(self, tmp_path):
        """Named by the suffix searched for; once, however often it is given."""
        self._scenarios(tmp_path, "suite/test_a.http.json")
        results = validate_paths([tmp_path / "suite", tmp_path, tmp_path / "suite"], suffix="api")
        assert [(path, _codes(result)) for path, result in results] == [(tmp_path, [C.NO_SCENARIO_FILES]), (tmp_path / "suite", [C.NO_SCENARIO_FILES])]
        assert results[1][1].errors == [f"No scenario files named test_<name>.api.json or test_<name>.api.jsonc in directory: {tmp_path / 'suite'}"]

    def test_reads_the_suffix_from_pytests_configuration_only_for_a_directory(self, tmp_path):
        """No directory, nothing to search: a configuration file pytest could
        not read does not fail a run over files."""
        self._scenarios(tmp_path, "suite/test_a.api.json")
        (tmp_path / "pytest.ini").write_text("[pytest]\nhttpchain_suffix = api\n")
        assert [path.name for path, _ in validate_paths([tmp_path / "suite"])] == ["test_a.api.json"]
        (tmp_path / "pytest.ini").write_text("  not ini")
        file = tmp_path / "suite" / "test_a.api.json"
        assert [path for path, _ in validate_paths([file])] == [file]
        assert [path for path, _ in validate_paths([tmp_path / "suite"], suffix="api")] == [file]
        with pytest.raises(DiscoveryError, match="^cannot read pytest configuration from "):
            validate_paths([tmp_path / "suite"])

    def test_searches_every_directory_before_validating_a_file(self, tmp_path, monkeypatch):
        """A run that cannot know its files does no work first."""
        self._scenarios(tmp_path, "test_a.http.json")
        (tmp_path / "pyproject.toml").write_text("[tool.pytest\n")
        validated = []
        monkeypatch.setattr(validation_validate, "validate_scenario", lambda path, **_: validated.append(path))
        with pytest.raises(DiscoveryError):
            validate_paths([tmp_path / "test_a.http.json", tmp_path])
        assert validated == []


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


class TestRetryOnMerge:
    """A stage's retry.on lists alternatives too, any one kind of which makes
    another attempt: a sibling ["request"] written to narrow a shared
    ["verify"] was concatenated onto it, and the stage then resent a request
    that timed out. It merges as a scalar does, as verify.status does."""

    POLL = {"attempts": 10, "delay": 0.5, "on": ["verify"]}

    @pytest.fixture
    def write(self, tmp_path):
        (tmp_path / "common.json").write_text(json.dumps({"poll": self.POLL, "stage": _stage(retry=self.POLL)}))
        return lambda stages: _write(tmp_path, stages)

    @pytest.mark.parametrize(
        ("stages", "where"),
        [
            pytest.param([_stage(retry={"$merge": "common.json#/poll", "on": ["request"]})], "on", id="merge-into-retry"),
            pytest.param([{"$merge": "common.json#/stage", "retry": {"on": ["request"]}}], "retry.on", id="merge-into-stage"),
            pytest.param({"s": _stage(retry={"$merge": "common.json#/poll", "on": ["request"]})}, "on", id="mapping-form-stages"),
            pytest.param([_stage(retry={"$merge": "common.json#/poll", "on": "request"})], "on", id="one-kind-sibling"),
        ],
    )
    def test_different_on_is_a_merge_conflict(self, write, stages, where):
        result = validate_scenario(write(stages))
        assert [(d.code, d.message) for d in result.diagnostics] == [(C.REF_ERROR, f"JSON reference resolution error: Merge conflict at {where}")]

    def test_equal_on_keeps_and_other_keys_still_merge(self, write):
        scenario, raw = load_scenario(write([_stage(retry={"$merge": "common.json#/poll", "on": ["verify"], "backoff": 2})]))
        assert raw["stages"][0]["retry"] == {"attempts": 10, "delay": 0.5, "on": ["verify"], "backoff": 2}
        retry = scenario.stages[0].retry
        assert retry is not None
        assert retry.on == ["verify"]


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

    @pytest.mark.parametrize("include", [False, True], ids=["scenario", "included-file"])
    @pytest.mark.parametrize(
        ("content", "code", "message"),
        [
            # A comment never closed, at its opening: line 3 of the file it is in.
            pytest.param(b'{\n  "a": 1,\n  /* never closed\n', C.INVALID_JSON, "Invalid JSON syntax{in_file}: Unterminated comment: line 3 column 3 (char 14)", id="syntax"),
            pytest.param(b'{"a": ' + TOO_DEEP_TO_PARSE + b"}", C.PARSE_ERROR, "Failed to parse JSON file{file}: nested too deeply (", id="too-deep"),
            # A word json.loads reads as a number, though JSON has no such
            # number: the file is no JSON file.
            pytest.param(b'{\n  "timeout": NaN\n}', C.INVALID_JSON, "Invalid JSON syntax{in_file}: NaN is not valid JSON: line 2 column 14 (char 15)", id="nan"),
        ],
    )
    def test_error_in_an_included_file_names_it(self, tmp_path, include, content, code, message):
        """The diagnostic is the scenario's, so a line and column in a file it
        pulls in read as the scenario's own: it said `Invalid JSON syntax:
        Unterminated comment: line 3 column 3 (char 14)`, with no file name.
        The scenario's own error needs none."""
        bad = tmp_path / ("part.jsonc" if include else "test_x.http.json")
        bad.write_bytes(content)
        scenario = _write(tmp_path, [{"$include": "part.jsonc"}]) if include else bad

        result = on_bounded_stack(validate_scenario, scenario)

        assert _codes(result) == [code]
        named = str(bad.resolve()) if include else None
        expected = message.format(in_file=f" in {named}" if named else "", file=f" {named}" if named else "")
        assert result.diagnostics[0].message.startswith(expected), result.diagnostics[0].message

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
    """The CLI's default $ref root (resolve_root_path) is the rootdir pytest
    would determine for a run on the file from the current directory, which
    collection holds references to: the directory of the configuration file
    pytest reads, else of the nearest setup.py, else the common ancestor of
    the current directory and the file's."""

    @staticmethod
    def _scenario_in(directory):
        directory.mkdir(parents=True, exist_ok=True)
        scenario = directory / "test_x.http.json"
        scenario.write_text("{}")
        return scenario

    def test_finds_project_marker(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")
        assert resolve_root_path(self._scenario_in(tmp_path / "suites" / "api")) == tmp_path

    def test_setup_py_is_a_rootdir(self, tmp_path):
        (tmp_path / "setup.py").write_text("")
        assert resolve_root_path(self._scenario_in(tmp_path / "suites" / "api")) == tmp_path

    def test_without_a_configuration_the_current_directory_counts(self, tmp_path, monkeypatch):
        """pytest's rootdir is then the common ancestor of the current
        directory and the file's, as a run from the project's directory has."""
        _skip_if_ancestors_set_a_rootdir(tmp_path)
        scenario = self._scenario_in(tmp_path / "a" / "b")
        monkeypatch.chdir(tmp_path)
        assert resolve_root_path(scenario) == tmp_path

    def test_without_a_configuration_from_elsewhere_the_file_parent(self, tmp_path, monkeypatch):
        """A common ancestor that is the root of the file system is no
        rootdir: the file's own directory is."""
        _skip_if_ancestors_set_a_rootdir(tmp_path)
        scenario = self._scenario_in(tmp_path / "a" / "b")
        monkeypatch.chdir(tmp_path.anchor)
        assert resolve_root_path(scenario) == tmp_path / "a" / "b"

    def test_on_another_drive_than_the_current_directory_the_file_parent(self, tmp_path, monkeypatch):
        """On Windows a path on another drive than the current directory has no
        common ancestor with it, and pytest keeps the current directory as its
        rootdir then, which holds none of the paths (a CI runner's checkout on
        D:, its temporary directory on C:). The paths' own is taken instead, as
        for a common ancestor that is the root of the file system."""
        _skip_if_ancestors_set_a_rootdir(tmp_path)
        scenario = self._scenario_in(tmp_path / "a" / "b")

        def on_other_drives(paths):
            raise ValueError("Paths don't have the same drive")

        monkeypatch.setattr(discovery.os.path, "commonpath", on_other_drives)
        assert resolve_root_path(scenario) == tmp_path / "a" / "b"

    def test_bare_marker_used_when_no_pytest_config_anywhere(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "solo"\n')
        assert resolve_root_path(self._scenario_in(tmp_path)) == tmp_path

    def test_ref_above_scenario_dir_resolves_within_project_root(self, tmp_path, monkeypatch):
        """A $ref reaching above the scenario's own tree but inside the
        project resolves by default, run from the project's directory, as
        pytest collection accepts it."""
        _skip_if_ancestors_set_a_rootdir(tmp_path)
        (tmp_path / "shared").mkdir()
        (tmp_path / "shared" / "common.json").write_text(json.dumps({"url": "http://server/x", "method": "GET"}))
        suite = tmp_path / "tests" / "api"
        suite.mkdir(parents=True)
        monkeypatch.chdir(tmp_path)

        result = validate_scenario(_write(suite, [_stage(request={"$ref": "../../shared/common.json"})]))

        assert result.diagnostics == []

    @pytest.mark.parametrize(
        ("filename", "content"),
        [
            ("pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'),
            ("pyproject.toml", '[tool.pytest]\ntestpaths = ["tests"]\n'),
            # pytest.ini and pytest.toml count unconditionally — they exist
            # only for pytest, so they need no section to prove intent.
            ("pytest.ini", "[pytest]\n"),
            ("pytest.ini", ""),
            (".pytest.ini", ""),
            ("pytest.toml", "[pytest]\n"),
            ("pytest.toml", ""),
            (".pytest.toml", ""),
            ("tox.ini", "[pytest]\ntestpaths = tests\n"),
            ("setup.cfg", "[tool:pytest]\ntestpaths = tests\n"),
        ],
        ids=[
            "pyproject.toml",
            "pyproject.toml-native",
            "pytest.ini",
            "pytest.ini-empty",
            ".pytest.ini",
            "pytest.toml",
            "pytest.toml-empty",
            ".pytest.toml",
            "tox.ini",
            "setup.cfg",
        ],
    )
    def test_pytest_config_beats_a_nearer_bare_marker(self, tmp_path, filename, content):
        """A sub-package's plain pyproject.toml is not a pytest rootdir.
        Preferring it would shrink the CLI's root below pytest's, so `validate`
        would reject $ref targets that collection resolves fine — for every
        file pytest accepts as a configuration file, not just pyproject.toml."""
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

    def test_a_file_outside_the_runs_root_has_its_own(self, tmp_path, monkeypatch):
        """Files of two projects in one run (a pre-commit hook passing the
        changed files of a monorepo): pytest's rootdir for the run is the
        first project's, which holds none of the second's files, so every
        reference of theirs would fail. A file the run's root does not hold
        gets the root pytest gives it alone, its own project's."""
        for name in ("a", "b"):
            (tmp_path / name).mkdir()
            (tmp_path / name / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n')
            (tmp_path / name / "common.json").write_text(json.dumps({"url": "http://server/x"}))
            (tmp_path / name / "sub").mkdir()
            _write(tmp_path / name / "sub", [_stage(request={"$include": "../common.json"})])
        monkeypatch.chdir(tmp_path)

        results = validate_paths([Path("a/sub/test_x.http.json"), Path("b/sub/test_x.http.json")])

        assert [result.diagnostics for _, result in results] == [[], []]

    @pytest.mark.parametrize(
        "content",
        ["this is not [ valid toml", '[tool.pytest]\nx = 1\n[tool.pytest.ini_options]\ny = "2"\n'],
        ids=["unparseable", "both-tables"],
    )
    def test_a_pyproject_pytest_would_refuse_is_still_a_bare_marker(self, tmp_path, monkeypatch, content):
        """Passed over as one holding no pytest configuration is: its
        directory is still the root when nothing else sets one, as 0.16.0
        had it, not narrowed to the current directory."""
        (tmp_path / "pyproject.toml").write_text(content)
        scenario = self._scenario_in(tmp_path / "tests" / "api")
        monkeypatch.chdir(tmp_path / "tests" / "api")
        assert resolve_root_path(scenario) == tmp_path

    def test_files_of_one_run_share_its_root(self, tmp_path, monkeypatch):
        """`validate a b` holds both files to the rootdir `pytest a b` has,
        the common ancestor here, where a root for each file alone was its
        own directory, and a reference to a file beside the two failed."""
        _skip_if_ancestors_set_a_rootdir(tmp_path)
        (tmp_path / "shared.json").write_text(json.dumps({"url": "http://server/x"}))
        for name in ("a", "b"):
            (tmp_path / name).mkdir()
            _write(tmp_path / name, [_stage(request={"$include": "../shared.json"})])
        monkeypatch.chdir(tmp_path.anchor)

        results = validate_paths([tmp_path / "a", tmp_path / "b"])

        assert [result.diagnostics for _, result in results] == [[], []]


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
