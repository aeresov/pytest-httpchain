def test_skip_scenario(pytester):
    """Test skip mark on entire scenario"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("marks/test_skip_scenario.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=0, passed=0, skipped=1)


def test_skip_stage(pytester):
    """Test skip mark on individual stage"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("marks/test_skip_stage.http.json")
    result = pytester.runpytest("-s")
    # Two stages pass, one is skipped
    result.assert_outcomes(errors=0, failed=0, passed=2, skipped=1)


def test_xfail_expected(pytester):
    """Test xfail mark - expected failure"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("marks/test_xfail.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=0, passed=0, xfailed=1)


def test_xfail_continues(pytester):
    """Test xfail doesn't abort subsequent stages"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("marks/test_xfail_continues.http.json")
    result = pytester.runpytest("-s")
    # xfail stage + passing stage after it
    result.assert_outcomes(errors=0, failed=0, passed=1, xfailed=1)


def test_always_run(pytester):
    """Test always_run executes after abort"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("marks/test_always_run.http.json")
    result = pytester.runpytest("-s")
    # First stage fails, second runs due to always_run
    result.assert_outcomes(errors=0, failed=1, passed=1)
