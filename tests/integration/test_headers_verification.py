"""Integration tests for response header verification."""


def test_headers_verification_pass(pytester):
    """Test successful header verification."""
    pytester.copy_example("headers/conftest.py")
    pytester.copy_example("headers/test_headers_pass.http.json")

    result = pytester.runpytest()
    result.assert_outcomes(passed=1, failed=0)


def test_headers_verification_fail(pytester):
    """Test failed header verification with wrong value."""
    pytester.copy_example("headers/conftest.py")
    pytester.copy_example("headers/test_headers_fail.http.json")

    result = pytester.runpytest()
    result.assert_outcomes(passed=0, failed=1)

    assert "Header 'X-Custom-Header' verification failed" in result.stdout.str()
    assert "expected 'wrong-value', got 'custom-value'" in result.stdout.str()


def test_headers_verification_missing(pytester):
    """Test failed header verification with missing header."""
    pytester.copy_example("headers/conftest.py")
    pytester.copy_example("headers/test_headers_missing.http.json")

    result = pytester.runpytest()
    result.assert_outcomes(passed=0, failed=1)

    assert "Header 'X-Non-Existent-Header' verification failed" in result.stdout.str()
    assert "expected 'some-value', got 'None'" in result.stdout.str()
