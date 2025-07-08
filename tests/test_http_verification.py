import pytest
import requests
import responses

from pytest_http.pytest_plugin import json_test_function


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

    test_data = {"stages": [{"name": "test_stage", "data": {}, "url": "https://api.example.com/test", "verify": {"status": expected_status}}]}

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

    test_data = {"stages": [{"name": "test_stage", "data": {}, "url": "https://api.example.com/test"}]}
    if verify_config is not None:
        test_data["stages"][0]["verify"] = verify_config

    json_test_function(test_data)


@pytest.mark.parametrize(
    "exception_type,exception_message,expected_error_text",
    [
        (requests.Timeout, "Request timed out", "HTTP request timed out"),
        (requests.ConnectionError, "Connection failed", "HTTP connection error"),
        (requests.RequestException, "General request error", "HTTP request failed"),
    ],
)
def test_http_request_errors(exception_type, exception_message, expected_error_text):
    responses.add(responses.GET, "https://api.example.com/test", body=exception_type(exception_message))

    test_data = {"stages": [{"name": "test_stage", "data": {}, "url": "https://api.example.com/test", "verify": {"status": 200}}]}

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
            {"name": "get_users", "data": {}, "url": "https://api.example.com/users", "verify": {"status": 200}},
            {"name": "get_nonexistent", "data": {}, "url": "https://api.example.com/nonexistent", "verify": {"status": 404}},
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

    test_data = {"stages": [{"name": "test_stage", "url": "https://api.example.com/test", "verify": {"json": verification_json}}]}

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

    test_data = {"stages": [{"name": "test_stage", "url": "https://api.example.com/test", "verify": verify_config}]}

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
            {"name": "get_users", "url": "https://api.example.com/users", "verify": {"json": {"json.total": 1, "json.users[0].name": "John"}}},
            {"name": "get_posts", "url": "https://api.example.com/posts", "verify": {"json": {"json.count": 1, "json.posts[0].title": "Test Post"}}},
        ]
    }

    json_test_function(test_data)

    assert len(responses.calls) == 2
