def test_files(pytester):
    pytester.copy_example("body_types/conftest.py")
    pytester.copy_example("body_types/test_files.http.json")
    pytester.copy_example("body_types/textfile1.txt")
    pytester.copy_example("body_types/textfile2.txt")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


def test_form(pytester):
    pytester.copy_example("body_types/conftest.py")
    pytester.copy_example("body_types/test_form.http.json")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


def test_json(pytester):
    pytester.copy_example("body_types/conftest.py")
    pytester.copy_example("body_types/test_json.http.json")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


def test_raw(pytester):
    pytester.copy_example("body_types/conftest.py")
    pytester.copy_example("body_types/test_raw.http.json")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


def test_xml(pytester):
    pytester.copy_example("body_types/conftest.py")
    pytester.copy_example("body_types/test_xml.http.json")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)
