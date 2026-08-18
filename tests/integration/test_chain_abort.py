def test_fixture_setup_error_aborts_chain(run_scenario):
    result = run_scenario("errors/test_fixture_setup_abort.http.json")
    result.assert_outcomes(errors=1, failed=0, passed=0, skipped=1)


def test_fixture_teardown_error_aborts_chain(run_scenario):
    result = run_scenario("errors/test_fixture_teardown_abort.http.json")
    result.assert_outcomes(errors=1, failed=0, passed=1, skipped=1)


def test_strict_xpass_aborts_chain(run_scenario):
    result = run_scenario("marks/test_strict_xpass_abort.http.json")
    result.assert_outcomes(errors=0, failed=1, passed=0, skipped=1)
