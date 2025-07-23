def test_type_preservation_in_params(pytester):
    """Test that variable types are preserved when used in request parameters."""
    pytester.copy_example("type_preservation/conftest.py")
    pytester.copy_example("type_preservation/test_type_preservation.http.json")

    result = pytester.runpytest("-v", "-s")
    result.assert_outcomes(passed=2)

    # The test should pass, meaning types were preserved correctly
    # when variables are used in request parameters
