


def test_basic_json_test_collection_and_execution(pytester):
    pytester.copy_example("test_basic.http.json")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


def test_json_test_with_fixtures(pytester):
    pytester.copy_example("conftest.py")
    pytester.copy_example("test_fixtures.http.json")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


def test_json_test_with_variable_substitution(pytester):
    pytester.copy_example("conftest.py")
    pytester.copy_example("test_substitution.http.json")
    result = pytester.runpytest("-vv")
    result.assert_outcomes(passed=1)


def test_json_test_with_skip_mark(pytester):
    pytester.copy_example("test_skip.http.json")
    result = pytester.runpytest()
    result.assert_outcomes(skipped=1)


def test_invalid_json_fails_gracefully(pytester):
    pytester.copy_example("test_invalid.http.json")
    result = pytester.runpytest()
    result.assert_outcomes(failed=1)


def test_non_matching_files_not_collected(pytester):
    pytester.copy_example("not_a_test.json")
    pytester.copy_example("not_a_test.http.json")
    pytester.copy_example("test_valid.http.json")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


def test_multiple_stages_in_test(pytester):
    pytester.copy_example("test_multistage.http.json")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


def test_regular_python_tests_work_alongside_plugin(pytester):
    pytester.copy_example("conftest.py")
    pytester.copy_example("test_regular_python.py")
    pytester.copy_example("test_basic.http.json")
    result = pytester.runpytest()
    result.assert_outcomes(passed=3)


def test_json_test_with_skipif_mark(pytester):
    pytester.copy_example("test_mark_skipif.http.json")
    result = pytester.runpytest()
    outcomes = result.parseoutcomes()
    assert outcomes.get("failed", 0) == 0
    assert outcomes.get("passed", 0) + outcomes.get("skipped", 0) == 1


def test_json_test_with_collection_validation_error(pytester):
    pytester.copy_example("test_mark_xfail.http.json")
    result = pytester.runpytest()
    result.assert_outcomes(failed=1)


def test_json_test_with_references(pytester):
    pytester.copy_example("test_ref.http.json")
    pytester.copy_example("ref_stage.json")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)
