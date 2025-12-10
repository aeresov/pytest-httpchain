def test_template_uuid(pytester):
    """Test uuid4() generation"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("templates/test_template_uuid.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_template_expressions(pytester):
    """Test Python expressions in templates"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("templates/test_template_expressions.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_template_type_preservation(pytester):
    """Test complete templates preserve type (int, dict, etc.)"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("templates/test_template_type_preservation.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_template_helpers(pytester):
    """Test exists() and get() helper functions"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("templates/test_template_helpers.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=0, passed=1)
