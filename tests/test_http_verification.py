from http import HTTPStatus
from unittest.mock import Mock, patch

import pytest
import requests

from pytest_http.pytest_plugin import json_test_function


def test_http_request_verification_success(monkeypatch):
    """Test that HTTP request verification passes when status codes match."""
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.text = '{"id": 1, "name": "test"}'
    mock_response.json.return_value = {"id": 1, "name": "test"}

    with patch("requests.get", return_value=mock_response):
        test_data = {"stages": [{"name": "test_stage", "data": {}, "url": "https://api.example.com/test", "verify": {"status": 200}}]}

        # Should not raise any exception
        json_test_function(test_data)


def test_http_request_verification_failure(monkeypatch):
    """Test that HTTP request verification fails when status codes don't match."""
    mock_response = Mock()
    mock_response.status_code = 404
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.text = '{"error": "Not found"}'
    mock_response.json.return_value = {"error": "Not found"}

    with patch("requests.get", return_value=mock_response):
        test_data = {"stages": [{"name": "test_stage", "data": {}, "url": "https://api.example.com/test", "verify": {"status": 200}}]}

        with pytest.raises(pytest.fail.Exception) as exc_info:
            json_test_function(test_data)

        assert "Status code verification failed" in str(exc_info.value)
        assert "expected 200, got 404" in str(exc_info.value)


def test_http_request_verification_with_different_status_codes():
    """Test verification with various HTTP status codes."""
    test_cases = [(200, HTTPStatus.OK), (201, HTTPStatus.CREATED), (404, HTTPStatus.NOT_FOUND), (500, HTTPStatus.INTERNAL_SERVER_ERROR)]

    for actual_status, expected_status in test_cases:
        mock_response = Mock()
        mock_response.status_code = actual_status
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.text = '{"test": "data"}'
        mock_response.json.return_value = {"test": "data"}

        with patch("requests.get", return_value=mock_response):
            test_data = {"stages": [{"name": "test_stage", "data": {}, "url": "https://api.example.com/test", "verify": {"status": expected_status.value}}]}

            # Should not raise any exception
            json_test_function(test_data)


def test_http_request_without_verification():
    """Test that HTTP requests work normally without verification."""
    mock_response = Mock()
    mock_response.status_code = 404  # Any status code should work
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.text = '{"error": "Not found"}'
    mock_response.json.return_value = {"error": "Not found"}

    with patch("requests.get", return_value=mock_response):
        test_data = {
            "stages": [
                {
                    "name": "test_stage",
                    "data": {},
                    "url": "https://api.example.com/test",
                    # No verify field
                }
            ]
        }

        # Should not raise any exception
        json_test_function(test_data)


def test_http_request_with_empty_verification():
    """Test that HTTP requests work with empty verification object."""
    mock_response = Mock()
    mock_response.status_code = 404  # Any status code should work
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.text = '{"error": "Not found"}'
    mock_response.json.return_value = {"error": "Not found"}

    with patch("requests.get", return_value=mock_response):
        test_data = {
            "stages": [
                {
                    "name": "test_stage",
                    "data": {},
                    "url": "https://api.example.com/test",
                    "verify": {},  # Empty verification object
                }
            ]
        }

        # Should not raise any exception
        json_test_function(test_data)


def test_http_request_timeout_error():
    """Test that HTTP request timeout errors are handled properly."""
    with patch("requests.get", side_effect=requests.Timeout("Request timed out")):
        test_data = {"stages": [{"name": "test_stage", "data": {}, "url": "https://api.example.com/test", "verify": {"status": 200}}]}

        with pytest.raises(pytest.fail.Exception) as exc_info:
            json_test_function(test_data)

        assert "HTTP request timed out" in str(exc_info.value)
        assert "test_stage" in str(exc_info.value)


def test_http_request_connection_error():
    """Test that HTTP connection errors are handled properly."""
    with patch("requests.get", side_effect=requests.ConnectionError("Connection failed")):
        test_data = {"stages": [{"name": "test_stage", "data": {}, "url": "https://api.example.com/test", "verify": {"status": 200}}]}

        with pytest.raises(pytest.fail.Exception) as exc_info:
            json_test_function(test_data)

        assert "HTTP connection error" in str(exc_info.value)
        assert "test_stage" in str(exc_info.value)


def test_http_request_general_error():
    """Test that general HTTP request errors are handled properly."""
    with patch("requests.get", side_effect=requests.RequestException("General request error")):
        test_data = {"stages": [{"name": "test_stage", "data": {}, "url": "https://api.example.com/test", "verify": {"status": 200}}]}

        with pytest.raises(pytest.fail.Exception) as exc_info:
            json_test_function(test_data)

        assert "HTTP request failed" in str(exc_info.value)
        assert "test_stage" in str(exc_info.value)


def test_multiple_stages_with_verification():
    """Test multiple stages with different verification requirements."""

    def mock_get(url, **kwargs):
        mock_response = Mock()
        if "users" in url:
            mock_response.status_code = 200
        else:
            mock_response.status_code = 404
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.text = '{"data": "test"}'
        mock_response.json.return_value = {"data": "test"}
        return mock_response

    with patch("requests.get", side_effect=mock_get):
        test_data = {
            "stages": [
                {"name": "get_users", "data": {}, "url": "https://api.example.com/users", "verify": {"status": 200}},
                {"name": "get_nonexistent", "data": {}, "url": "https://api.example.com/nonexistent", "verify": {"status": 404}},
            ]
        }

        # Should not raise any exception
        json_test_function(test_data)
