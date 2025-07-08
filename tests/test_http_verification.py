from http import HTTPStatus

import pytest
import requests
import responses

from pytest_http.pytest_plugin import json_test_function


@responses.activate
def test_http_request_verification_success():
    """Test that HTTP request verification passes when status codes match."""
    responses.add(
        responses.GET,
        "https://api.example.com/test",
        json={"id": 1, "name": "test"},
        status=200,
        headers={"Content-Type": "application/json"},
    )

    test_data = {"stages": [{"name": "test_stage", "data": {}, "url": "https://api.example.com/test", "verify": {"status": 200}}]}

    json_test_function(test_data)

    assert len(responses.calls) == 1


@responses.activate
def test_http_request_verification_failure():
    """Test that HTTP request verification fails when status codes don't match."""
    responses.add(
        responses.GET,
        "https://api.example.com/test",
        json={"error": "Not found"},
        status=404,
        headers={"Content-Type": "application/json"},
    )

    test_data = {"stages": [{"name": "test_stage", "data": {}, "url": "https://api.example.com/test", "verify": {"status": 200}}]}

    with pytest.raises(pytest.fail.Exception) as exc_info:
        json_test_function(test_data)

    assert "Status code verification failed" in str(exc_info.value)
    assert "expected 200, got 404" in str(exc_info.value)


@responses.activate
def test_http_request_verification_with_different_status_codes():
    """Test verification with various HTTP status codes."""
    test_cases = [
        (200, HTTPStatus.OK),
        (201, HTTPStatus.CREATED),
        (404, HTTPStatus.NOT_FOUND),
        (500, HTTPStatus.INTERNAL_SERVER_ERROR),
    ]

    for actual_status, expected_status in test_cases:
        responses.reset()
        responses.add(
            responses.GET,
            "https://api.example.com/test",
            json={"test": "data"},
            status=actual_status,
            headers={"Content-Type": "application/json"},
        )

        test_data = {"stages": [{"name": "test_stage", "data": {}, "url": "https://api.example.com/test", "verify": {"status": expected_status.value}}]}

        json_test_function(test_data)


@responses.activate
def test_http_request_without_verification():
    """Test that HTTP requests work normally without verification."""
    responses.add(
        responses.GET,
        "https://api.example.com/test",
        json={"error": "Not found"},
        status=404,
        headers={"Content-Type": "application/json"},
    )

    test_data = {"stages": [{"name": "test_stage", "data": {}, "url": "https://api.example.com/test"}]}

    json_test_function(test_data)


@responses.activate
def test_http_request_with_empty_verification():
    """Test that HTTP requests work with empty verification object."""
    responses.add(
        responses.GET,
        "https://api.example.com/test",
        json={"error": "Not found"},
        status=404,
        headers={"Content-Type": "application/json"},
    )

    test_data = {"stages": [{"name": "test_stage", "data": {}, "url": "https://api.example.com/test", "verify": {}}]}

    json_test_function(test_data)


def test_http_request_timeout_error():
    """Test that HTTP request timeout errors are handled properly."""
    responses.add(responses.GET, "https://api.example.com/test", body=requests.Timeout("Request timed out"))

    test_data = {"stages": [{"name": "test_stage", "data": {}, "url": "https://api.example.com/test", "verify": {"status": 200}}]}

    with pytest.raises(pytest.fail.Exception) as exc_info:
        json_test_function(test_data)

    assert "HTTP request timed out" in str(exc_info.value)
    assert "test_stage" in str(exc_info.value)


def test_http_request_connection_error():
    """Test that HTTP connection errors are handled properly."""
    responses.add(responses.GET, "https://api.example.com/test", body=requests.ConnectionError("Connection failed"))

    test_data = {"stages": [{"name": "test_stage", "data": {}, "url": "https://api.example.com/test", "verify": {"status": 200}}]}

    with pytest.raises(pytest.fail.Exception) as exc_info:
        json_test_function(test_data)

    assert "HTTP connection error" in str(exc_info.value)
    assert "test_stage" in str(exc_info.value)


def test_http_request_general_error():
    """Test that general HTTP request errors are handled properly."""
    responses.add(responses.GET, "https://api.example.com/test", body=requests.RequestException("General request error"))

    test_data = {"stages": [{"name": "test_stage", "data": {}, "url": "https://api.example.com/test", "verify": {"status": 200}}]}

    with pytest.raises(pytest.fail.Exception) as exc_info:
        json_test_function(test_data)

    assert "HTTP request failed" in str(exc_info.value)
    assert "test_stage" in str(exc_info.value)


