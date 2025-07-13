def test_full(pytester):
    pytester.copy_example("conftest.py")
    pytester.copy_example("test_full.http.json")
    pytester.copy_example("stage_ref.json")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)
