"""Unit tests for Request model."""

from http import HTTPMethod

import pydantic_core
import pytest
from pydantic import AnyHttpUrl, AnyUrl, HttpUrl, ValidationError

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
        "url",
        [
            pytest.param("https://example.com/search?q=test&page=1", id="plain"),
            # pydantic's HttpUrl handed the URL back WHATWG-normalized, and
            # that was sent: an encoded path-traversal probe reached /ok.
            pytest.param("http://example.com/static/%2e%2e/ok", id="encoded-dot-segment"),
            pytest.param("http://example.com/a\\b", id="backslash-in-path"),
            pytest.param("http://example.com", id="no-slash-appended"),
            pytest.param("HTTP://Example.COM/Path", id="case"),
            # HttpUrl refused anything over 2083 characters; httpx has no cap.
            pytest.param("http://example.com/" + "a" * 3000, id="over-2083-chars"),
            # Only C0 controls and space are what WHATWG strips from the ends;
            # other Unicode whitespace is part of the URL to both parsers.
            pytest.param("http://example.com/ok?q=東京\u3000", id="trailing-ideographic-space"),
            pytest.param("http://example.com/ok?name=José\u00a0", id="trailing-nbsp"),
            pytest.param("http://example.com/x\u2028", id="trailing-line-separator"),
            # httpx sends the host as written too, and the resolver reads these
            # as the address WHATWG does.
            pytest.param("http://127.1/", id="ipv4-shorthand"),
            pytest.param("http://[0:0::1]:8080/", id="ipv6-uncompressed"),
            # Only a percent-encoded host is refused: userinfo escapes are sent
            # as they are by both parsers.
            pytest.param("http://u%40x:p%5C@example.com/", id="percent-encoded-userinfo"),
            # Not a template (nothing inside), so the engine sends it as is.
            pytest.param("http://example.com/a{{}}b", id="empty-braces"),
        ],
    )
    def test_concrete_url_kept_as_written(self, url):
        assert Request(url=url).url == url

    @pytest.mark.parametrize("url_type", [HttpUrl, AnyHttpUrl, AnyUrl, pydantic_core.Url])
    def test_pydantic_url_object_taken_as_its_string(self, url_type):
        """A single-expression template keeps its value's type, so
        ``"{{ api_url }}"`` can render to a pydantic URL (a pydantic-settings
        field). The ``HttpUrl`` field took it; the str field must too."""
        assert Request(url=url_type("http://example.com/ok")).url == "http://example.com/ok"

    def test_pydantic_url_object_with_other_scheme_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            Request(url=AnyUrl("ftp://example.com/ok"))
        assert_error_types(exc_info, "url_scheme", at="url")

    @pytest.mark.parametrize("url", ["{{ base_url }}/api/users", "https://example.com/users/{{ user_id }}"])
    def test_template_url_kept_as_str(self, url):
        assert Request(url=url).url == url

    @pytest.mark.parametrize("url", ["http://example.com/a{{ }}b", "{{ base_url }}/a{{ }}b"])
    def test_empty_template_rejected(self, url):
        """The engine renders any URL with a ``{{ }}`` in it, and an empty one
        fails that render. A literal URL around it passed the URL check and
        failed only at runtime; it is a template, and refused as one."""
        with pytest.raises(ValidationError, match="Template expression cannot be empty"):
            Request(url=url)

    @pytest.mark.parametrize(
        "url",
        [
            pytest.param("/users/1", id="path-absolute"),
            pytest.param("users/1?page=2", id="path-relative"),
            pytest.param("?page=2", id="query-only"),
            # Anything without a scheme is a relative reference: this was
            # refused as not a URL, and is a path under client.base_url now.
            pytest.param("not-a-url", id="bare-word"),
            # As written, as an absolute URL is: nothing normalizes these.
            pytest.param("/static/%2e%2e/ok", id="encoded-dot-segment"),
            pytest.param("/a\\b", id="backslash"),
            pytest.param("1a:b", id="colon-after-a-non-scheme"),
        ],
    )
    def test_relative_url_kept_as_written(self, url):
        """A relative URL is completed by the scenario's client.base_url; the
        model cannot see whether there is one (HTTPCHAIN034 and the request
        builder report it when there is not)."""
        assert Request(url=url).url == url

    @pytest.mark.parametrize(
        ("url", "message"),
        [
            pytest.param(" /users/1", "URL must not start or end with a space or control character", id="leading-space"),
            pytest.param("/users/1\n", "URL must not start or end with a space or control character", id="trailing-newline"),
            pytest.param("/a\tb", "Invalid URL: Invalid non-printable ASCII character", id="tab"),
            # httpx keeps only the path of a network-path reference, dropping the host.
            pytest.param("//other.example/x", "URL must not start with '//' without a scheme", id="network-path"),
            # httpx reads the ':' as an empty scheme and drops it.
            pytest.param(":8080/x", "Relative URL must not start with ':'", id="leading-colon"),
        ],
    )
    def test_relative_url_httpx_would_not_send_as_written_rejected(self, url, message):
        with pytest.raises(ValidationError, match=message):
            Request(url=url)

    @pytest.mark.parametrize(
        ("url", "error_type"),
        [
            ("", "url_parsing"),
            ("ftp://example.com/", "url_scheme"),
            # A scheme makes it absolute, so it is not taken for a relative path.
            ("localhost:8080/x", "url_scheme"),
            ("http://", "url_parsing"),
            # The WHATWG parser still judges host and port.
            ("http://exa mple.com/", "url_parsing"),
            ("http://example.com:99999/", "url_parsing"),
        ],
    )
    def test_invalid_url_rejected(self, url, error_type):
        with pytest.raises(ValidationError) as exc_info:
            Request(url=url)
        assert_error_types(exc_info, error_type, at="url")

    @pytest.mark.parametrize(
        ("url", "message"),
        [
            # WHATWG repaired these, and the repaired URL was sent. Sent as
            # written they are not what they look like, so they are refused.
            pytest.param("http:/example.com/x", "URL must start with 'http://' or 'https://' and a host", id="missing-slash"),
            pytest.param("http:///example.com/x", "URL must start with 'http://' or 'https://' and a host", id="extra-slash"),
            pytest.param(" http://example.com/x", "URL must not start or end with a space or control character", id="leading-space"),
            pytest.param("http://example.com/x ", "URL must not start or end with a space or control character", id="trailing-space"),
            pytest.param("http://example.com/x\x01", "URL must not start or end with a space or control character", id="trailing-control"),
            pytest.param("http://example.com/a\nb", "Invalid URL: Invalid non-printable ASCII character", id="newline"),
            pytest.param("http://example.com/a\tb", "Invalid URL: Invalid non-printable ASCII character", id="tab"),
            # WHATWG's IDNA mapping folds this to example.com; httpx's refuses it.
            pytest.param("http://ＥＸＡＭＰＬＥ.com/", "Invalid URL: Invalid IDNA hostname", id="fullwidth-host"),
            # WHATWG ends the authority at a backslash, httpx does not: the
            # host and port checked were not the ones connected to.
            pytest.param("http://a\\b/x", "URL must not contain '\\\\' before its path", id="backslash-in-host"),
            pytest.param("http://127.0.0.1:1\\@localhost:9/x", "URL must not contain '\\\\' before its path", id="backslash-before-at"),
            # WHATWG decodes the host, httpx sends it undecoded.
            pytest.param("http://ex%61mple.com/", "URL host must not be percent-encoded", id="percent-encoded-host"),
        ],
    )
    def test_url_whatwg_would_repair_rejected(self, url, message):
        with pytest.raises(ValidationError, match=message):
            Request(url=url)


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


def test_schema_offers_no_timeout_or_redirect_default():
    """Not set, these take client.timeout and client.follow_redirects, which
    the model defaults only stand in for. An editor inserting a schema default
    would declare it, and a declared value overrides the client's."""
    properties = Request.model_json_schema()["properties"]
    assert [name for name in ("timeout", "allow_redirects") if "default" in properties[name]] == []
