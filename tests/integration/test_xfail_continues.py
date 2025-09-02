"""Test that xfail stages don't abort the test flow."""


def test_xfail_stage_continues_execution(pytester):
    """Test that when a stage marked with xfail fails, execution continues."""
    pytester.copy_example("xfail_continues/conftest.py")
    pytester.copy_example("xfail_continues/test_xfail_continues.http.json")

    result = pytester.runpytest("-v")

    # Expect:
    # - First stage passes
    # - Second stage (xfail) fails as expected
    # - Third stage runs and passes (not skipped!)
    result.assert_outcomes(
        errors=0,
        failed=0,
        passed=2,  # first and third stages
        skipped=0,
        xfailed=1,  # second stage
        xpassed=0,
    )


def test_normal_failure_aborts_execution(pytester):
    """Test that when a normal stage fails, subsequent stages are skipped."""
    pytester.copy_example("xfail_continues/conftest.py")
    pytester.copy_example("xfail_continues/test_normal_failure_aborts.http.json")

    result = pytester.runpytest("-v")

    # Expect:
    # - First stage passes
    # - Second stage fails (no xfail mark)
    # - Third stage is skipped due to abort
    result.assert_outcomes(
        errors=0,
        failed=1,  # second stage
        passed=1,  # first stage
        skipped=1,  # third stage
        xfailed=0,
        xpassed=0,
    )
