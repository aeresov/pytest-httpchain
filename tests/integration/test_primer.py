from tests.integration.conftest import run_scenario


def test_primer(pytester):
    result = run_scenario(pytester, "primer/test_primer.http.json", "primer/common.json")
    result.assert_outcomes(
        errors=0,
        failed=0,
        passed=1,
        skipped=0,
        xfailed=0,
        xpassed=0,
    )
