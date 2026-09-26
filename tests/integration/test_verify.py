import pytest

from tests.integration.helpers import named

# Every row copies both: user-function and schema-file scenarios need them,
# and an unused copy is harmless.
AUX = ("verify.py", "verify/schema.json")


@pytest.mark.parametrize(
    ("scenario", "passed"),
    named(
        ("status", 2),
        ("headers", 1),
        ("expressions", 1),
        ("user_function", 1),
        ("body_schema", 2),
        ("body_contains", 1),
        ("body_matches", 1),
    ),
)
def test_verify_passes(run_scenario, scenario, passed):
    run_scenario(f"verify/test_verify_{scenario}.http.json", *AUX).assert_outcomes(passed=passed)


@pytest.mark.parametrize(
    ("scenario", "outcomes", "line"),
    named(
        # `{{ response.status }}` against a 400 renders to the truthy int 400:
        # a truthiness gate passed the stage while asserting nothing at all.
        ("expression_non_bool", {"failed": 1}, "*must evaluate to bool*"),
        ("user_function_false", {"failed": 1}, "*verification failed*"),
        # Rejected, not coerced.
        ("user_function_non_bool", {"failed": 1}, "*must return bool*"),
        # A clean stage failure, not a raw traceback.
        ("user_function_raises", {"failed": 1}, "*Error calling user function*"),
        # An inline schema's own $ref/$defs are opaque to the scenario resolver
        # and genuinely applied by jsonschema: the `$ref`ed def is what rejects
        # the second stage's body — not a resolution error or some other
        # schema complaint.
        ("body_schema_defs", {"passed": 1, "failed": 1}, "*Body schema validation failed*'email' is a required property*"),
        # An unresolvable $ref inside an inline schema is a stage failure like
        # any other: the chain aborts, so the next stage skips.
        ("body_schema_broken_ref", {"failed": 1, "skipped": 1}, "*Cannot resolve*body schema*"),
        # A `status` template that resolves to null must fail: both models
        # validate cleanly (`status` is optional), so a truthiness gate silently
        # dropped the only assertion and passed green against a 400.
        ("status_rendered_away", {"failed": 1}, "*rendered to None*"),
    ),
)
def test_verify_fails_cleanly(run_scenario, scenario, outcomes, line):
    result = run_scenario(f"verify/test_verify_{scenario}.http.json", *AUX)
    result.assert_outcomes(**outcomes)
    result.stdout.fnmatch_lines([line])
    result.stdout.no_fnmatch_line("*_WrappedReferencingError*")


def test_stage_failure_message_is_not_duplicated(run_scenario):
    """pytest.fail raised from inside the except block set Failed.__context__,
    and pytest walks the whole __cause__/__context__ chain even under
    pytrace=False — printing one failure 2-4 times."""
    result = run_scenario("verify/test_verify_user_function_false.http.json", "verify.py")
    result.assert_outcomes(failed=1)

    failures = result.stdout.str().split("=== FAILURES ===")[-1].split("short test summary")[0]
    assert failures.count("The above exception was the direct cause") == 0
    assert failures.count("During handling of the above exception") == 0
