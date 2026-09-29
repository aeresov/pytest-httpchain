"""redaction: the one set of rules the report, the HAR export and the header
verification messages show credentials through."""

import httpx
import pytest

from pytest_httpchain.redaction import (
    DEFAULT_REDACT_HEADERS,
    DEFAULT_REDACT_QUERY_PARAMS,
    DEFAULT_REDACTION,
    NO_REDACTION,
    REDACTED,
    Redaction,
)

_EXPIRES = "Expires=Wed, 21 Oct 2026 07:28:00 GMT"


@pytest.mark.parametrize(
    ("redaction", "name", "value", "expected"),
    [
        pytest.param(DEFAULT_REDACTION, "Authorization", "Bearer abc", REDACTED, id="authorization-whole-value"),
        pytest.param(DEFAULT_REDACTION, "x-api-key", "k-123", REDACTED, id="api-key"),
        # Names match case-insensitively on both sides.
        pytest.param(DEFAULT_REDACTION, "AUTHORIZATION", "Basic dTpw", REDACTED, id="header-name-case"),
        pytest.param(Redaction(["X-TENANT-SECRET"]), "x-tenant-secret", "s", REDACTED, id="configured-name-case"),
        pytest.param(DEFAULT_REDACTION, "X-Request-Id", "12345", "12345", id="unlisted-header-untouched"),
        # An empty value hides nothing, and shows a template that rendered to "".
        pytest.param(DEFAULT_REDACTION, "Authorization", "", "", id="empty-value-shown"),
        pytest.param(DEFAULT_REDACTION, "Cookie", "a=1; b=2", "a=[REDACTED]; b=[REDACTED]", id="cookie-keeps-names"),
        # An empty cookie stays empty; a pair without "=" is a nameless cookie, all value.
        pytest.param(DEFAULT_REDACTION, "Cookie", "a=; flag", "a=; [REDACTED]", id="cookie-empty-and-nameless"),
        pytest.param(DEFAULT_REDACTION, "Set-Cookie", "sid=abc; Path=/; HttpOnly", "sid=[REDACTED]; Path=/; HttpOnly", id="set-cookie-keeps-attributes"),
        # Headers.get() comma-folds repeated lines; an Expires comma is no separator.
        pytest.param(
            DEFAULT_REDACTION,
            "set-cookie",
            f"a=1; Path=/, b=2; {_EXPIRES}, c=3",
            f"a=[REDACTED]; Path=/, b=[REDACTED]; {_EXPIRES}, c=[REDACTED]",
            id="set-cookie-folded",
        ),
        # A URL-bearing header has its query redacted: a redirect's Location.
        pytest.param(DEFAULT_REDACTION, "Location", "/cb?state=s&access_token=t", "/cb?state=s&access_token=[REDACTED]", id="location-query"),
        pytest.param(Redaction(["Location"]), "location", "/cb?state=s", REDACTED, id="location-listed-whole-value"),
        pytest.param(NO_REDACTION, "Authorization", "Bearer abc", "Bearer abc", id="disabled-authorization"),
        pytest.param(NO_REDACTION, "Cookie", "a=1", "a=1", id="disabled-cookie"),
        pytest.param(NO_REDACTION, "Set-Cookie", "sid=abc; Path=/", "sid=abc; Path=/", id="disabled-set-cookie"),
        pytest.param(NO_REDACTION, "Location", "/cb?access_token=t", "/cb?access_token=t", id="disabled-location"),
    ],
)
def test_header(redaction, name, value, expected):
    assert redaction.header(name, value) == expected


@pytest.mark.parametrize(
    ("redaction", "url", "expected"),
    [
        pytest.param(
            DEFAULT_REDACTION,
            "https://x.test/p?page=1&access_token=abc&q=a%20b+c",
            "https://x.test/p?page=1&access_token=[REDACTED]&q=a%20b+c",
            id="only-listed-values-rest-as-written",
        ),
        pytest.param(DEFAULT_REDACTION, "https://x.test/p?Token=a&TOKEN=b", "https://x.test/p?Token=[REDACTED]&TOKEN=[REDACTED]", id="key-case-and-repeats"),
        # Keys compare decoded: api%5Fkey is api_key.
        pytest.param(DEFAULT_REDACTION, "https://x.test/p?api%5Fkey=k", "https://x.test/p?api%5Fkey=[REDACTED]", id="encoded-key"),
        pytest.param(DEFAULT_REDACTION, "https://x.test/p?token=&token", "https://x.test/p?token=&token", id="empty-and-bare-shown"),
        # OAuth's implicit flow returns the token in the fragment.
        pytest.param(DEFAULT_REDACTION, "https://x.test/cb#id_token=t&state=s", "https://x.test/cb#id_token=[REDACTED]&state=s", id="fragment"),
        pytest.param(DEFAULT_REDACTION, "https://x.test/p#token", "https://x.test/p#token", id="plain-fragment"),
        # httpx sends userinfo as Authorization's Basic credentials.
        pytest.param(DEFAULT_REDACTION, "https://u:pw@x.test/p", "https://u:[REDACTED]@x.test/p", id="userinfo-password"),
        pytest.param(DEFAULT_REDACTION, "https://u:p@w@x.test/p", "https://u:[REDACTED]@x.test/p", id="userinfo-password-with-at"),
        # Without a password the user name is the credential (a token passed as
        # the user name), sent as Basic "<token>:".
        pytest.param(DEFAULT_REDACTION, "https://ghp_tok@x.test/p", "https://[REDACTED]@x.test/p", id="userinfo-without-password"),
        pytest.param(DEFAULT_REDACTION, "https://ghp_tok:@x.test/p", "https://[REDACTED]:@x.test/p", id="userinfo-empty-password"),
        pytest.param(DEFAULT_REDACTION, "https://:pw@x.test/p", "https://:[REDACTED]@x.test/p", id="userinfo-password-only"),
        pytest.param(DEFAULT_REDACTION, "https://@x.test/p", "https://@x.test/p", id="userinfo-empty"),
        pytest.param(Redaction(query_params=["token"]), "https://u:pw@x.test/p", "https://u:pw@x.test/p", id="userinfo-follows-authorization"),
        pytest.param(DEFAULT_REDACTION, "https://x.test/p", "https://x.test/p", id="no-query"),
        pytest.param(NO_REDACTION, "https://u:pw@x.test/p?token=t#id_token=i", "https://u:pw@x.test/p?token=t#id_token=i", id="disabled"),
    ],
)
def test_url(redaction, url, expected):
    assert redaction.url(url) == expected


