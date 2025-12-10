def test_primer(pytester):
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("primer/common.json")
    pytester.copy_example("primer/test_primer.http.json")
    result = pytester.runpytest("-s")
    print(result.stdout.str())
    print(result.stderr.str())
    result.assert_outcomes(
        errors=0,
        failed=0,
        passed=1,
        skipped=0,
        xfailed=0,
        xpassed=0,
    )
