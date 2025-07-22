def test_mark_skip(pytester):
    pytester.copy_example("conftest.py")
    pytester.copy_example("test_mark_skip.http.json")
    result = pytester.runpytest()
    # No stages = no tests to run
    result.assert_outcomes()


def test_mark_xfail(pytester):
    pytester.copy_example("conftest.py")
    pytester.copy_example("test_mark_xfail.http.json")
    result = pytester.runpytest()
    # Test correctly xfails because URL is invalid
    result.assert_outcomes(xfailed=1)
