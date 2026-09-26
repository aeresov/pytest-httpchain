"""Unit tests for Request model."""

from http import HTTPMethod

import pytest
from pydantic import ValidationError

from pytest_httpchain.models.entities import Request, UserFunctionKwargs, UserFunctionName
from tests.unit.models.helpers import assert_error_types, make_request


@pytest.mark.parametrize(
    ("attr", "default"),
    [
        ("method", HTTPMethod.GET),
        ("params", {}),
        ("headers", {}),
        ("body", None),
        ("timeout", 30.0),
        ("allow_redirects", True),
        ("auth", None),
    ],
)
def test_field_default(attr, default):
    assert getattr(make_request(), attr) == default


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("params", {"page": 1, "limit": 10}, id="params"),
        pytest.param("params", {"q": "search term", "sort": "asc"}, id="params-str"),
        pytest.param("headers", {"Content-Type": "application/json"}, id="headers"),
        pytest.param("headers", {"X-Custom-Header": "custom-value"}, id="headers-custom"),
        pytest.param("timeout", 60.0, id="timeout"),
        pytest.param("timeout", "{{ timeout_value }}", id="timeout-template"),
        pytest.param("allow_redirects", False, id="allow_redirects-false"),
        pytest.param("allow_redirects", "{{ follow_redirects }}", id="allow_redirects-template"),
    ],
)
def test_field_round_trip(field, value):
    assert getattr(make_request(**{field: value}), field) == value


class TestUrl:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("http://example.com", "http://example.com/"),
            ("https://api.example.com/v1/users", "https://api.example.com/v1/users"),
            ("https://example.com/search?q=test&page=1", "https://example.com/search?q=test&page=1"),
        ],
    )
    def test_concrete_url_normalized(self, url, expected):
        assert str(Request(url=url).url) == expected

    @pytest.mark.parametrize("url", ["{{ base_url }}/api/users", "https://example.com/users/{{ user_id }}"])
    def test_template_url_kept_as_str(self, url):
        assert Request(url=url).url == url

    def test_invalid_url_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            Request(url="not-a-url")
        assert_error_types(exc_info, "url_parsing", at="url")


class TestMethod:
    @pytest.mark.parametrize(
        "method",
        [
            "GET",
            "POST",
            "PUT",
            "DELETE",
            "PATCH",
            "HEAD",
            "OPTIONS",
            # Non-enum verbs are legal HTTP too: any RFC 9110 token (WebDAV
            # PROPFIND/REPORT, cache PURGE, vendor methods).
            "PROPFIND",
            "REPORT",
            "MKCALENDAR",
            "PURGE",
            "INVALID",
            pytest.param(HTTPMethod.POST, id="enum"),
            "{{ http_method }}",
        ],
    )
    def test_accepted_verbatim(self, method):
        assert make_request(method=method).method == method

    @pytest.mark.parametrize("method", ["FOO BAR", "GET/POST", "", "MÉTHODE"])
    def test_non_token_rejected(self, method):
        """Strings that are not RFC 9110 tokens (spaces, separators) are rejected."""
        with pytest.raises(ValidationError, match="Invalid HTTP method token"):
            make_request(method=method)


@pytest.mark.parametrize("timeout", [0, -1])
def test_timeout_must_be_positive(timeout):
    with pytest.raises(ValidationError) as exc_info:
        make_request(timeout=timeout)
    assert_error_types(exc_info, "greater_than", at="timeout")


@pytest.mark.parametrize(
    ("auth", "expected"),
    [
        pytest.param("auth:get_credentials", UserFunctionName("auth:get_credentials"), id="name"),
        pytest.param("auth:{{ auth_func }}", UserFunctionName("auth:{{ auth_func }}"), id="name-template"),
        pytest.param(
            {"name": "auth:oauth2", "kwargs": {"client_id": "abc123"}},
            UserFunctionKwargs(name=UserFunctionName("auth:oauth2"), kwargs={"client_id": "abc123"}),
            id="kwargs",
        ),
    ],
)
def test_auth_forms(auth, expected):
    """A bare name or a {name, kwargs} object (``auth`` is shared with Scenario via Authenticated)."""
    assert make_request(auth=auth).auth == expected
