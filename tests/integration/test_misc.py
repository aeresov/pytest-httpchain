def test_regular_python_tests_work(pytester):
    pytester.copy_example("conftest.py")
    pytester.copy_example("test_regular_python.py")
    result = pytester.runpytest()
    result.assert_outcomes(passed=2)


def test_mock_server(pytester):
    pytester.copy_example("conftest.py")
    pytester.copy_example("test_mock_server.py")
    result = pytester.runpytest()
    result.assert_outcomes(passed=2)
