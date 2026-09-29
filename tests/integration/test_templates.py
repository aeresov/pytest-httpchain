import pytest

from tests.integration.helpers import stage


# uuid4(); Python expressions; complete templates keep their type (int, dict,
# ...); the exists()/get() helpers.
@pytest.mark.parametrize("scenario", ["uuid", "expressions", "type_preservation", "helpers"])
def test_template_features(run_scenario, scenario):
    run_scenario(f"templates/test_template_{scenario}.http.json").assert_outcomes(passed=1)


def test_value_nested_hundreds_deep_collects_and_runs(run_scenario):
    """A `vars` value 600 levels deep loads fine. Collection used to crash with a
    RecursionError: the validator walked it recursively, two frames per level.
    The stage's template walk spent two frames per level of it as well."""
    deep = "{{ 'x' }}"
    for _ in range(600):
        deep = {"k": deep}
    run_scenario({"stages": [stage("deep", substitutions=[{"vars": {"deep": deep}}])]}).assert_outcomes(passed=1)


def test_builtin_functions_in_a_round_trip(run_scenario):
    """The time, encoding, hashing and URL helpers build a request, and check in
    its response steps what the server received; `urlencode` builds a query the
    server reads back. Collection's validator takes each helper for a built-in,
    so the run carries no undefined-variable warning."""
    run_scenario("templates/test_template_functions.http.json").assert_outcomes(passed=2, warnings=0)


def test_objects_read_by_key(run_scenario):
    """A saved JSON object and `vars` objects read by key, keys with a dash
    included, into request headers and verify expressions, with `in`, len(),
    iteration and the keys/values/items/get methods; a `vars` object whose
    templates render per stage keeps its key access, and `response` reads by
    key too. Collection's validator flags none of it."""
    run_scenario("templates/test_template_mapping_access.http.json").assert_outcomes(passed=2, warnings=0)


def test_missing_key_fails_the_stage_naming_it(run_scenario):
    """A subscript of a key the object does not have fails its stage as a
    missing attribute does, naming the key, not with a bare KeyError."""
    result = run_scenario(
        {
            "substitutions": [{"vars": {"trace": {"X-Request-Id": "req-42"}}}],
            "stages": [stage("traced", request={"headers": {"X-Trace": "{{ trace['X-Trace-Id'] }}"}})],
        }
    )
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*Key error in expression '{{ trace[[]'X-Trace-Id'] }}': Key 'X-Trace-Id' does not exist in expression 'trace[[]'X-Trace-Id']'*"])
    result.stdout.no_fnmatch_line("*Traceback*")


def test_helper_without_parentheses_fails_the_stage(run_scenario):
    """`{{ now }}` for `{{ now() }}` would send `<function now at 0x...>`: the
    validator warns of it at collection (HTTPCHAIN035), and the request that
    would carry it fails its stage cleanly, naming the call to write."""
    result = run_scenario({"stages": [stage("stamped", request={"headers": {"X-Sent-At": "{{ now }}"}})]})
    result.assert_outcomes(failed=1, warnings=1)
    result.stdout.fnmatch_lines(
        [
            "Uncalled function in expression '{{ now }}': no value named 'now' is defined here, so now is the built-in function, not a value; "
            "if the built-in is meant, call it: now()",
            "*HTTPCHAIN035] Stage 'stamped': request uses the built-in function 'now' without calling it*",
        ]
    )
    result.stdout.no_fnmatch_line("*Traceback*")


def test_assignment_for_a_comparison_fails_the_stage(run_scenario):
    """`=` for `==` against a 400: simpleeval evaluated the assignment to its
    right-hand side behind a mere warning, so the expression came out True
    and the stage passed. Collection warns of it (HTTPCHAIN037), and nothing
    else of it (reading its identifiers had `True` undefined), and the stage
    fails cleanly with the same reason."""
    result = run_scenario(
        {
            "stages": [
                stage(
                    "checked",
                    "/bad",
                    response=[{"save": {"substitutions": [{"vars": {"passed": "{{ response.status == 200 }}"}}]}}, {"verify": {"expressions": ["{{ passed = True }}"]}}],
                )
            ]
        }
    )
    result.assert_outcomes(failed=1, warnings=1)
    reason = "a template holds one expression, not an assignment; to compare two values, write '=='"
    result.stdout.fnmatch_lines(
        [
            f"*Invalid expression '{{{{ passed = True }}}}': {reason}",
            f"*HTTPCHAIN037] Stage 'checked': response has an invalid expression '{{{{ passed = True }}}}', and rendering it fails the stage: {reason}*",
        ]
    )
    result.stdout.no_fnmatch_line("*Traceback*")


def test_builtin_called_where_a_stage_defines_its_own_warns_and_runs(run_scenario):
    """One stage fakes the clock with a function substitution named
    `timestamp` (frozen at 0); the scenario-level substitutions, which no
    stage's definitions reach, call the built-in. Collection warns of that
    (HTTPCHAIN036) and the scenario runs, where the validator used to stop it
    at collection with an HTTPCHAIN017 error."""
    result = run_scenario(
        {
            "substitutions": [{"vars": {"started": "{{ timestamp() }}"}}],
            "stages": [
                stage("real", response=[{"verify": {"status": 200, "expressions": ["{{ started > 1700000000 }}"]}}]),
                stage(
                    "frozen",
                    substitutions=[{"functions": {"timestamp": "builtins:int"}}],
                    response=[{"verify": {"status": 200, "expressions": ["{{ timestamp() == 0 }}"]}}],
                ),
            ],
        }
    )
    result.assert_outcomes(passed=2, warnings=1)
    result.stdout.fnmatch_lines(["*HTTPCHAIN036] Scenario-level 'substitutions' uses the function timestamp(), but the scenario's own definition of 'timestamp'*"])
