# def test_scenario_skip(pytester):
#     pytester.copy_example("mark/conftest.py")
#     pytester.copy_example("mark/test_scenario_skip.http.json")
#     result = pytester.runpytest()
#     # No stages = no tests to run
#     result.assert_outcomes()


# def test_scenario_xfail(pytester):
#     pytester.copy_example("mark/conftest.py")
#     pytester.copy_example("mark/test_scenario_xfail.http.json")
#     result = pytester.runpytest()
#     # Test correctly xfails because URL is invalid
#     result.assert_outcomes(xfailed=1)
