import pytest
import requests
import responses

from pytest_http.models import Scenario, Stage
from pytest_http.pytest_plugin import json_test_function


@pytest.mark.parametrize(
    "stage_config,expected_url,expected_assertions,description",
    [
        (
            {"name": "get_users", "request": {"url": "https://api.example.com/users"}},
            "https://api.example.com/users",
            {"url": "https://api.example.com/users"},
            "basic_url"
        ),
        (
            {
                "name": "get_user",
                "request": {"url": "https://api.example.com/users", "params": {"id": 1, "format": "json"}}
            },
            "https://api.example.com/users",
            {"url_contains": ["id=1", "format=json"]},
            "with_params"
        ),
        (
            {
                "name": "get_users",
                "request": {"url": "https://api.example.com/users", "headers": {"Authorization": "Bearer token123", "Accept": "application/json"}}
            },
            "https://api.example.com/users",
            {"headers": {"Authorization": "Bearer token123", "Accept": "application/json"}},
            "with_headers"
        ),
        (
            {
                "name": "get_user",
                "request": {"url": "https://api.example.com/user/1"},
                "response": {"save": {"vars": {"user_id": "json.id", "user_name": "json.name", "status": "status_code"}}}
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
                {"name": "get_users", "request": {"url": "https://api.example.com/users"}},
                {"name": "get_user_details", "request": {"url": "https://api.example.com/user/1"}}
            ],
            ["https://api.example.com/users", "https://api.example.com/user/1"],
            "multiple_http_stages"
        ),
        (
            [{"name": "no_http_stage", "request": {"url": "https://api.example.com/test"}}],
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

    test_data = {"stages": [{"name": "get_users", "request": {"url": "https://api.example.com/users"}}]}

    if expected_behavior == "should_pass":
        json_test_function(test_data)
        assert len(responses.calls) == 1
    else:
        with pytest.raises((requests.RequestException, ValueError)):
            json_test_function(test_data)


@pytest.mark.parametrize(
    "actual_status,expected_status,should_fail,expected_error",
    [
        (200, 200, False, None),
        (404, 200, True, "expected 200, got 404"),
        (201, 201, False, None),
        (500, 404, True, "expected 404, got 500"),
    ],
)
@responses.activate
def test_http_request_verification(actual_status, expected_status, should_fail, expected_error):
    responses.add(
        responses.GET,
        "https://api.example.com/test",
        json={"id": 1, "name": "test"},
        status=actual_status,
        headers={"Content-Type": "application/json"},
    )

    test_data = {"stages": [{"name": "test_stage", "request": {"url": "https://api.example.com/test"}, "response": {"verify": {"status": expected_status}}}]}

    if should_fail:
        with pytest.raises(pytest.fail.Exception) as exc_info:
            json_test_function(test_data)
        assert "Status code verification failed" in str(exc_info.value)
        assert expected_error in str(exc_info.value)
    else:
        json_test_function(test_data)
        assert len(responses.calls) == 1


@pytest.mark.parametrize(
    "verify_config",
    [
        None,
        {},
    ],
)
@responses.activate
def test_http_request_without_or_empty_verification(verify_config):
    responses.add(
        responses.GET,
        "https://api.example.com/test",
        json={"error": "Not found"},
        status=404,
        headers={"Content-Type": "application/json"},
    )

    test_data = {"stages": [{"name": "test_stage", "request": {"url": "https://api.example.com/test"}}]}
    if verify_config is not None:
        test_data["stages"][0]["response"] = {"verify": verify_config}
    
    json_test_function(test_data)


@pytest.mark.parametrize(
    "exception_type,exception_message,expected_error_text",
    [
        (requests.Timeout, "Request timed out", "HTTP request timed out"),
        (requests.ConnectionError, "Connection failed", "HTTP connection error"),
        (requests.RequestException, "General request error", "HTTP request failed"),
    ],
)
@responses.activate
def test_http_request_errors(exception_type, exception_message, expected_error_text):
    responses.add(responses.GET, "https://api.example.com/test", body=exception_type(exception_message))

    test_data = {"stages": [{"name": "test_stage", "request": {"url": "https://api.example.com/test"}, "response": {"verify": {"status": 200}}}]}

    with pytest.raises(pytest.fail.Exception) as exc_info:
        json_test_function(test_data)

    assert expected_error_text in str(exc_info.value)
    assert "test_stage" in str(exc_info.value)


@responses.activate
def test_multiple_stages_with_verification():
    responses.add(
        responses.GET,
        "https://api.example.com/users",
        json={"data": "test"},
        status=200,
        headers={"Content-Type": "application/json"},
    )
    responses.add(
        responses.GET,
        "https://api.example.com/nonexistent",
        json={"data": "test"},
        status=404,
        headers={"Content-Type": "application/json"},
    )

    test_data = {
        "stages": [
            {"name": "get_users", "request": {"url": "https://api.example.com/users"}, "response": {"verify": {"status": 200}}},
            {"name": "get_nonexistent", "request": {"url": "https://api.example.com/nonexistent"}, "response": {"verify": {"status": 404}}},
        ]
    }

    json_test_function(test_data)

    assert len(responses.calls) == 2


@pytest.mark.parametrize(
    "response_json,verification_json,should_fail,expected_error",
    [
        (
            {"users": [{"id": 1, "name": "John"}, {"id": 2, "name": "Jane"}], "total": 2},
            {"json.total": 2, "json.users[0].name": "John", "json.users[1].id": 2},
            False,
            None,
        ),
        (
            {"users": [{"id": 1, "name": "John"}], "total": 1},
            {"json.total": 2},
            True,
            "expected 2, got 1",
        ),
        (
            {
                "string_field": "test",
                "number_field": 42,
                "boolean_field": True,
                "null_field": None,
                "array_field": [1, 2, 3],
                "object_field": {"nested": "value"},
            },
            {
                "json.string_field": "test",
                "json.number_field": 42,
                "json.boolean_field": True,
                "json.null_field": None,
                "json.array_field": [1, 2, 3],
                "json.object_field": {"nested": "value"},
            },
            False,
            None,
        ),
        (
            {"test": "data"},
            {"json.nonexistent.field": "value"},
            True,
            "expected value, got None",
        ),
    ],
)
@responses.activate
def test_json_verification(response_json, verification_json, should_fail, expected_error):
    responses.add(
        responses.GET,
        "https://api.example.com/test",
        json=response_json,
        status=200,
        headers={"Content-Type": "application/json"},
    )

    test_data = {"stages": [{"name": "test_stage", "request": {"url": "https://api.example.com/test"}, "response": {"verify": {"json": verification_json}}}]}

    if should_fail:
        with pytest.raises(pytest.fail.Exception) as exc_info:
            json_test_function(test_data)
        assert "JSON verification failed" in str(exc_info.value)
        assert expected_error in str(exc_info.value)
    else:
        json_test_function(test_data)
        assert len(responses.calls) == 1


@pytest.mark.parametrize(
    "response_json,response_headers,verify_config",
    [
        (
            {"users": [{"id": 1, "name": "John"}], "total": 1},
            {"Content-Type": "application/json"},
            {"status": 200, "json": {"json.total": 1, "json.users[0].name": "John"}},
        ),
        (
            {"test": "data"},
            {"Content-Type": "application/json", "X-Custom-Header": "custom-value"},
            {
                "json": {
                    'headers."Content-Type"': "application/json",
                    'headers."X-Custom-Header"': "custom-value",
                    "status_code": 200,
                }
            },
        ),
        (
            {"test": "data"},
            {"Content-Type": "application/json"},
            {"status": 200},
        ),
    ],
)
@responses.activate
def test_json_verification_integration(response_json, response_headers, verify_config):
    responses.add(
        responses.GET,
        "https://api.example.com/test",
        json=response_json,
        status=200,
        headers=response_headers,
    )

    test_data = {"stages": [{"name": "test_stage", "request": {"url": "https://api.example.com/test"}, "response": verify_config}]}

    json_test_function(test_data)

    assert len(responses.calls) == 1


@responses.activate
def test_json_verification_multiple_stages():
    responses.add(
        responses.GET,
        "https://api.example.com/users",
        json={"users": [{"id": 1, "name": "John"}], "total": 1},
        status=200,
        headers={"Content-Type": "application/json"},
    )
    responses.add(
        responses.GET,
        "https://api.example.com/posts",
        json={"posts": [{"id": 1, "title": "Test Post"}], "count": 1},
        status=200,
        headers={"Content-Type": "application/json"},
    )

    test_data = {
        "stages": [
            {
                "name": "get_users",
                "request": {"url": "https://api.example.com/users"},
                "response": {"verify": {"json": {"json.total": 1, "json.users[0].name": "John"}}}
            },
            {
                "name": "get_posts",
                "request": {"url": "https://api.example.com/posts"},
                "response": {"verify": {"json": {"json.count": 1, "json.posts[0].title": "Test Post"}}}
            },
        ]
    }

    json_test_function(test_data)

    assert len(responses.calls) == 2


@pytest.mark.parametrize(
    "stage_data,expected_attrs",
    [
        (
            {
                "name": "test_stage",
                "request": {
                    "url": "https://api.example.com/test",
                    "params": {"key": "value"},
                    "headers": {"Content-Type": "application/json"}
                }
            },
            {
                "name": "test_stage",
                "request": {
                    "url": "https://api.example.com/test",
                    "params": {"key": "value"},
                    "headers": {"Content-Type": "application/json"}
                }
            }
        ),
        (
            {"name": "no_http_stage", "request": {"url": "https://api.example.com/test"}},
            {"name": "no_http_stage", "request": {"url": "https://api.example.com/test"}}
        ),
    ],
)
def test_stage_model_validation(stage_data, expected_attrs):
    stage = Stage(**stage_data)

    for attr_name, expected_value in expected_attrs.items():
        if attr_name == "name":
            assert getattr(stage, attr_name) == expected_value
        elif attr_name == "request":
            assert stage.request.url == expected_value["url"]
            assert stage.request.params == expected_value["params"]
            assert stage.request.headers == expected_value["headers"]


def test_scenario_model_validation():
    scenario = Scenario(
        stages=[
            {
                "name": "http_stage",
                "request": {
                    "url": "https://api.example.com/test",
                    "params": {"key": "value"},
                    "headers": {"Authorization": "Bearer token"}
                }
            },
            {"name": "no_http_stage", "request": {}},
        ]
    )

    assert len(scenario.stages) == 2
    assert scenario.stages[0].request.url == "https://api.example.com/test"
    assert scenario.stages[1].request.url is None

