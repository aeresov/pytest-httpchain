def test_primer(run_scenario):
    result = run_scenario("primer/test_primer.http.json", "primer/common.json")
    result.assert_outcomes(passed=1)
