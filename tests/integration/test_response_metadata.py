"""Declarative response metadata (task 25).

Response steps see a ``response`` namespace (status, reason, headers,
elapsed_ms) in their template context — usable in ``verify.expressions`` and
as a header save source via substitution saves — and ``verify.headers``
accepts matcher objects (contains/not_contains/matches/not_matches) besides
exact-match strings.
"""

from tests.integration.helpers import stage


def test_response_namespace_in_verify_expressions(run_scenario):
    expressions = [
        "{{ response.status == 200 }}",
        "{{ 'json' in response.headers['content-type'] }}",
        "{{ response.headers['x-custom-header'] == 'test-value' }}",
        "{{ response.elapsed_ms >= 0 }}",
    ]
    result = run_scenario({"stages": [stage("meta", "/headers", response=[{"verify": {"expressions": expressions}}])]})
    result.assert_outcomes(passed=1)


def test_save_header_via_substitutions(run_scenario):
    """The header save source: a substitutions save reads response.headers;
    the saved value is visible to the next stage."""
    save = {"save": {"substitutions": [{"vars": {"req_id": "{{ response.headers['x-request-id'] }}"}}]}}
    scenario = {
        "stages": [
            stage("capture", "/headers", response=[save]),
            stage("use", response=[{"verify": {"expressions": ["{{ req_id == '12345' }}"]}}]),
        ]
    }
    result = run_scenario(scenario)
    result.assert_outcomes(passed=2)


def test_header_matchers(run_scenario):
    headers = {
        "content-type": {"contains": "json"},
        "x-request-id": {"matches": "^[0-9]+$"},
        "x-custom-header": "test-value",
        "x-absent": {"not_contains": "anything"},
    }
    result = run_scenario({"stages": [stage("matchers", "/headers", response=[{"verify": {"headers": headers}}])]})
    result.assert_outcomes(passed=1)


def test_header_matcher_failure_names_the_header(run_scenario):
    verify = {"verify": {"headers": {"x-custom-header": {"contains": "nope"}}}}
    result = run_scenario({"stages": [stage("fails", "/headers", response=[verify])]})
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*x-custom-header*doesn't contain*nope*"])


def test_response_namespace_not_visible_in_request(run_scenario):
    """The namespace exists for response steps only: referencing it in a
    request template is still an error at runtime. (The static twin,
    HTTPCHAIN003 at collection, is pinned by test_validation.)"""
    result = run_scenario({"stages": [stage("bad", request={"headers": {"x-echo": "{{ response.status }}"}})]})
    result.assert_outcomes(failed=1)
    # Pin the cause: `failed=1` alone would also pass if the stage broke on the
    # URL template, or on anything else this scenario happens to touch.
    result.stdout.fnmatch_lines(["*'response' is not defined*"])


def test_user_function_saving_reserved_name_warns_at_runtime(pytester, run_scenario):
    """The static HTTPCHAIN027 check cannot see user_functions save keys, so
    the shadowing must surface as a runtime warning instead of silently
    changing what `response` means in later response steps."""
    pytester.syspathinsert()
    pytester.makepyfile(saver="def extract(response):\n    return {'response': {'id': 5}}\n")
    steps = [{"save": {"user_functions": ["saver:extract"]}}, {"verify": {"status": 200}}]
    result = run_scenario({"stages": [stage("s", response=steps)]})
    result.assert_outcomes(passed=1, warnings=1)
    result.stdout.fnmatch_lines(["*HTTPCHAIN027*"])
