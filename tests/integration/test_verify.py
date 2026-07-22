from tests.integration.conftest import run_scenario


def test_verify_status(pytester):
    """Test status code verification"""
    result = run_scenario(pytester, "verify/test_verify_status.http.json")
    # 2 stages = 2 test methods
    result.assert_outcomes(errors=0, failed=0, passed=2)


def test_verify_headers(pytester):
    """Test header verification"""
    result = run_scenario(pytester, "verify/test_verify_headers.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_verify_expressions(pytester):
    """Test bool expression verification"""
    result = run_scenario(pytester, "verify/test_verify_expressions.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_verify_user_function(pytester):
    """Test user function returning bool"""
    result = run_scenario(pytester, "verify/test_verify_user_function.http.json", "verify.py")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_verify_body_schema(pytester):
    """Test JSON schema validation"""
    result = run_scenario(pytester, "verify/test_verify_body_schema.http.json", "verify/schema.json")
    # 2 stages = 2 test methods
    result.assert_outcomes(errors=0, failed=0, passed=2)


def test_verify_body_schema_inline_defs(pytester):
    """An inline schema using standard JSON Schema $ref/$defs is opaque to the
    scenario's reference resolver: it survives collection intact and jsonschema
    genuinely applies the schema-internal $ref (the second stage's unsatisfiable
    def must fail the verify, not error out)."""
    result = run_scenario(pytester, "verify/test_verify_body_schema_defs.http.json")
    result.assert_outcomes(errors=0, failed=1, passed=1)
    result.stdout.fnmatch_lines(["*schema*"])


def test_verify_body_schema_unresolvable_ref_fails_cleanly(pytester):
    """An unresolvable $ref inside an inline schema is a stage failure like any
    other: a clean VerificationError (with the HTTP exchange attached), and the
    chain aborts so the next stage skips — no raw referencing traceback, no
    stages running past the failure."""
    result = run_scenario(pytester, "verify/test_verify_body_schema_broken_ref.http.json")
    result.assert_outcomes(errors=0, failed=1, passed=0, skipped=1)
    result.stdout.fnmatch_lines(["*Cannot resolve*body schema*"])
    result.stdout.no_fnmatch_line("*_WrappedReferencingError*")


def test_verify_body_contains(pytester):
    """Test body contains/not_contains"""
    result = run_scenario(pytester, "verify/test_verify_body_contains.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_verify_body_matches(pytester):
    """Test body regex matching"""
    result = run_scenario(pytester, "verify/test_verify_body_matches.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)
