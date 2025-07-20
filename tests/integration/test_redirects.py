def test_follow_redirects_enabled(pytester):
    """Test that redirects are followed by default (allow_redirects=True)."""
    pytester.copy_example("redirects/conftest.py")
    pytester.copy_example("redirects/test_follow_redirects.http.json")

    result = pytester.runpytest()

    result.assert_outcomes(passed=1, failed=0)


def test_follow_redirects_disabled(pytester):
    """Test that redirects are not followed when allow_redirects=False."""
    pytester.copy_example("redirects/conftest.py")
    pytester.copy_example("redirects/test_no_follow_redirects.http.json")

    result = pytester.runpytest()

    result.assert_outcomes(passed=1, failed=0)


def test_multiple_redirects(pytester):
    """Test that multiple redirects are followed correctly."""
    pytester.copy_example("redirects/conftest.py")
    pytester.copy_example("redirects/test_multiple_redirects.http.json")

    result = pytester.runpytest()

    result.assert_outcomes(passed=1, failed=0)