def test_url_accepts_an_httpx_url():
    assert DEFAULT_REDACTION.url(httpx.URL("https://x.test/p", params={"token": "t"})) == "https://x.test/p?token=[REDACTED]"


@pytest.mark.parametrize(
    ("redaction", "message", "expected"),
    [
        # h11 refuses a value with a newline (a token read from a file), quoting its bytes.
        pytest.param(DEFAULT_REDACTION, "Illegal header value b'Bearer tok\\n'", "Illegal header value b'[REDACTED]'", id="bytes-repr"),
        pytest.param(DEFAULT_REDACTION, "bad value 'Bearer tok\\n'", "bad value '[REDACTED]'", id="text-repr"),
        # A quoted value is shown as the report shows it: a Cookie keeps its names.
        pytest.param(DEFAULT_REDACTION, "Illegal header value b'sid=c-tok\\r'", "Illegal header value b'sid=[REDACTED]'", id="cookie-names-kept"),
        pytest.param(DEFAULT_REDACTION, "Illegal header value b'/cb?token=l-tok\\n'", "Illegal header value b'/cb?token=[REDACTED]'", id="url-header-query"),
        # Unquoted, a value could be any part of the message (the "1" of 127.0.0.1).
        pytest.param(DEFAULT_REDACTION, "failed at 127.0.0.1", "failed at 127.0.0.1", id="unquoted-left-alone"),
        pytest.param(DEFAULT_REDACTION, "Illegal header value b'trace\\n'", "Illegal header value b'trace\\n'", id="unlisted-header"),
        pytest.param(NO_REDACTION, "Illegal header value b'Bearer tok\\n'", "Illegal header value b'Bearer tok\\n'", id="disabled"),
    ],
)
def test_error_text(redaction, message, expected):
    headers = httpx.Headers(
        [
            ("authorization", "Bearer tok\n"),
            ("cookie", "sid=c-tok\r"),
            ("referer", "/cb?token=l-tok\n"),
            ("x-api-key", "1"),
            ("x-trace", "trace\n"),
        ]
    )
    assert redaction.error_text(message, headers) == expected


@pytest.mark.parametrize(
    ("redaction", "name", "value", "expected"),
    [
        pytest.param(DEFAULT_REDACTION, "Client_Secret", "s", REDACTED, id="listed-any-case"),
        pytest.param(DEFAULT_REDACTION, "page", "1", "1", id="unlisted"),
        pytest.param(DEFAULT_REDACTION, "token", "", "", id="empty"),
        pytest.param(NO_REDACTION, "token", "t", "t", id="disabled"),
    ],
)
def test_query_param(redaction, name, value, expected):
    assert redaction.query_param(name, value) == expected


def test_header_items_keep_repeated_lines_apart():
    """Each Set-Cookie wire line is redacted on its own, never comma-folded."""
    headers = httpx.Headers([("set-cookie", "a=1; Path=/"), ("set-cookie", f"b=2; {_EXPIRES}"), ("x-id", "7")])

    assert DEFAULT_REDACTION.header_items(headers) == [
        ("set-cookie", "a=[REDACTED]; Path=/"),
        ("set-cookie", f"b=[REDACTED]; {_EXPIRES}"),
        ("x-id", "7"),
    ]


def test_defaults_are_the_documented_lists():
    """The ini defaults, which DEFAULT_REDACTION applies too."""
    assert DEFAULT_REDACT_HEADERS == ("Authorization", "Proxy-Authorization", "Cookie", "Set-Cookie", "X-API-Key", "API-Key", "X-Auth-Token")
    assert DEFAULT_REDACT_QUERY_PARAMS == ("access_token", "refresh_token", "id_token", "api_key", "apikey", "client_secret", "password", "token")
    assert DEFAULT_REDACTION.headers == {name.lower() for name in DEFAULT_REDACT_HEADERS}
    assert DEFAULT_REDACTION.query_params == set(DEFAULT_REDACT_QUERY_PARAMS)
