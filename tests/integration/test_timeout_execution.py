def test_timeout_with_slow_server(pytester):
    """Test that timeout properly fails when server response is slow."""
    pytester.copy_example("timeout/conftest.py")
    pytester.copy_example("timeout/test_timeout_fail.http.json")

    # Run the test and expect it to fail due to timeout
    result = pytester.runpytest("-v", "-s")
    result.assert_outcomes(failed=1)
    # Check that the failure message contains the expected text
    assert "HTTP request timed out" in result.stdout.str()
    assert "timeout_test" in result.stdout.str()
    assert "slow" in result.stdout.str()


def test_timeout_with_fast_server(pytester):
    """Test that timeout works correctly when server responds quickly."""
    pytester.copy_example("timeout/conftest.py")
    pytester.copy_example("timeout/test_timeout_pass.http.json")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


def test_timeout_not_specified(pytester):
    """Test that requests work normally when timeout is not specified."""
    pytester.copy_example("timeout/conftest.py")
    pytester.copy_example("timeout/test_no_timeout.http.json")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)
