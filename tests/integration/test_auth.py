"""Integration tests for authentication functionality."""

def test_scenario_level_basic_auth(pytester):
    """Test scenario-level basic authentication works correctly."""
    pytester.copy_example("auth/conftest.py")
    pytester.copy_example("auth/auth_functions.py")
    pytester.copy_example("auth/test_scenario_auth_basic.http.json")

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=2, failed=0)

    # Check that both public and protected endpoints were accessed
    assert "test_public_endpoint" in result.stdout.str()
    assert "test_protected_endpoint" in result.stdout.str()


def test_stage_level_auth_override(pytester):
    """Test stage-level auth overrides scenario-level auth correctly."""
    pytester.copy_example("auth/conftest.py")
    pytester.copy_example("auth/auth_functions.py")
    pytester.copy_example("auth/test_stage_auth_override.http.json")

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=2, failed=0)

    # Check that both scenario auth and stage override worked
    assert "test_with_scenario_auth" in result.stdout.str()
    assert "test_with_stage_auth_override" in result.stdout.str()


def test_no_auth_fails_on_protected_endpoint(pytester):
    """Test that protected endpoints fail without authentication."""
    pytester.copy_example("auth/conftest.py")
    pytester.copy_example("auth/test_auth_failure.http.json")

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=1, failed=0)

    # Verify the 401 status was expected and handled correctly
    assert "test_no_auth_fails" in result.stdout.str()


def test_invalid_auth_function_fails(pytester):
    """Test that invalid auth function returns proper error."""
    pytester.copy_example("auth/conftest.py")
    pytester.copy_example("auth/auth_functions.py")
    pytester.copy_example("auth/test_invalid_auth_function.http.json")

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=0, errors=1)

    # Check that the error message mentions AuthBase
    assert "must return a requests.AuthBase instance" in result.stdout.str()


def test_nonexistent_auth_function_fails(pytester):
    """Test that nonexistent auth function returns proper error."""
    pytester.copy_example("auth/conftest.py")
    pytester.copy_example("auth/auth_functions.py")
    pytester.copy_example("auth/test_nonexistent_auth_function.http.json")

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=0, failed=1)

    # Check that error mentions the missing function
    assert "not found in module" in result.stdout.str()


def test_auth_with_kwargs_stage_level(pytester):
    """Test stage-level auth functions with kwargs work correctly."""
    pytester.copy_example("auth/conftest.py")
    pytester.copy_example("auth/auth_functions.py")
    pytester.copy_example("auth/test_auth_with_kwargs.http.json")

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=2, failed=0)

    # Check that both variable and literal kwargs worked
    assert "test_basic_auth_with_variables" in result.stdout.str()
    assert "test_basic_auth_with_literals" in result.stdout.str()


def test_auth_with_kwargs_scenario_level(pytester):
    """Test scenario-level auth function with kwargs works correctly."""
    pytester.copy_example("auth/conftest.py")
    pytester.copy_example("auth/auth_functions.py")
    pytester.copy_example("auth/test_scenario_auth_with_kwargs.http.json")

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=2, failed=0)

    # Check that both scenario auth and stage override worked
    assert "test_scenario_auth_with_kwargs" in result.stdout.str()
    assert "test_stage_override_with_kwargs" in result.stdout.str()
