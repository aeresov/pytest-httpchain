"""Tests for regex body verification."""


def test_regex_matches_pass(pytester):
    """Test that regex patterns correctly match response body content."""
    pytester.copy_example("regex/conftest.py")
    pytester.copy_example("regex/test_regex_matches_pass.http.json")

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=2, failed=0)


def test_regex_not_matches_pass(pytester):
    """Test that regex patterns correctly do not match response body content."""
    pytester.copy_example("regex/conftest.py")
    pytester.copy_example("regex/test_regex_not_matches_pass.http.json")

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=2, failed=0)


def test_regex_matches_fail(pytester):
    """Test that regex verification fails when patterns don't match."""
    pytester.copy_example("regex/conftest.py")
    pytester.copy_example("regex/test_regex_matches_fail.http.json")

    result = pytester.runpytest("-v")
    result.assert_outcomes(failed=1)
    assert "pattern 'This pattern should not be found' did not match response body" in result.stdout.str()


def test_regex_not_matches_fail(pytester):
    """Test that regex verification fails when patterns that should not match do match."""
    pytester.copy_example("regex/conftest.py")
    pytester.copy_example("regex/test_regex_not_matches_fail.http.json")

    result = pytester.runpytest("-v")
    result.assert_outcomes(failed=1)
    assert "matched response body but should not have" in result.stdout.str()


def test_regex_combined(pytester):
    """Test combining both body_matches and body_not_matches in the same verification."""
    pytester.copy_example("regex/conftest.py")
    pytester.copy_example("regex/test_regex_combined.http.json")

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=1, failed=0)


def test_regex_invalid_pattern(pytester):
    """Test that invalid regex patterns produce clear error messages."""
    pytester.copy_example("regex/conftest.py")
    pytester.copy_example("regex/test_regex_invalid_pattern.http.json")

    result = pytester.runpytest("-v")
    result.assert_outcomes(failed=1)
    assert "Invalid regex pattern" in result.stdout.str()


def test_regex_with_schema(pytester):
    """Test combining JSON schema validation with regex patterns."""
    pytester.copy_example("regex/conftest.py")
    pytester.copy_example("regex/test_regex_with_schema.http.json")

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=1, failed=0)


def test_substring_contains_pass(pytester):
    """Test that substring contains verification works correctly."""
    pytester.copy_example("regex/conftest.py")
    pytester.copy_example("regex/test_substring_contains_pass.http.json")

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=2, failed=0)


def test_substring_not_contains_pass(pytester):
    """Test that substring not_contains verification works correctly."""
    pytester.copy_example("regex/conftest.py")
    pytester.copy_example("regex/test_substring_not_contains_pass.http.json")

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=2, failed=0)


def test_substring_contains_fail(pytester):
    """Test that substring verification fails when substring not found."""
    pytester.copy_example("regex/conftest.py")
    pytester.copy_example("regex/test_substring_contains_fail.http.json")

    result = pytester.runpytest("-v")
    result.assert_outcomes(failed=1)
    assert "substring 'This string should not be found' not found in response body" in result.stdout.str()


def test_substring_not_contains_fail(pytester):
    """Test that substring verification fails when unwanted substring is found."""
    pytester.copy_example("regex/conftest.py")
    pytester.copy_example("regex/test_substring_not_contains_fail.http.json")

    result = pytester.runpytest("-v")
    result.assert_outcomes(failed=1)
    assert "found in response body" in result.stdout.str()


def test_substring_combined(pytester):
    """Test combining substring and regex verification."""
    pytester.copy_example("regex/conftest.py")
    pytester.copy_example("regex/test_substring_combined.http.json")

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=1, failed=0)
