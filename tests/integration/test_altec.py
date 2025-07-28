def test_user_session(pytester):
    pytester.copy_example("altec/conftest.py")
    pytester.copy_example("altec/status.json")
    pytester.copy_example("altec/session.json")
    pytester.copy_example("altec/test_user_session.http.json")
    result = pytester.runpytest("-s")
    print(result.stdout.str())
    print(result.stderr.str())
    result.assert_outcomes(
        errors=0,
        failed=0,
        passed=2,
        skipped=0,
        xfailed=0,
        xpassed=0,
    )
