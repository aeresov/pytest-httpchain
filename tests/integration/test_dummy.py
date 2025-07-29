def test_blip(pytester):
    pytester.copy_example("dummy/conftest.py")
    pytester.copy_example("dummy/common.json")
    pytester.copy_example("dummy/test_blip.http.json")
    result = pytester.runpytest("-s")
    # print(result.stdout.str())
    # print(result.stderr.str())
    result.assert_outcomes(
        errors=0,
        failed=0,
        passed=0,
        skipped=0,
        xfailed=0,
        xpassed=3,
    )
