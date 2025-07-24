"""Integration tests for eval-based variable substitution."""


def test_eval_expressions(pytester):
    """Test eval-based expressions with double curly braces."""
    pytester.copy_example("eval_substitution/conftest.py")
    pytester.copy_example("eval_substitution/test_eval_expressions.http.json")

    result = pytester.runpytest()
    result.assert_outcomes(passed=4)
