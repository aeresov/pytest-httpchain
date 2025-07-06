import json

from pytest_http.pytest_plugin import json_test_function


def test_json_test_function_valid_json(tmp_path):
    test_path = tmp_path / "test.http.json"
    json_content = '{"stages": [{"name": "test", "data": 42}]}'
    test_data = json.loads(json_content)
    json_test_function(test_data, test_path)


def test_json_test_function_with_fixtures(tmp_path):
    test_path = tmp_path / "test.http.json"
    json_content = '{"stages": [{"name": "test", "data": "$value"}]}'
    test_data = json.loads(json_content)
    fixtures = {"value": "test_data"}
    json_test_function(test_data, test_path, **fixtures)
