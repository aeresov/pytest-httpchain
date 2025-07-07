import responses

from pytest_http.models import Scenario, Stage
from pytest_http.pytest_plugin import json_test_function


@responses.activate
def test_http_request_with_url():
    responses.add(
        responses.GET,
        "https://api.example.com/users",
        json={"users": [{"id": 1, "name": "John"}, {"id": 2, "name": "Jane"}]},
        status=200,
        headers={"Content-Type": "application/json"},
    )

    test_data = {"stages": [{"name": "get_users", "url": "https://api.example.com/users", "data": {}}]}

    json_test_function(test_data)

    assert len(responses.calls) == 1
    assert responses.calls[0].request.url == "https://api.example.com/users"


@responses.activate
def test_http_request_with_params():
    responses.add(responses.GET, "https://api.example.com/users", json={"users": [{"id": 1, "name": "John"}]}, status=200, headers={"Content-Type": "application/json"})

    test_data = {"stages": [{"name": "get_user", "url": "https://api.example.com/users", "params": {"id": 1, "format": "json"}, "data": {}}]}

    json_test_function(test_data)

    assert len(responses.calls) == 1
    assert "id=1" in responses.calls[0].request.url
    assert "format=json" in responses.calls[0].request.url


@responses.activate
def test_http_request_with_headers():
    responses.add(responses.GET, "https://api.example.com/users", json={"users": []}, status=200, headers={"Content-Type": "application/json"})

    test_data = {
        "stages": [{"name": "get_users", "url": "https://api.example.com/users", "headers": {"Authorization": "Bearer token123", "Accept": "application/json"}, "data": {}}]
    }

    json_test_function(test_data)

    assert len(responses.calls) == 1
    assert responses.calls[0].request.headers["Authorization"] == "Bearer token123"
    assert responses.calls[0].request.headers["Accept"] == "application/json"


@responses.activate
def test_http_request_with_save():
    responses.add(
        responses.GET, "https://api.example.com/user/1", json={"id": 1, "name": "John", "email": "john@example.com"}, status=200, headers={"Content-Type": "application/json"}
    )

    test_data = {
        "stages": [{"name": "get_user", "url": "https://api.example.com/user/1", "data": {}, "save": {"user_id": "json.id", "user_name": "json.name", "status": "status_code"}}]
    }

    json_test_function(test_data)

    assert len(responses.calls) == 1


@responses.activate
def test_multiple_stages_with_http_requests():
    responses.add(responses.GET, "https://api.example.com/users", json={"users": [{"id": 1, "name": "John"}]}, status=200)

    responses.add(responses.GET, "https://api.example.com/user/1", json={"id": 1, "name": "John", "details": "Additional info"}, status=200)

    test_data = {
        "stages": [{"name": "get_users", "url": "https://api.example.com/users", "data": {}}, {"name": "get_user_details", "url": "https://api.example.com/user/1", "data": {}}]
    }

    json_test_function(test_data)

    assert len(responses.calls) == 2
    assert responses.calls[0].request.url == "https://api.example.com/users"
    assert responses.calls[1].request.url == "https://api.example.com/user/1"


def test_stage_without_url():
    test_data = {"stages": [{"name": "no_http_stage", "data": {"some": "data"}}]}

    json_test_function(test_data)


@responses.activate
def test_http_request_failure():
    responses.add(responses.GET, "https://api.example.com/users", json={"error": "Not found"}, status=404)

    test_data = {"stages": [{"name": "get_users", "url": "https://api.example.com/users", "data": {}}]}

    json_test_function(test_data)

    assert len(responses.calls) == 1


def test_stage_model_validation():
    stage = Stage(name="test_stage", data={"test": "data"}, url="https://api.example.com/test", params={"key": "value"}, headers={"Content-Type": "application/json"})

    assert stage.name == "test_stage"
    assert stage.url == "https://api.example.com/test"
    assert stage.params == {"key": "value"}
    assert stage.headers == {"Content-Type": "application/json"}

    stage_no_http = Stage(name="no_http_stage", data={"test": "data"})

    assert stage_no_http.name == "no_http_stage"
    assert stage_no_http.url is None
    assert stage_no_http.params is None
    assert stage_no_http.headers is None


def test_scenario_model_validation():
    scenario = Scenario(
        stages=[
            {"name": "http_stage", "data": {}, "url": "https://api.example.com/test", "params": {"key": "value"}, "headers": {"Authorization": "Bearer token"}},
            {"name": "no_http_stage", "data": {"some": "data"}},
        ]
    )

    assert len(scenario.stages) == 2
    assert scenario.stages[0].url == "https://api.example.com/test"
    assert scenario.stages[1].url is None