@responses.activate
def test_multiple_stages_with_verification():
    """Test multiple stages with different verification requirements."""
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


@responses.activate
def test_json_verification_success():
    responses.add(
        responses.GET,
        "https://api.example.com/users",
        json={"users": [{"id": 1, "name": "John"}, {"id": 2, "name": "Jane"}], "total": 2},
        status=200,
        headers={"Content-Type": "application/json"},
    )

    test_data = {
        "stages": [{"name": "test_stage", "url": "https://api.example.com/users", "verify": {"json": {"json.total": 2, "json.users[0].name": "John", "json.users[1].id": 2}}}]
    }

    json_test_function(test_data)

    assert len(responses.calls) == 1


@responses.activate
def test_json_verification_failure():
    responses.add(
        responses.GET,
        "https://api.example.com/users",
        json={"users": [{"id": 1, "name": "John"}], "total": 1},
        status=200,
        headers={"Content-Type": "application/json"},
    )

    test_data = {"stages": [{"name": "test_stage", "url": "https://api.example.com/users", "verify": {"json": {"json.total": 2}}}]}

    with pytest.raises(pytest.fail.Exception) as exc_info:
        json_test_function(test_data)

    assert "JSON verification failed" in str(exc_info.value)
    assert "expected 2, got 1" in str(exc_info.value)


@responses.activate
def test_json_verification_with_different_data_types():
    responses.add(
        responses.GET,
        "https://api.example.com/test",
        json={"string_field": "test", "number_field": 42, "boolean_field": True, "null_field": None, "array_field": [1, 2, 3], "object_field": {"nested": "value"}},
        status=200,
        headers={"Content-Type": "application/json"},
    )

    test_data = {
        "stages": [
            {
                "name": "test_stage",
                "url": "https://api.example.com/test",
                "verify": {
                    "json": {
                        "json.string_field": "test",
                        "json.number_field": 42,
                        "json.boolean_field": True,
                        "json.null_field": None,
                        "json.array_field": [1, 2, 3],
                        "json.object_field": {"nested": "value"},
                    }
                },
            }
        ]
    }

    json_test_function(test_data)

    assert len(responses.calls) == 1


@responses.activate
def test_json_verification_with_status_check():
    responses.add(
        responses.GET,
        "https://api.example.com/users",
        json={"users": [{"id": 1, "name": "John"}], "total": 1},
        status=200,
        headers={"Content-Type": "application/json"},
    )

    test_data = {"stages": [{"name": "test_stage", "url": "https://api.example.com/users", "verify": {"status": 200, "json": {"json.total": 1, "json.users[0].name": "John"}}}]}

    json_test_function(test_data)

    assert len(responses.calls) == 1


@responses.activate
def test_json_verification_with_response_headers():
    responses.add(
        responses.GET,
        "https://api.example.com/test",
        json={"test": "data"},
        status=200,
        headers={"Content-Type": "application/json", "X-Custom-Header": "custom-value"},
    )

    test_data = {
        "stages": [
            {
                "name": "test_stage",
                "url": "https://api.example.com/test",
                "verify": {"json": {'headers."Content-Type"': "application/json", 'headers."X-Custom-Header"': "custom-value", "status_code": 200}},
            }
        ]
    }

    json_test_function(test_data)

    assert len(responses.calls) == 1


@responses.activate
def test_json_verification_error_handling():
    responses.add(
        responses.GET,
        "https://api.example.com/test",
        json={"test": "data"},
        status=200,
        headers={"Content-Type": "application/json"},
    )

    test_data = {"stages": [{"name": "test_stage", "url": "https://api.example.com/test", "verify": {"json": {"json.nonexistent.field": "value"}}}]}

    with pytest.raises(pytest.fail.Exception) as exc_info:
        json_test_function(test_data)

    assert "JSON verification failed" in str(exc_info.value)
    assert "expected value, got None" in str(exc_info.value)


@responses.activate
def test_json_verification_without_json_field():
    responses.add(
        responses.GET,
        "https://api.example.com/test",
        json={"test": "data"},
        status=200,
        headers={"Content-Type": "application/json"},
    )

    test_data = {"stages": [{"name": "test_stage", "url": "https://api.example.com/test", "verify": {"status": 200}}]}

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
