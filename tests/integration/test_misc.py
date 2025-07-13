def test_regular_python_tests_work_alongside_plugin(pytester):
    pytester.copy_example("conftest.py")
    pytester.copy_example("test_regular_python.py")
    # pytester.copy_example("test_basic.http.json")
    result = pytester.runpytest()
    result.assert_outcomes(passed=2)
