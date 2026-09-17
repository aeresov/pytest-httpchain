def test_parametrize_individual(run_scenario):
    """Test stage parametrize with individual values"""
    result = run_scenario("parametrize/test_parametrize_individual.http.json")
    # 3 parametrized test cases
    result.assert_outcomes(errors=0, failed=0, passed=3)


def test_parametrize_combinations(run_scenario):
    """Test stage parametrize with combinations"""
    result = run_scenario("parametrize/test_parametrize_combinations.http.json")
    # 2 parametrized test cases
    result.assert_outcomes(errors=0, failed=0, passed=2)


def test_parametrize_multiple(run_scenario):
    """Test multiple parametrize steps (cartesian product)"""
    result = run_scenario("parametrize/test_parametrize_multiple.http.json")
    # 2 x 2 = 4 parametrized test cases
    result.assert_outcomes(errors=0, failed=0, passed=4)


def test_parametrize_templated_combinations(run_scenario):
    """`combinations` given as a single template resolves at collection.

    The resolved value is fed back through CombinationsParameter, because the
    model's own validation only ever saw the template string. Without that
    round trip the keys of the first combination decide the parameter names for
    all of them.
    """
    result = run_scenario("parametrize/test_parametrize_templated_combinations.http.json")
    # 2 combinations, each verifying its own expected_name — so a combination
    # whose second key was dropped would fail rather than silently pass.
    result.assert_outcomes(errors=0, failed=0, passed=2)


def test_parametrize_templated_combinations_ragged_fails_collection(run_scenario):
    """Combinations that disagree on their keys are rejected by name at
    collection — not with a bare KeyError naming neither the index nor the
    problem, and not by silently dropping the extra key."""
    result = run_scenario("parametrize/test_parametrize_templated_combinations_ragged.http.json", args="--collect-only")
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*different parameters*"])
