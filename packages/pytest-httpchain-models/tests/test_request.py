"""Unit tests for Request model."""

from http import HTTPMethod

import pytest
from pydantic import ValidationError
from pytest_httpchain_models.entities import (
    JsonBody,
    Request,
    UserFunctionKwargs,
    UserFunctionName,
)


class TestRequestUrl:
    """Tests for Request.url field."""

    def test_url_valid_http(self):
        """Test valid HTTP URL."""
        request = Request(url="http://example.com")
        assert str(request.url) == "http://example.com/"

    def test_url_valid_https(self):
        """Test valid HTTPS URL."""
        request = Request(url="https://api.example.com/v1/users")
        assert "api.example.com" in str(request.url)

    def test_url_with_query_params(self):
        """Test URL with query parameters."""
        request = Request(url="https://example.com/search?q=test&page=1")
        assert "q=test" in str(request.url)

    def test_url_with_template(self):
        """Test URL with template expression."""
        request = Request(url="{{ base_url }}/api/users")
        assert request.url == "{{ base_url }}/api/users"

    def test_url_with_partial_template(self):
        """Test URL with partial template."""
        request = Request(url="https://example.com/users/{{ user_id }}")
        assert request.url == "https://example.com/users/{{ user_id }}"

    def test_url_invalid_rejected(self):
        """Test that invalid URLs are rejected."""
        with pytest.raises(ValidationError):
            Request(url="not-a-url")


class TestRequestMethod:
    """Tests for Request.method field."""

    def test_method_default_get(self):
        """Test default method is GET."""
        request = Request(url="https://example.com")
        assert request.method == HTTPMethod.GET

    def test_method_post(self):
        """Test POST method."""
        request = Request(url="https://example.com", method=HTTPMethod.POST)
        assert request.method == HTTPMethod.POST

    def test_method_all_http_methods(self):
        """Test all standard HTTP methods."""
        methods = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
        for method in methods:
            request = Request(url="https://example.com", method=method)
            assert request.method == HTTPMethod(method)

    def test_method_with_template(self):
        """Test method with template expression."""
        request = Request(url="https://example.com", method="{{ http_method }}")
        assert request.method == "{{ http_method }}"

    def test_method_invalid_rejected(self):
        """Test that invalid method is rejected."""
        with pytest.raises(ValidationError):
            Request(url="https://example.com", method="INVALID")


class TestRequestParams:
    """Tests for Request.params field."""

    def test_params_default_empty(self):
        """Test default params is empty dict."""
        request = Request(url="https://example.com")
        assert request.params == {}

    def test_params_simple(self):
        """Test simple query params."""
        request = Request(
            url="https://example.com",
            params={"page": 1, "limit": 10},
        )
        assert request.params == {"page": 1, "limit": 10}

    def test_params_with_strings(self):
        """Test params with string values."""
        request = Request(
            url="https://example.com",
            params={"q": "search term", "sort": "asc"},
        )
        assert request.params["q"] == "search term"


class TestRequestHeaders:
    """Tests for Request.headers field."""

    def test_headers_default_empty(self):
        """Test default headers is empty dict."""
        request = Request(url="https://example.com")
        assert request.headers == {}

    def test_headers_simple(self):
        """Test simple headers."""
        request = Request(
            url="https://example.com",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer token123",
            },
        )
        assert request.headers["Content-Type"] == "application/json"

    def test_headers_custom(self):
        """Test custom headers."""
        request = Request(
            url="https://example.com",
            headers={"X-Custom-Header": "custom-value"},
        )
        assert request.headers["X-Custom-Header"] == "custom-value"


class TestRequestTimeout:
    """Tests for Request.timeout field."""

    def test_timeout_default(self):
        """Test default timeout is 30 seconds."""
        request = Request(url="https://example.com")
        assert request.timeout == 30.0

    def test_timeout_custom(self):
        """Test custom timeout."""
        request = Request(url="https://example.com", timeout=60.0)
        assert request.timeout == 60.0

    def test_timeout_with_template(self):
        """Test timeout with template expression."""
        request = Request(url="https://example.com", timeout="{{ timeout_value }}")
        assert request.timeout == "{{ timeout_value }}"

    def test_timeout_must_be_positive(self):
        """Test that timeout must be positive."""
        with pytest.raises(ValidationError):
            Request(url="https://example.com", timeout=0)
        with pytest.raises(ValidationError):
            Request(url="https://example.com", timeout=-1)


class TestRequestAllowRedirects:
    """Tests for Request.allow_redirects field."""

    def test_allow_redirects_default_true(self):
        """Test default allow_redirects is True."""
        request = Request(url="https://example.com")
        assert request.allow_redirects is True

    def test_allow_redirects_false(self):
        """Test allow_redirects set to False."""
        request = Request(url="https://example.com", allow_redirects=False)
        assert request.allow_redirects is False

    def test_allow_redirects_with_template(self):
        """Test allow_redirects with template expression."""
        request = Request(url="https://example.com", allow_redirects="{{ follow_redirects }}")
        assert request.allow_redirects == "{{ follow_redirects }}"


class TestRequestAuth:
    """Tests for Request.auth field."""

    def test_auth_default_none(self):
        """Test default auth is None."""
        request = Request(url="https://example.com")
        assert request.auth is None

    def test_auth_simple_function(self):
        """Test auth with simple function name."""
        request = Request(url="https://example.com", auth=UserFunctionName("auth:get_credentials"))
        assert isinstance(request.auth, UserFunctionName)

    def test_auth_with_kwargs(self):
        """Test auth with function kwargs."""
        request = Request(
            url="https://example.com",
            auth=UserFunctionKwargs(
                name=UserFunctionName("auth:oauth2"),
                kwargs={"client_id": "abc123"},
            ),
        )
        assert isinstance(request.auth, UserFunctionKwargs)
        assert request.auth.kwargs == {"client_id": "abc123"}

    def test_auth_with_template(self):
        """Test auth with template in function name."""
        request = Request(url="https://example.com", auth=UserFunctionName("auth:{{ auth_func }}"))
        assert isinstance(request.auth, UserFunctionName)


class TestRequestBody:
    """Tests for Request.body field."""

    def test_body_default_none(self):
        """Test default body is None."""
        request = Request(url="https://example.com")
        assert request.body is None

    def test_body_json(self):
        """Test body with JSON data."""
        request = Request(
            url="https://example.com",
            body=JsonBody(json={"key": "value"}),
        )
        assert isinstance(request.body, JsonBody)


class TestRequestComplete:
    """Tests for complete Request configurations."""

    def test_request_full_config(self):
        """Test Request with all fields."""
        request = Request(
            url="https://api.example.com/users",
            method=HTTPMethod.POST,
            params={"version": "v1"},
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer token",
            },
            body=JsonBody(json={"name": "Alice", "email": "alice@example.com"}),
            timeout=45.0,
            allow_redirects=False,
            auth=UserFunctionName("auth:api_key"),
        )
        assert str(request.url) == "https://api.example.com/users"
        assert request.method == HTTPMethod.POST
        assert request.timeout == 45.0
        assert request.allow_redirects is False
