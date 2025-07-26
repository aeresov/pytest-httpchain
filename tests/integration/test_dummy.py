def test_blip(pytester):
    pytester.copy_example("dummy/conftest.py")
    pytester.copy_example("dummy/test_blip.http.json")
    result = pytester.runpytest()
    result.assert_outcomes(xfailed=1)
