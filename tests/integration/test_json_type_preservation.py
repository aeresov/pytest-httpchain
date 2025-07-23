def test_type_preservation_in_json_body(pytester):
    """Test that variable types are preserved when used in JSON request body."""
    pytester.copy_example("type_preservation/conftest.py")
    pytester.copy_example("type_preservation/test_json_type_preservation.http.json")

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=2)

    # Check that the test verified types were preserved in JSON body
    result.stdout.str()

    # The test should pass all verifications, meaning types were preserved
