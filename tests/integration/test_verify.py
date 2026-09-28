import pytest

from tests.integration.helpers import named, stage

# Every row copies both: user-function and schema-file scenarios need them,
# and an unused copy is harmless.
AUX = ("verify.py", "verify/schema.json")


@pytest.mark.parametrize(
    ("scenario", "passed"),
    named(
        ("status", 2),
        ("headers", 1),
        ("expressions", 1),
        ("jmespath", 2),
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
        # The same for a header matcher field — here fed by a JMESPath save of a
        # missing key. Its static sibling keeps the matcher valid, so the
        # rendered-away `contains` was simply not checked.
        ("header_matcher_rendered_away", {"failed": 1}, "*'verify.headers.X-Custom-Header.contains' was declared as '{{ expected_value }}' but rendered to None*"),
        # JSON equality, not Python's: `true == 1` would have passed the stage.
        ("jmespath_mismatch", {"failed": 1}, "*JMESPath 'active' doesn't match: expected 1, got true*"),
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


def test_failure_report_lists_every_failed_check_and_a_curl_command(run_scenario):
    """One run shows everything wrong with the response, a template that
    fails to render among it, and a command that sends the request again: its
    credentials stay hidden, with a note to fill them in. The step after the
    failing one does not run."""
    request = {
        "method": "POST",
        "params": {"access_token": "s3cret-query"},
        "headers": {"Authorization": "Bearer s3cret-header"},
        "body": {"json": {"note": "it's"}},
    }
    response = [
        {
            "verify": {
                "status": 201,
                "headers": {"Content-Type": {"contains": "json"}},
                "jmespath": {"received.note": "its"},
                "expressions": ["{{ response.headers['x-missing'] == 'a' }}"],
                "body": {"contains": ["nope"]},
            }
        },
        {"verify": {"status": 404}},
    ]
    result = run_scenario({"stages": [stage("create", "/echo/json", request=request, response=response)]})

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "4 verification checks failed:",
            "  1. Status code doesn't match: expected 201, got 200",
            """  2. JMESPath 'received.note' doesn't match: expected "its", got "it's\"""",
            # Rendered with the step's other values, before any check ran, and
            # listed in its check's place: the status failure is not hidden.
            "  3. KeyError in expression '{{ response.headers[[]'x-missing'] == 'a' }}': 'x-missing'",
            "  4. Body doesn't contain 'nope'",
            "*HTTP Request (curl)*",
            "# [[]REDACTED] stands for a value this report hides: fill it in before running.",
            "curl -X POST 'http://*/echo/json?access_token=[[]REDACTED]' \\",
            "  --globoff \\",
            "  -H 'authorization: [[]REDACTED]' \\",
            "  -H 'content-type: application/json' \\",
            "  --compressed \\",
            # httpx's JSON spacing differs across its supported versions.
            """  --data-raw '{"note":*"it'"'"'s"}'""",
            "*HTTP Response*",
        ]
    )
    output = result.stdout.str()
    assert "s3cret" not in output
    assert "expected 404" not in output
