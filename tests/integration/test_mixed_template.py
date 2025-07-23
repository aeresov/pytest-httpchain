def test_mixed_template_substitution(pytester):
    """Test that mixed template substitution works correctly with the new syntax."""
    pytester.copy_example("type_preservation/conftest.py")
    pytester.copy_example("type_preservation/test_mixed_template.http.json")

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=1)

    # The test should pass, meaning both single variables and mixed templates work
