import pytest
import requests
import responses

from pytest_http.models import Scenario, Stage
from pytest_http.pytest_plugin import json_test_function


@pytest.mark.parametrize(
    "stage_config,expected_url,expected_assertions,description",
    [
        (
            {"name": "get_users", "url": "https://api.example.com/users"},
            "https://api.example.com/users",
            {"url": "https://api.example.com/users"},
            "basic_url"
        ),
        (
            {
                "name": "get_user",
                "url": "https://api.example.com/users",
                "params": {"id": 1, "format": "json"}
            },
            "https://api.example.com/users",
            {"url_contains": ["id=1", "format=json"]},
            "with_params"
        ),
        (
            {
                "name": "get_users",
                "url": "https://api.example.com/users",
                "headers": {"Authorization": "Bearer token123", "Accept": "application/json"}
            },
            "https://api.example.com/users",
            {"headers": {"Authorization": "Bearer token123", "Accept": "application/json"}},
            "with_headers"
        ),
        (
            {
                "name": "get_user",
                "url": "https://api.example.com/user/1",
                "save": {"vars": {"user_id": "json.id", "user_name": "json.name", "status": "status_code"}}
            },
            "https://api.example.com/user/1",
            {"url": "https://api.example.com/user/1"},
            "with_save"
        ),
    ],
)
@responses.activate
def test_http_request_configurations(stage_config, expected_url, expected_assertions, description):
    # Setup response based on test case
    if description == "with_save":
        responses.add(
            responses.GET,
            expected_url,
            json={"id": 1, "name": "John", "email": "john@example.com"},
            status=200,
            headers={"Content-Type": "application/json"}
        )
    else:
        responses.add(
            responses.GET,
            expected_url,
            json={"users": [{"id": 1, "name": "John"}]},
            status=200,
            headers={"Content-Type": "application/json"}
        )

    test_data = {"stages": [stage_config]}
    json_test_function(test_data)

    assert len(responses.calls) == 1

    # Verify specific assertions based on test case
    if "url" in expected_assertions:
        assert responses.calls[0].request.url == expected_assertions["url"]

    if "url_contains" in expected_assertions:
        for substring in expected_assertions["url_contains"]:
            assert substring in responses.calls[0].request.url

    if "headers" in expected_assertions:
        for header_name, header_value in expected_assertions["headers"].items():
            assert responses.calls[0].request.headers[header_name] == header_value


@pytest.mark.parametrize(
    "stages_config,expected_calls,description",
    [
        (
            [
                {"name": "get_users", "url": "https://api.example.com/users"},
                {"name": "get_user_details", "url": "https://api.example.com/user/1"}
            ],
            ["https://api.example.com/users", "https://api.example.com/user/1"],
            "multiple_http_stages"
        ),
        (
            [{"name": "no_http_stage"}],
            [],
            "no_http_stage"
        ),
    ],
)
@responses.activate
def test_multiple_stages_scenarios(stages_config, expected_calls, description):
    # Setup responses for HTTP stages
    if description == "multiple_http_stages":
        responses.add(
            responses.GET,
            "https://api.example.com/users",
            json={"users": [{"id": 1, "name": "John"}]},
            status=200
        )
        responses.add(
            responses.GET,
            "https://api.example.com/user/1",
            json={"id": 1, "name": "John", "details": "Additional info"},
            status=200
        )

    test_data = {"stages": stages_config}
    json_test_function(test_data)

    assert len(responses.calls) == len(expected_calls)
    for i, expected_url in enumerate(expected_calls):
        assert responses.calls[i].request.url == expected_url


@pytest.mark.parametrize(
    "response_status,expected_behavior",
    [
        (404, "should_pass"),  # Plugin should handle errors gracefully
        (500, "should_pass"),
        (200, "should_pass"),
    ],
)
@responses.activate
def test_http_request_error_handling(response_status, expected_behavior):
    responses.add(
        responses.GET,
        "https://api.example.com/users",
        json={"error": "Not found"} if response_status >= 400 else {"data": "success"},
        status=response_status
    )

    test_data = {"stages": [{"name": "get_users", "url": "https://api.example.com/users"}]}

    if expected_behavior == "should_pass":
        json_test_function(test_data)
        assert len(responses.calls) == 1
    else:
        with pytest.raises((requests.RequestException, ValueError)):
            json_test_function(test_data)


@pytest.mark.parametrize(
    "stage_data,expected_attrs",
    [
        (
            {
                "name": "test_stage",
                "url": "https://api.example.com/test",
                "params": {"key": "value"},
                "headers": {"Content-Type": "application/json"}
            },
            {
                "name": "test_stage",
                "url": "https://api.example.com/test",
                "params": {"key": "value"},
                "headers": {"Content-Type": "application/json"}
            }
        ),
        (
            {"name": "no_http_stage"},
            {
                "name": "no_http_stage",
                "url": None,
                "params": None,
                "headers": None
            }
        ),
    ],
)
def test_stage_model_validation(stage_data, expected_attrs):
    stage = Stage(**stage_data)

    for attr_name, expected_value in expected_attrs.items():
        assert getattr(stage, attr_name) == expected_value


def test_scenario_model_validation():
    scenario = Scenario(
        stages=[
            {
                "name": "http_stage",
                "url": "https://api.example.com/test",
                "params": {"key": "value"},
                "headers": {"Authorization": "Bearer token"}
            },
            {"name": "no_http_stage"},
        ]
    )

    assert len(scenario.stages) == 2
    assert scenario.stages[0].url == "https://api.example.com/test"
    assert scenario.stages[1].url is None
