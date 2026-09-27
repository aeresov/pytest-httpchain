"""Unit tests for the ClientConfig model (a scenario's ``client`` block)."""

import re

import pytest
from pydantic import ValidationError

from pytest_httpchain.models.entities import ClientConfig, Scenario
from tests.unit.models.helpers import assert_error_types


@pytest.mark.parametrize(
    ("attr", "default"),
    [
        ("base_url", None),
        ("headers", {}),
        ("params", {}),
        # The plugin's defaults before the block existed: a 30 s timeout,
        # redirects followed, HTTP/2 offered.
        ("timeout", 30.0),
        ("follow_redirects", True),
        ("max_redirects", 20),
        ("proxy", None),
        ("http2", True),
        # No connection limit: httpx's 100 silently capped parallel stages.
        ("max_connections", None),
        ("max_keepalive_connections", 20),
    ],
)
def test_field_default(attr, default):
    assert getattr(ClientConfig(), attr) == default


def test_scenario_without_client_block_gets_the_defaults():
    assert Scenario().client == ClientConfig()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("base_url", "https://api.example.com/v1", id="base_url"),
        pytest.param("base_url", "https://api.example.com/v1/", id="base_url-trailing-slash"),
        pytest.param("base_url", "{{ api_root }}", id="base_url-template"),
        pytest.param("headers", {"Accept": "application/json", "X-Env": "{{ env_name }}"}, id="headers"),
        pytest.param("params", {"api_key": "{{ key }}", "v": 2}, id="params"),
        pytest.param("timeout", 5.5, id="timeout"),
        pytest.param("timeout", "{{ timeout }}", id="timeout-template"),
        pytest.param("follow_redirects", False, id="follow_redirects"),
        pytest.param("follow_redirects", "{{ follow }}", id="follow_redirects-template"),
        pytest.param("max_redirects", 3, id="max_redirects"),
        pytest.param("proxy", "http://proxy.example.com:3128", id="proxy-http"),
        pytest.param("proxy", "socks5h://user:pass@proxy.example.com:1080", id="proxy-socks"),
        pytest.param("proxy", "{{ proxy_url }}", id="proxy-template"),
        pytest.param("http2", False, id="http2"),
        pytest.param("max_connections", 200, id="max_connections"),
        pytest.param("max_connections", "{{ conns }}", id="max_connections-template"),
        # A literal null is "no limit"; only a template rendering to null is
        # refused (the carrier's rendered-to-None guard).
        pytest.param("max_keepalive_connections", None, id="max_keepalive_connections-unbounded"),
    ],
)
def test_field_round_trip(field, value):
    assert getattr(ClientConfig.model_validate({field: value}), field) == value


@pytest.mark.parametrize(
    ("data", "message"),
    [
        # httpx appends the stage's path to the base URL's raw path, query and
        # all, so a query would swallow every stage's path.
        pytest.param({"base_url": "https://api.example.com/v1?key=1"}, "base_url must not have a query or fragment", id="base_url-query"),
        pytest.param({"base_url": "https://api.example.com/v1#top"}, "base_url must not have a query or fragment", id="base_url-fragment"),
        pytest.param({"base_url": "/v1"}, "relative URL without a base", id="base_url-relative"),
        pytest.param({"base_url": "ftp://files.example.com/"}, "URL scheme should be 'http' or 'https'", id="base_url-scheme"),
        # The same as-written checks as a request URL.
        pytest.param({"base_url": "https://api.example.com/v1 "}, "URL must not start or end with a space", id="base_url-trailing-space"),
        pytest.param({"proxy": "ftp://proxy.example.com"}, "Proxy URL must start with 'http://', 'https://', 'socks5://' or 'socks5h://'", id="proxy-scheme"),
        pytest.param({"proxy": "proxy.example.com:3128"}, "Proxy URL must start with", id="proxy-no-scheme"),
        pytest.param({"proxy": "http://"}, "Proxy URL must start with", id="proxy-no-host"),
        pytest.param({"proxy": " http://proxy.example.com:3128"}, "URL must not start or end with a space", id="proxy-leading-space"),
        pytest.param({"proxy": "http://pro\txy.example.com:3128"}, "Invalid URL: Invalid non-printable ASCII character", id="proxy-control-character"),
    ],
)
def test_invalid_url_rejected(data, message):
    with pytest.raises(ValidationError, match=message):
        ClientConfig.model_validate(data)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        # A '/' in the password ends httpx's authority early: 'Invalid port:
        # 'pa'' would quote the password's start, so the reason goes too.
        pytest.param({"proxy": "http://user:pa/s3cret@proxy.internal:3128"}, "Invalid URL (httpx's reason is not shown", id="proxy-unparseable"),
        pytest.param({"proxy": "ftp://user:s3cret@proxy.internal:21"}, "Proxy URL must start with 'http://'", id="proxy-scheme"),
        pytest.param({"proxy": "http://user:s3cret@proxy.internal:3128 "}, "URL must not start or end with a space", id="proxy-trailing-space"),
        pytest.param({"base_url": "https://user:pa/s3cret@api.example.com/v1"}, "Input should be a valid URL", id="base_url-unparseable"),
        pytest.param({"base_url": "https://user:s3cret@api.example.com/v1?x=1"}, "base_url must not have a query or fragment", id="base_url-query"),
        pytest.param({"base_url": "https://user:s3cret@api.example.com\\@v1/"}, "URL must not contain '\\' before its path", id="base_url-backslash"),
        # hide_input_in_errors: pydantic's input_value would quote a header too.
        pytest.param({"headers": {"Authorization": ["Bearer s3cret"]}}, "Input should be a valid string", id="header"),
    ],
)
def test_refused_value_is_not_quoted(data, message):
    """A proxy's userinfo is credentials, and so is a base URL's or a header,
    usually rendered from a secret at scenario initialization, whose failure
    message is every later stage's skip reason too: the message says what is
    wrong without the value, in the URL check's words, the template branch's
    and pydantic's ``input_value``."""
    with pytest.raises(ValidationError, match=re.escape(message)) as exc_info:
        ClientConfig.model_validate(data)
    assert "s3cret" not in str(exc_info.value)
    assert all("s3cret" not in error["msg"] for error in exc_info.value.errors())


@pytest.mark.parametrize("field", ["timeout", "max_redirects", "max_connections", "max_keepalive_connections"])
@pytest.mark.parametrize("value", [0, -1])
def test_numbers_must_be_positive(field, value):
    with pytest.raises(ValidationError) as exc_info:
        ClientConfig.model_validate({field: value})
    assert_error_types(exc_info, "greater_than", at=field)


@pytest.mark.parametrize("field", ["timeout", "follow_redirects", "max_redirects", "http2"])
def test_null_is_not_a_setting_of_a_required_field(field):
    """Only base_url, proxy and the pool limits mean something by null."""
    with pytest.raises(ValidationError):
        ClientConfig.model_validate({field: None})
