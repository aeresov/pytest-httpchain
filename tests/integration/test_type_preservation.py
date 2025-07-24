"""Test type preservation in variable verification."""


def test_type_preservation_integer_variables(pytester):
    """Test that integer variables saved from JMESPath maintain their type during verification."""
    pytester.copy_example("type_preservation/conftest.py")
    pytester.copy_example("type_preservation/test_type_preservation.http.json")

    result = pytester.runpytest("-v")

    # This should pass once we fix the type preservation issue
    result.assert_outcomes(passed=2, failed=0)
