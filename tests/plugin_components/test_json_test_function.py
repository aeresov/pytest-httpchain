import json

from pytest_http.pytest_plugin import json_test_function


def test_json_test_function_valid_json(tmp_path):
    json_content = '{"stages": [{"name": "test", "request": {"url": "https://httpbin.org/json"}}]}'
    test_data = json.loads(json_content)
    json_test_function(test_data)


def test_json_test_function_with_fixtures(tmp_path):
    json_content = '{"stages": [{"name": "test", "request": {"url": "https://httpbin.org/json", "params": {"key": "$value"}}}]}'
    test_data = json.loads(json_content)
    fixtures = {"value": "test_data"}
    json_test_function(test_data, **fixtures)
