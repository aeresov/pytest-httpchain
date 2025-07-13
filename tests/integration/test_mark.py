def test_mark_skip(pytester):
    pytester.copy_example("conftest.py")
    pytester.copy_example("test_mark_skip.http.json")
    result = pytester.runpytest()
    result.assert_outcomes(skipped=1)


def test_mark_xfail(pytester):
    pytester.copy_example("conftest.py")
    pytester.copy_example("test_mark_xfail.http.json")
    result = pytester.runpytest()
    result.assert_outcomes(xfailed=1)
