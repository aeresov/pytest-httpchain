def test_stage_xfail(pytester):
    """Test that xfail mark on stage works correctly."""
    pytester.copy_example("stage_marks/conftest.py")
    pytester.copy_example("stage_marks/test_stage_xfail.http.json")
    result = pytester.runpytest("-v")
    # success_stage passes, xfail_stage fails as expected (xfailed), another_success_stage passes
    result.assert_outcomes(passed=2, xfailed=1)
    assert "xfail_stage XFAIL" in result.stdout.str()


def test_stage_skip(pytester):
    """Test that skip mark on stage works correctly."""
    pytester.copy_example("stage_marks/conftest.py")
    pytester.copy_example("stage_marks/test_stage_skip.http.json")
    result = pytester.runpytest("-v")
    # success_stage passes, skip_stage is skipped, another_success_stage passes
    result.assert_outcomes(passed=2, skipped=1)
    assert "skip_stage SKIPPED" in result.stdout.str()


def test_combined_marks(pytester):
    """Test that marks on both scenario and stage are combined correctly."""
    pytester.copy_example("stage_marks/conftest.py")
    pytester.copy_example("stage_marks/test_combined_marks.http.json")

    # Run with -m critical to select only the critical stage
    result = pytester.runpytest("-v", "-m", "critical")
    result.assert_outcomes(passed=1)
    assert "critical_stage PASSED" in result.stdout.str()

    # Run with -m slow to select all stages (since scenario has slow mark)
    result = pytester.runpytest("-v", "-m", "slow")
    result.assert_outcomes(passed=2)

    # Run with -m "slow and critical" to select only critical_stage
    result = pytester.runpytest("-v", "-m", "slow and critical")
    result.assert_outcomes(passed=1)
    assert "critical_stage PASSED" in result.stdout.str()


def test_xfail_stage_continues_flow(pytester):
    """Test that xfail stage doesn't break the test flow - all stages run."""
    pytester.copy_example("stage_marks/conftest.py")
    pytester.copy_example("stage_marks/test_xfail_prevents_flow_break.http.json")
    result = pytester.runpytest("-v")
    # success_stage passes, xfail_stage fails as expected (xfailed), should_still_run passes
    result.assert_outcomes(passed=2, xfailed=1)
    assert "success_stage PASSED" in result.stdout.str()
    assert "xfail_stage_expected_to_fail XFAIL" in result.stdout.str()
    assert "should_still_run PASSED" in result.stdout.str()
