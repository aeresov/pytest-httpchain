def test_fixture_injection(pytester):
    """Test fixture values in stage context"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("fixtures/test_fixture_injection.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_fixture_dict(pytester):
    """Test fixture providing dict values"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("fixtures/test_fixture_dict.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_fixture_factory(pytester):
    """Test callable fixture (factory pattern)"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("fixtures/test_fixture_factory.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=0, passed=1)
