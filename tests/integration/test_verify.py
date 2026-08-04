def test_verify_status(run_scenario):
    """Test status code verification"""
    result = run_scenario("verify/test_verify_status.http.json")
    # 2 stages = 2 test methods
    result.assert_outcomes(errors=0, failed=0, passed=2)


def test_verify_headers(run_scenario):
    """Test header verification"""
    result = run_scenario("verify/test_verify_headers.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_verify_expressions(run_scenario):
    """Test bool expression verification"""
    result = run_scenario("verify/test_verify_expressions.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_verify_user_function(run_scenario):
    """Test user function returning bool"""
    result = run_scenario("verify/test_verify_user_function.http.json", "verify.py")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_verify_user_function_returns_false(run_scenario):
    """A verify function returning False fails the stage with a clear message."""
    result = run_scenario("verify/test_verify_user_function_false.http.json", "verify.py")
    result.assert_outcomes(errors=0, failed=1, passed=0)
    result.stdout.fnmatch_lines(["*verification failed*"])


def test_verify_user_function_returns_non_bool(run_scenario):
    """A verify function returning a non-bool is rejected, not coerced."""
    result = run_scenario("verify/test_verify_user_function_non_bool.http.json", "verify.py")
    result.assert_outcomes(errors=0, failed=1, passed=0)
    result.stdout.fnmatch_lines(["*must return bool*"])


def test_verify_user_function_raises(run_scenario):
    """A raising verify function surfaces as a clean stage failure, not a raw traceback."""
    result = run_scenario("verify/test_verify_user_function_raises.http.json", "verify.py")
    result.assert_outcomes(errors=0, failed=1, passed=0)
    result.stdout.fnmatch_lines(["*Error calling user function*"])


def test_verify_body_schema(run_scenario):
    """Test JSON schema validation"""
    result = run_scenario("verify/test_verify_body_schema.http.json", "verify/schema.json")
    # 2 stages = 2 test methods
    result.assert_outcomes(errors=0, failed=0, passed=2)


def test_verify_body_schema_inline_defs(run_scenario):
    """An inline schema using standard JSON Schema $ref/$defs is opaque to the
    scenario's reference resolver: it survives collection intact and jsonschema
    genuinely applies the schema-internal $ref (the second stage's unsatisfiable
    def must fail the verify, not error out)."""
    result = run_scenario("verify/test_verify_body_schema_defs.http.json")
    result.assert_outcomes(errors=0, failed=1, passed=1)
    result.stdout.fnmatch_lines(["*schema*"])


def test_verify_body_schema_unresolvable_ref_fails_cleanly(run_scenario):
    """An unresolvable $ref inside an inline schema is a stage failure like any
    other: a clean VerificationError (with the HTTP exchange attached), and the
    chain aborts so the next stage skips — no raw referencing traceback, no
    stages running past the failure."""
    result = run_scenario("verify/test_verify_body_schema_broken_ref.http.json")
    result.assert_outcomes(errors=0, failed=1, passed=0, skipped=1)
    result.stdout.fnmatch_lines(["*Cannot resolve*body schema*"])
    result.stdout.no_fnmatch_line("*_WrappedReferencingError*")


def test_verify_body_contains(run_scenario):
    """Test body contains/not_contains"""
    result = run_scenario("verify/test_verify_body_contains.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_verify_body_matches(run_scenario):
    """Test body regex matching"""
    result = run_scenario("verify/test_verify_body_matches.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_verify_status_rendered_away_fails(run_scenario):
    """A `status` template that resolves to null must fail the stage.

    Both the pre-walk and post-walk models validate cleanly (`status` is
    optional), so a truthiness gate silently dropped the only assertion and the
    stage passed green against a 400 — the one outcome a test runner must never
    produce.
    """
    result = run_scenario("verify/test_verify_status_rendered_away.http.json")
    result.assert_outcomes(errors=0, failed=1, passed=0)
    result.stdout.fnmatch_lines(["*rendered to None*"])


def test_stage_failure_message_is_not_duplicated(run_scenario):
    """pytest.fail raised from inside the except block set Failed.__context__,
    and pytest walks the whole __cause__/__context__ chain even under
    pytrace=False — printing one failure 2-4 times."""
    result = run_scenario("verify/test_verify_user_function_false.http.json", "verify.py")
    result.assert_outcomes(errors=0, failed=1, passed=0)

    failures = result.stdout.str().split("=== FAILURES ===")[-1].split("short test summary")[0]
    assert failures.count("The above exception was the direct cause") == 0
    assert failures.count("During handling of the above exception") == 0
