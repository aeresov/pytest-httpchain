def test_scenario_auth(run_scenario):
    """Test scenario-level auth function"""
    result = run_scenario("auth/test_scenario_auth.http.json", "auth.py")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_request_auth(run_scenario):
    """Test request-level auth"""
    result = run_scenario("auth/test_request_auth.http.json", "auth.py")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_auth_from_fixture(run_scenario):
    """Test auth using fixture values"""
    result = run_scenario("auth/test_auth_from_fixture.http.json", "auth.py")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_request_auth_failure_fails_stage_cleanly(run_scenario):
    """A raising request-level auth function becomes a clean stage failure that
    aborts the chain — not a raw traceback bypassing the abort machinery (the
    scenario-level twin lives in the lazy-init suite; this is request_builder's
    per-request branch)."""
    result = run_scenario("auth/test_request_auth_failure.http.json", "auth.py")
    result.assert_outcomes(errors=0, failed=1, passed=0, skipped=1)
    result.stdout.fnmatch_lines(["*Failed to configure authentication*"])
