def test_primer(run_scenario):
    result = run_scenario("primer/test_primer.http.json", "primer/common.json")
    result.assert_outcomes(
        errors=0,
        failed=0,
        passed=1,
        skipped=0,
        xfailed=0,
        xpassed=0,
    )
