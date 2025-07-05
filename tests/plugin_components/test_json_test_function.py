from pytest_http.pytest_plugin import json_test_function


def test_json_test_function_valid_json(tmp_path):
    test_path = tmp_path / "test.http.json"
    json_content = '{"stages": [{"name": "test", "data": 42}]}'
    json_test_function(json_content, test_path)


def test_json_test_function_with_fixtures(tmp_path):
    test_path = tmp_path / "test.http.json"
    json_content = '{"stages": [{"name": "test", "data": "$value"}]}'
    fixtures = {"value": "test_data"}
    json_test_function(json_content, test_path, **fixtures)
