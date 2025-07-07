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
