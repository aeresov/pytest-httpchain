def test_blip(pytester):
    pytester.copy_example("dummy/conftest.py")
    pytester.copy_example("dummy/common.json")
    pytester.copy_example("dummy/test_blip.http.json")
    result = pytester.runpytest()
    result.assert_outcomes(
        errors=0,
        failed=0,
        passed=0,
        skipped=0,
        xfailed=2,
        xpassed=0,
    )
