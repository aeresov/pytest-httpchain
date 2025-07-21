"""Integration tests for response body schema validation."""


def test_schema_inline_pass(pytester):
    """Test that inline schema validation passes for valid response."""
    pytester.copy_example("schema/conftest.py")
    pytester.copy_example("schema/test_schema_inline_pass.http.json")

    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


def test_schema_file_pass(pytester):
    """Test that file-based schema validation passes for valid response."""
    pytester.copy_example("schema/conftest.py")
    pytester.copy_example("schema/user_schema.json")
    pytester.copy_example("schema/test_schema_file_pass.http.json")

    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


def test_schema_inline_fail(pytester):
    """Test that inline schema validation fails for invalid response."""
    pytester.copy_example("schema/conftest.py")
    pytester.copy_example("schema/test_schema_inline_fail.http.json")

    result = pytester.runpytest()
    result.assert_outcomes(failed=1)

    # Check for specific validation error messages
    output = result.stdout.str()
    assert "Response body schema validation failed" in output
    # The actual error is about missing 'email' field, not type error
    assert "'email' is a required property" in output


def test_schema_extra_fields_fail(pytester):
    """Test that schema validation fails when additionalProperties is false."""
    pytester.copy_example("schema/conftest.py")
    pytester.copy_example("schema/user_schema.json")
    pytester.copy_example("schema/test_schema_extra_fields_fail.http.json")

    result = pytester.runpytest()
    result.assert_outcomes(failed=1)

    # Check for specific validation error about additional properties
    output = result.stdout.str()
    assert "Response body schema validation failed" in output
    assert "Additional properties are not allowed" in output


def test_schema_file_not_found(pytester):
    """Test that missing schema file results in appropriate error."""
    pytester.copy_example("schema/conftest.py")
    # Don't copy the schema file to simulate missing file
    pytester.copy_example("schema/test_schema_file_pass.http.json")

    result = pytester.runpytest()
    result.assert_outcomes(failed=1)

    output = result.stdout.str()
    assert "Failed to load schema file" in output


def test_invalid_schema(pytester):
    """Test that invalid JSON schema results in appropriate error."""
    pytester.copy_example("schema/conftest.py")
    pytester.copy_example("schema/test_invalid_schema.http.json")

    result = pytester.runpytest()
    result.assert_outcomes(failed=1)

    output = result.stdout.str()
    assert "Invalid JSON schema" in output
