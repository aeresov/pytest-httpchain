from tests.integration.conftest import run_scenario


def test_skip_scenario(pytester):
    """Test skip mark on entire scenario"""
    result = run_scenario(pytester, "marks/test_skip_scenario.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=0, skipped=1)


def test_skip_stage(pytester):
    """Test skip mark on individual stage"""
    result = run_scenario(pytester, "marks/test_skip_stage.http.json")
    # Two stages pass, one is skipped
    result.assert_outcomes(errors=0, failed=0, passed=2, skipped=1)


def test_xfail_expected(pytester):
    """Test xfail mark - expected failure"""
    result = run_scenario(pytester, "marks/test_xfail.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=0, xfailed=1)


def test_xfail_continues(pytester):
    """Test xfail doesn't abort subsequent stages"""
    result = run_scenario(pytester, "marks/test_xfail_continues.http.json")
    # xfail stage + passing stage after it
    result.assert_outcomes(errors=0, failed=0, passed=1, xfailed=1)


def test_always_run(pytester):
    """Test always_run executes after abort"""
    result = run_scenario(pytester, "marks/test_always_run.http.json")
    # First stage fails, second runs due to always_run
    result.assert_outcomes(errors=0, failed=1, passed=1)


def test_always_run_template(pytester):
    """always_run templates evaluate against fixtures + saved variables after abort"""
    result = run_scenario(pytester, "marks/test_always_run_template.http.json")
    # save_flag passes, failing_stage fails, the template-true cleanup runs,
    # the template-false cleanup is skipped like any other stage after abort
    result.assert_outcomes(errors=0, failed=1, passed=2, skipped=1)


def test_always_run_template_error(pytester):
    """A broken always_run template fails the stage with a clear message"""
    result = run_scenario(pytester, "marks/test_always_run_template_error.http.json")
    result.assert_outcomes(errors=0, failed=2, passed=0)
    result.stdout.fnmatch_lines(["*Failed to evaluate always_run template*"])


def test_always_run_template_fixture_and_param_scope(pytester):
    """always_run templates can read stage fixtures and parametrize parameters"""
    result = run_scenario(pytester, "marks/test_always_run_scope.http.json")
    # failing_stage fails; fixture_cleanup runs (api_key matches);
    # param_cleanup[true] runs, param_cleanup[false] is skipped
    result.assert_outcomes(errors=0, failed=1, passed=2, skipped=1)


def test_always_run_template_lazy(pytester):
    """always_run templates are only evaluated once a stage has failed"""
    result = run_scenario(pytester, "marks/test_always_run_lazy.http.json")
    # The chain is healthy, so the would-crash template is never evaluated
    result.assert_outcomes(errors=0, failed=0, passed=2)
