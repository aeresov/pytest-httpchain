"""Unit tests for importers/builder.py: recorded requests written as a
scenario in the dialect, with no secret in it."""

import base64
import json
from typing import Any
from urllib.parse import unquote

import pytest
from pydantic import ValidationError

from pytest_httpchain.importers import Base64Data, FileData, ImportSourceError, Part, PartsData, RecordedRequest, TextData, build_scenario, scenario_text, validate_text
from pytest_httpchain.importers.builder import multipart_parts
from pytest_httpchain.redaction import Redaction
from pytest_httpchain.templates import TemplatesError
from tests.unit.helpers import TOO_DEEP_TO_WALK
from tests.unit.importers.helpers import sent_requests


def _valid(scenario: dict[str, Any]) -> bool:
    """Whether the file an import writes for ``scenario`` passes the
    validator, read from disk as ``validate`` reads it."""
    return validate_text(scenario_text(scenario)).valid


def _scenario(*requests: RecordedRequest) -> dict[str, Any]:
    result = build_scenario(list(requests), description="test")
    # Whatever it builds passes the validator.
    assert _valid(result.scenario)
    return result.scenario


def _request(**fields: Any) -> dict[str, Any]:
    """The one stage's request of a scenario built from one request."""
    [stage] = _scenario(RecordedRequest(**{"method": "GET", "url": "https://x.test/a", **fields}))["stages"]
    return stage["request"]


def _basic(credentials: str) -> str:
    return "Basic " + base64.b64encode(credentials.encode()).decode()


def test_no_requests_is_an_error():
    with pytest.raises(ImportSourceError, match="^There are no requests to import$"):
        build_scenario([], description="none")


# --- URLs ---


def test_one_origin_is_the_base_url():
    scenario = _scenario(RecordedRequest("GET", "https://api.test:8443/v1/users?page=2"), RecordedRequest("POST", "https://api.test:8443/v1/users"))
    assert scenario["client"]["base_url"] == "https://api.test:8443"
    assert [stage["request"]["url"] for stage in scenario["stages"]] == ["/v1/users", "/v1/users"]


def test_several_origins_keep_absolute_urls():
    scenario = _scenario(RecordedRequest("GET", "https://a.test/x"), RecordedRequest("GET", "https://b.test/y"))
    assert "client" not in scenario or "base_url" not in scenario["client"]
    assert [stage["request"]["url"] for stage in scenario["stages"]] == ["https://a.test/x", "https://b.test/y"]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        pytest.param("https://x.test", {"url": "/"}, id="no-path"),
        # A relative `//a` would name a host: that one URL stays absolute.
        pytest.param("https://x.test//a", {"url": "https://x.test//a"}, id="double-slash-path"),
        # The query as params, a repeated name's values a list, order kept.
        pytest.param("https://x.test/a?b=2&b=3&a=1&e=&f&q=a+b%26", {"url": "/a", "params": {"b": ["2", "3"], "a": "1", "e": "", "f": "", "q": "a b&"}}, id="params"),
        # What params would not send as it was stays in the URL as written:
        # escapes that are not UTF-8, a repeated name apart from its first
        # (params sends a name's values together), a name the file's loader
        # would read as a reference.
        pytest.param("https://x.test/a?q=%E9", {"url": "/a?q=%E9"}, id="query-kept"),
        pytest.param("https://x.test/a?a=1&b=2&a=3", {"url": "/a?a=1&b=2&a=3"}, id="query-kept-interleaved"),
        pytest.param("https://x.test/a?$ref=x&a=1", {"url": "/a?$ref=x&a=1"}, id="query-kept-reference-key"),
        pytest.param("https://x.test/a?$include=x.json", {"url": "/a?$include=x.json"}, id="query-kept-include"),
        # ... its secrets still placeholders, which fail the stage unset.
        pytest.param("https://x.test/a?access_token=S3CRET&q=caf%E9&{{x}}=1", {"url": "/a?access_token={{ quote(access_token) }}&q=caf%E9&\\{{x}}=1"}, id="query-kept-secret"),
        pytest.param("https://x.test/{{x}}?q={{y}}", {"url": "/\\{{x}}", "params": {"q": "\\{{y}}"}}, id="template-syntax"),
        pytest.param("https://x.test/a#frag", {"url": "/a"}, id="fragment"),
    ],
)
def test_url(url, expected):
    assert _request(url=url) == expected


# --- headers ---


def test_transport_headers_are_left_out():
    """What the client writes itself: HTTP/2's pseudo-headers, the body's
    length, the connection's headers, Accept-Encoding, and a Host naming
    the URL's own host (with its default port or without)."""
    headers = [
        (":authority", "x.test"),
        ("Host", "x.test:443"),
        ("Content-Length", "0"),
        ("Connection", "keep-alive"),
        ("Keep-Alive", "300"),
        ("Transfer-Encoding", "chunked"),
        ("TE", "trailers"),
        ("Upgrade", "h2c"),
        ("Accept-Encoding", "gzip"),
        ("Accept", "*/*"),
    ]
    assert _request(headers=headers) == {"url": "/a", "headers": {"Accept": "*/*"}}


def test_host_of_another_host_is_kept():
    assert _request(headers=[("Host", "vhost.test")]) == {"url": "/a", "headers": {"Host": "vhost.test"}}


def test_proxy_authorization_is_left_out_with_a_note():
    result = build_scenario([RecordedRequest("GET", "https://x.test/a", headers=[("Proxy-Authorization", "Basic eA==")])], description="t")
    assert "headers" not in result.scenario["stages"][0]["request"]
    assert result.notes == ["Stage 'get_a': its Proxy-Authorization header is left out: a proxy's credentials go in client.proxy's URL."]


def test_repeated_header_is_one_field():
    assert _request(headers=[("Accept", "a/b"), ("accept", "c/d"), ("X-T", "{{ x }}")]) == {"url": "/a", "headers": {"Accept": "a/b, c/d", "X-T": "\\{{ x }}"}}


def test_headers_every_request_sends_are_the_clients():
    """With several requests, a header each sends with the same value is
    sent by the client; a Content-Type belongs to its body, and stays."""
    shared = [("User-Agent", "UA"), ("Content-Type", "text/plain")]
    scenario = _scenario(
        RecordedRequest("POST", "https://x.test/a", headers=[*shared, ("X-A", "1")], body=TextData("a")),
        RecordedRequest("POST", "https://x.test/b", headers=[("user-agent", "UA"), ("Content-Type", "text/plain")], body=TextData("b")),
    )
    assert scenario["client"]["headers"] == {"User-Agent": "UA"}
    assert [stage["request"]["headers"] for stage in scenario["stages"]] == [{"Content-Type": "text/plain", "X-A": "1"}, {"Content-Type": "text/plain"}]


def test_one_request_keeps_its_headers():
    scenario = _scenario(RecordedRequest("GET", "https://x.test/a", headers=[("User-Agent", "UA")]))
    assert "headers" not in scenario["client"]
    assert scenario["stages"][0]["request"]["headers"] == {"User-Agent": "UA"}


# --- secrets ---

# Every secret below holds "s3cret", which the scenario must not.
SECRETS = [
    pytest.param({"headers": [("Authorization", "Bearer s3cret-tok")]}, {"auth": {"bearer": "{{ api_token }}"}}, [("API_TOKEN", "the bearer token")], id="bearer-header"),
    pytest.param({"bearer": "s3cret-tok"}, {"auth": {"bearer": "{{ api_token }}"}}, [("API_TOKEN", "the bearer token")], id="oauth2-bearer"),
    pytest.param(
        {"headers": [("Authorization", _basic("ann:s3cret-pw"))]},
        {"auth": {"basic": {"username": "ann", "password": "{{ api_password }}"}}},
        [("API_PASSWORD", "the password of user 'ann'")],
        id="basic-header",
    ),
    pytest.param(
        {"user": ("ann", "s3cret-pw")}, {"auth": {"basic": {"username": "ann", "password": "{{ api_password }}"}}}, [("API_PASSWORD", "the password of user 'ann'")], id="-u"
    ),
    pytest.param(
        {"user": ("ann", "s3cret-pw"), "digest": True},
        {"auth": {"digest": {"username": "ann", "password": "{{ api_password }}"}}},
        [("API_PASSWORD", "the password of user 'ann'")],
        id="--digest",
    ),
    # curl asks for a password it was not given.
    pytest.param(
        {"user": ("ann", None)}, {"auth": {"basic": {"username": "ann", "password": "{{ api_password }}"}}}, [("API_PASSWORD", "the password of user 'ann'")], id="-u-no-password"
    ),
    # Without a password the user name is the credential (-u key:).
    pytest.param(
        {"user": ("s3cret-key", "")},
        {"auth": {"basic": {"username": "{{ api_user }}", "password": ""}}},
        [("API_USER", "the user name of credentials without a password")],
        id="-u-key",
    ),
    pytest.param(
        {"url": "https://ann:s3cret%40pw@x.test/a"},
        {"auth": {"basic": {"username": "ann", "password": "{{ api_password }}"}}},
        [("API_PASSWORD", "the password of user 'ann'")],
        id="url-userinfo",
    ),
    # A URL's user name without a password is sent with an empty one, as an
    # API key is (`https://<key>@host`): the user name is the credential.
    pytest.param(
        {"url": "https://s3cret-key@x.test/a"},
        {"auth": {"basic": {"username": "{{ api_user }}", "password": ""}}},
        [("API_USER", "the user name of credentials without a password")],
        id="url-userinfo-key",
    ),
    # Any other scheme, and Basic credentials that are not user:password.
    pytest.param(
        {"headers": [("Authorization", "Token s3cret")]}, {"headers": {"Authorization": "{{ authorization }}"}}, [("AUTHORIZATION", "the Authorization header")], id="other-scheme"
    ),
    pytest.param(
        {"headers": [("Authorization", "Basic s3cret!")]},
        {"headers": {"Authorization": "{{ authorization }}"}},
        [("AUTHORIZATION", "the Authorization header")],
        id="basic-not-base64",
    ),
    # The header wins over -u, as curl sends it in place of its own.
    pytest.param(
        {"headers": [("Authorization", "Bearer s3cret")], "user": ("ann", "s3cret-pw")},
        {"auth": {"bearer": "{{ api_token }}"}},
        [("API_TOKEN", "the bearer token")],
        id="header-over-user",
    ),
    # The cookies are one placeholder: a Cookie header given apart from them
    # too, whose cookies come first.
    pytest.param(
        {"headers": [("Cookie", "sid=s3cret-1; s3cret-anon")], "cookies": [("more", "s3cret-2"), ("sid", "s3cret-1")]},
        {"headers": {"Cookie": "{{ cookie }}"}},
        [("COOKIE", "the cookies more, sid, a nameless cookie")],
        id="cookies",
    ),
    # The headers and query parameters reports redact; an empty one hides
    # nothing. A query holding one stays in the URL, its placeholder a
    # quote() that fails the stage unset, which a params value (sent empty)
    # would not.
    pytest.param(
        {"headers": [("X-API-Key", "s3cret-k"), ("X-Auth-Token", "s3cret-t"), ("API-Key", "")], "url": "https://x.test/a?access_token=s3cret-a&page=1"},
        {
            "url": "/a?access_token={{ quote(access_token) }}&page=1",
            "headers": {"X-API-Key": "{{ x_api_key }}", "X-Auth-Token": "{{ x_auth_token }}", "API-Key": ""},
        },
        [("ACCESS_TOKEN", "query parameter 'access_token'"), ("X_API_KEY", "the X-API-Key header"), ("X_AUTH_TOKEN", "the X-Auth-Token header")],
        id="redacted-names",
    ),
    # The query parameters' names in a form or JSON body, at any depth. A
    # form holding one is its text, as a query is.
    pytest.param(
        {"headers": [("Content-Type", "application/x-www-form-urlencoded")], "body": TextData("grant_type=password&password=s3cret-pw&client_secret=s3cret-s")},
        {
            "headers": {"Content-Type": "application/x-www-form-urlencoded"},
            "body": {"text": "grant_type=password&password={{ quote(password) }}&client_secret={{ quote(client_secret) }}"},
        },
        [("PASSWORD", "form field 'password'"), ("CLIENT_SECRET", "form field 'client_secret'")],
        id="form-fields",
    ),
    pytest.param(
        {"headers": [("Content-Type", "application/json")], "body": TextData('{"user": {"password": "s3cret-pw", "token": 5}, "items": [{"api_key": "s3cret-k"}]}')},
        {"body": {"json": {"user": {"password": "{{ password }}", "token": "{{ json_loads(token) }}"}, "items": [{"api_key": "{{ api_key }}"}]}}},
        [("PASSWORD", "JSON body member 'password'"), ("TOKEN", "JSON body member 'token'"), ("API_KEY", "JSON body member 'api_key'")],
        id="json-members",
    ),
    # A number is read as JSON, to be sent as one; the strings and numbers
    # of a list under the name are each a secret; an object under it is
    # judged by its own members (a JSON Schema's "password": {...} holds
    # none); true, false, null and "" hide nothing.
    pytest.param(
        {
            "headers": [("Content-Type", "application/json")],
            "body": TextData(
                '{"password": 834211, "token": ["s3cret-1", 7.5, ["s3cret-2"]], "api_key": {"type": "string", "default": ""},'
                ' "properties": {"password": {"minLength": 8}}, "access_token": null, "refresh_token": true, "id_token": ""}'
            ),
        },
        {
            "body": {
                "json": {
                    "password": "{{ json_loads(password) }}",
                    "token": ["{{ token }}", "{{ json_loads(token_2) }}", ["{{ token_3 }}"]],
                    "api_key": {"type": "string", "default": ""},
                    "properties": {"password": {"minLength": 8}},
                    "access_token": None,
                    "refresh_token": True,
                    "id_token": "",
                }
            }
        },
        [("PASSWORD", "JSON body member 'password'"), ("TOKEN", "JSON body member 'token'"), ("TOKEN_2", "JSON body member 'token'"), ("TOKEN_3", "JSON body member 'token'")],
        id="json-member-numbers-and-lists",
    ),
    # A URL-valued header carrying a credential the redaction rules hide in
    # a URL (as a report shows a Referer) is a secret as a whole.
    pytest.param(
        {"headers": [("Referer", "https://app.test/cb?code=1&access_token=s3cret-a"), ("Location", "https://app.test/#token=s3cret-t"), ("Content-Location", "/a?page=1")]},
        {"headers": {"Referer": "{{ referer }}", "Location": "{{ location }}", "Content-Location": "/a?page=1"}},
        [("REFERER", "the Referer header, whose URL holds a credential"), ("LOCATION", "the Location header, whose URL holds a credential")],
        id="url-headers",
    ),
    pytest.param(
        {"body": PartsData((Part("password", value="s3cret-pw"), Part("title", value="t")))},
        {"body": {"multipart": {"fields": {"password": "{{ password }}", "title": "t"}}}},
        [("PASSWORD", "multipart field 'password'")],
        id="multipart-fields",
    ),
    # A part with a type or a filename is a file object, its text a
    # placeholder all the same.
    pytest.param(
        {"body": PartsData((Part("password", value="s3cret-pw", content_type="text/plain"), Part("token", content="s3cret-t", filename="t.txt"), Part("doc", path="token")))},
        {
            "body": {
                "multipart": {
                    "files": {
                        "password": {"content": "{{ password }}", "filename": "", "content_type": "text/plain"},
                        "token": {"content": "{{ token }}", "filename": "t.txt"},
                        "doc": "token",
                    }
                }
            }
        },
        [("PASSWORD", "multipart part 'password'"), ("TOKEN", "multipart part 'token'")],
        id="multipart-file-parts",
    ),
    # A part recorded as bytes that are no text ("s3cretAA" decodes to
    # them) is a secret by its name all the same, the placeholder holding it
    # base64-encoded.
    pytest.param(
        {"body": PartsData((Part("password", base64="s3cretAA", filename="p.bin"), Part("avatar", base64="AAE=", filename="a.bin")))},
        {"body": {"multipart": {"files": {"password": {"base64": "{{ password }}", "filename": "p.bin"}, "avatar": {"base64": "AAE=", "filename": "a.bin"}}}}},
        [("PASSWORD", "multipart part 'password', base64-encoded")],
        id="multipart-base64-part",
    ),
]


@pytest.mark.parametrize(("fields", "expected", "placeholders"), SECRETS)
def test_secrets_become_placeholders(fields, expected, placeholders):
    """Each secret is a ``vars`` entry reading an environment variable, and
    the request refers to it; the value itself is nowhere."""
    result = build_scenario([RecordedRequest(**{"method": "GET", "url": "https://x.test/a", **fields})], description="t")
    assert _valid(result.scenario)
    stage_request = result.scenario["stages"][0]["request"]
    assert {key: value for key, value in stage_request.items() if key != "method" and (key, value) != ("url", "/a")} == expected
    assert [(entry.env, entry.what) for entry in result.placeholders] == placeholders
    assert result.scenario["substitutions"] == [{"vars": {entry.var: f"{{{{ env('{entry.env}') }}}}" for entry in result.placeholders}}]
    assert "s3cret" not in json.dumps(result.scenario)


UNSET = [
    pytest.param(RecordedRequest("GET", "https://x.test/a?access_token=s3cret&page=1"), id="query-parameter"),
    pytest.param(RecordedRequest("GET", "https://x.test/a?password=s3cret&a=1&a=2&b=1"), id="query-parameter-kept-in-url"),
    pytest.param(
        RecordedRequest("POST", "https://x.test/a", headers=[("Content-Type", "application/x-www-form-urlencoded")], body=TextData("a=1&password=s3cret")),
        id="form-field",
    ),
    pytest.param(RecordedRequest("GET", "https://x.test/a", headers=[("X-API-Key", "s3cret")]), id="header"),
    pytest.param(RecordedRequest("GET", "https://x.test/a", cookies=[("sid", "s3cret")]), id="cookie"),
    pytest.param(RecordedRequest("GET", "https://x.test/a", bearer="s3cret"), id="bearer"),
    pytest.param(RecordedRequest("GET", "https://x.test/a", user=("ann", "s3cret")), id="password"),
    pytest.param(RecordedRequest("POST", "https://x.test/a", body=PartsData((Part("password", value="s3cret"),))), id="multipart-field"),
    pytest.param(RecordedRequest("POST", "https://x.test/a", body=PartsData((Part("password", value="s3cret", content_type="text/plain"),))), id="multipart-typed-field"),
    pytest.param(RecordedRequest("POST", "https://x.test/a", headers=[("Content-Type", "application/json")], body=TextData('{"token": 5}')), id="json-number"),
]


@pytest.mark.parametrize("recorded", UNSET)
def test_an_unset_placeholder_sends_nothing(recorded, monkeypatch):
    """A placeholder whose environment variable is unset fails the request
    rather than send it without its secret: a params value or form field
    would be sent empty, so a query or form holding one is its text, the
    placeholder a quote() that null fails."""
    result = build_scenario([recorded], description="t")
    [placeholder] = result.placeholders
    monkeypatch.delenv(placeholder.env, raising=False)
    with pytest.raises((TemplatesError, ValidationError)):
        sent_requests(result.scenario)
    # Set, it is sent (a JSON number's is read as JSON).
    monkeypatch.setenv(placeholder.env, "5")
    assert len(sent_requests(result.scenario)) == 1


def test_url_userinfo_without_either_holds_no_secret():
    """``https://@host`` sends Basic credentials with both empty: nothing to hide."""
    result = build_scenario([RecordedRequest("GET", "https://@x.test/a")], description="t")
    assert result.scenario["stages"][0]["request"]["auth"] == {"basic": {"username": "", "password": ""}}
    assert result.placeholders == []


@pytest.mark.parametrize(
    "recorded",
    [
        pytest.param(RecordedRequest("GET", "https://x.test/a?q={{x&token=s3cret"), id="query"),
        pytest.param(RecordedRequest("GET", "https://x.test/a{{b?token=s3cret"), id="path-before-query"),
        pytest.param(
            RecordedRequest("POST", "https://x.test/a", headers=[("Content-Type", "application/x-www-form-urlencoded")], body=TextData("q={{x&password=s3cret")),
            id="form-text",
        ),
        pytest.param(
            RecordedRequest("POST", "https://x.test/a", headers=[("Content-Type", "application/json")], body=TextData('{"$ref": "x", "note": "{{", "password": "s3cret"}')),
            id="json-text",
        ),
        # A `}}` after the placeholder closes no escape before it: an
        # object's end, or text of the JSON's own.
        pytest.param(
            RecordedRequest(
                "POST", "https://x.test/a", headers=[("Content-Type", "application/json")], body=TextData('{"$ref": "#/x", "note": "{{", "creds": {"password": "s3cret"}}')
            ),
            id="json-text-nested",
        ),
        pytest.param(
            RecordedRequest("POST", "https://x.test/a", headers=[("Content-Type", "application/json")], body=TextData('{"$ref":"x","note":"{{","password":"s3cret","z":"}}"}')),
            id="json-text-closing-braces-after",
        ),
        # A repeated header's values are one field, a placeholder among them.
        pytest.param(
            RecordedRequest("GET", "https://x.test/a", headers=[("Referer", "https://r.test/{{x"), ("Referer", "https://r.test/?access_token=s3cret")]),
            id="repeated-header",
        ),
    ],
)
def test_recorded_braces_leave_a_placeholder_after_them_a_template(recorded, monkeypatch):
    """A recorded ``{{`` that no ``}}`` follows, escaped, would cover the
    text after it up to a placeholder's own ``}}``, which would then be
    sent as its text and the secret never: the request sent is the one
    recorded, the secret filled in."""
    result = build_scenario([recorded], description="t")
    [placeholder] = result.placeholders
    monkeypatch.setenv(placeholder.env, "s3cret")
    [sent] = sent_requests(result.scenario)
    assert "s3cret" not in json.dumps(result.scenario)
    sent_text = unquote(str(sent.url)) + sent.content.decode() + ", ".join(sent.headers.get_list("referer"))
    # The placeholder rendered, not sent as its text; the secret sent.
    assert not any(written in sent_text for written in ("quote(", "json_dumps(", "{{ referer }}"))
    assert "s3cret" in sent_text
    if isinstance(recorded.body, TextData) and "json" in recorded.headers[0][1]:
        assert json.loads(sent.content) == json.loads(recorded.body.text)


def test_recorded_text_keeps_its_whitespace():
    """Text a placeholder does not follow is escaped as it is: a form body
    that is whitespace and braces is sent exactly."""
    form = [("Content-Type", "application/x-www-form-urlencoded")]
    for text in (" {{", "{{ ", "\t{{"):
        result = build_scenario([RecordedRequest("POST", "https://x.test/a", headers=form, body=TextData(text))], description="t")
        [sent] = sent_requests(result.scenario)
        assert sent.content == text.encode()


def test_secret_base64_part_is_sent_as_recorded(monkeypatch):
    """The placeholder for a secret part recorded as bytes holds them
    base64-encoded: set to the recorded base64, the part sends the bytes it
    was recorded with; unset, the request fails rather than go without it."""
    secret = b"\xffs3cret-pw"
    recorded = RecordedRequest("POST", "https://x.test/a", body=PartsData((Part("password", base64=base64.b64encode(secret).decode(), filename="p.bin"),)))
    result = build_scenario([recorded], description="t")
    [placeholder] = result.placeholders
    assert base64.b64encode(secret).decode() not in json.dumps(result.scenario)
    monkeypatch.delenv(placeholder.env, raising=False)
    with pytest.raises((TemplatesError, ValidationError)):
        sent_requests(result.scenario)
    monkeypatch.setenv(placeholder.env, base64.b64encode(secret).decode())
    [request] = sent_requests(result.scenario)
    assert secret in request.content


def test_multipart_recorded_as_bytes_is_taken_apart():
    """A multipart body with a part that is no text is recorded whole as
    base64: its fields are judged as a text body's are, a secret one a
    placeholder, and the part that is no text kept as base64."""
    content = (
        b'--b0undary\r\nContent-Disposition: form-data; name="password"\r\n\r\ns3cret\r\n'
        b'--b0undary\r\nContent-Disposition: form-data; name="avatar"; filename="a.bin"\r\nContent-Type: application/octet-stream\r\n\r\n\x00\xff\xfe\r\n'
        b"--b0undary--\r\n"
    )
    recorded = RecordedRequest(
        "POST", "https://x.test/a", headers=[("Content-Type", "multipart/form-data; boundary=b0undary")], body=Base64Data(base64.b64encode(content).decode())
    )
    result = build_scenario([recorded], description="t")
    assert result.scenario["stages"][0]["request"]["body"] == {
        "multipart": {
            "fields": {"password": "{{ password }}"},
            "files": {"avatar": {"base64": base64.b64encode(b"\x00\xff\xfe").decode(), "filename": "a.bin", "content_type": "application/octet-stream"}},
        }
    }
    # Base64 that is no multipart body, or no base64, is sent as it is.
    for data in (base64.b64encode(b"not multipart").decode(), "not base64!"):
        body = build_scenario([RecordedRequest("POST", "https://x.test/a", headers=[("Content-Type", "multipart/form-data; boundary=b")], body=Base64Data(data))], description="t")
        assert body.scenario["stages"][0]["request"]["body"] == {"base64": data}


def test_redacted_url_header_is_a_placeholder():
    """A report shows a Referer's credential as ``[REDACTED]``, which stands
    for the value it hid: a placeholder, never sent as written."""
    result = build_scenario([RecordedRequest("GET", "https://x.test/a", headers=[("Referer", "https://app.test/cb?token=[REDACTED]")])], description="t")
    assert result.scenario["stages"][0]["request"]["headers"] == {"Referer": "{{ referer }}"}


def test_secrets_in_text_render_as_recorded(monkeypatch):
    """Where recorded text is kept as written (a query params would not send
    as it was, a form or JSON body a mapping would not), its secrets are
    placeholders inside it, which render, once filled in, to the request
    recorded: a query parameter and form field URL-encoded, a JSON member as
    JSON."""
    monkeypatch.setenv("ACCESS_TOKEN", "t/ok en")
    monkeypatch.setenv("PASSWORD", 'p"w')
    monkeypatch.setenv("TOKEN", "12")
    recorded = [
        RecordedRequest("GET", "https://x.test/a?access_token=t%2Fok+en&q=caf%E9"),
        RecordedRequest("POST", "https://x.test/f", headers=[("Content-Type", "application/x-www-form-urlencoded")], body=TextData("a=1&password=p%22w&a=2")),
        RecordedRequest(
            "POST", "https://x.test/j", headers=[("Content-Type", "application/json")], body=TextData('{"$ref": "#/x", "password": "p\\"w", "token": 12, "nul": "\\u0000"}')
        ),
    ]
    result = build_scenario(recorded, description="t")
    assert "p%22w" not in json.dumps(result.scenario)
    query, form, body = sent_requests(result.scenario)
    assert query.url.query == b"access_token=t%2Fok%20en&q=caf%E9"
    assert form.content == b"a=1&password=p%22w&a=2"
    # (A string holding a NUL, which the placeholders' own markers are made of.)
    assert json.loads(body.content) == {"$ref": "#/x", "password": 'p"w', "token": 12, "nul": "\x00"}


def test_one_secret_is_one_placeholder():
    """The same value under the same name is one placeholder, whatever stages
    send it; another value under that name is another."""
    result = build_scenario(
        [
            RecordedRequest("GET", "https://x.test/a", cookies=[("sid", "1")]),
            RecordedRequest("GET", "https://x.test/b", cookies=[("sid", "1")]),
            RecordedRequest("GET", "https://x.test/c", cookies=[("sid", "2")]),
        ],
        description="t",
    )
    assert [(entry.var, entry.stages) for entry in result.placeholders] == [("cookie", ["get_a", "get_b"]), ("cookie_2", ["get_c"])]


def test_redacted_values_are_placeholders_of_their_own():
    """A redacted source hides every secret as `[REDACTED]`: one placeholder
    per name, never one for all of them."""
    result = build_scenario([RecordedRequest("GET", "https://x.test/a?token=[REDACTED]", headers=[("X-API-Key", "[REDACTED]"), ("Authorization", "[REDACTED]")])], description="t")
    assert [entry.var for entry in result.placeholders] == ["token", "x_api_key", "authorization"]


@pytest.mark.parametrize(
    ("name", "var"),
    [
        pytest.param("X-Api-Key", "x_api_key", id="words"),
        # Never a keyword, a built-in or `response`, which a template would
        # read as the engine's own.
        pytest.param("type", "type_value", id="soft-keyword"),
        pytest.param("class", "class_value", id="keyword"),
        pytest.param("str", "str_value", id="built-in"),
        pytest.param("response", "response_value", id="response"),
        pytest.param("2fa", "v_2fa", id="leading-digit"),
        pytest.param("---", "secret", id="no-word"),
    ],
)
def test_placeholder_names(name, var):
    """A placeholder is named after what it stands for, as a variable a
    template can read; its environment variable is that name uppercased."""
    result = build_scenario([RecordedRequest("GET", f"https://x.test/a?{name}=v")], description="t", redaction=Redaction(query_params=[name]))
    assert [(entry.var, entry.env) for entry in result.placeholders] == [(var, var.upper())]


# --- reference keys ---


def test_json_body_with_a_reference_key_is_text():
    """A member named ``$ref`` (a JSON Schema, a Mongo DBRef) would be read
    as a reference by the file's loader: the body is the text it was, its
    Content-Type kept, and a note says why."""
    text = '{"properties": {"a": {"$ref": "#/definitions/a"}}, "definitions": {"a": {"type": "string"}}, "x": "{{y}}"}'
    result = build_scenario([RecordedRequest("POST", "https://x.test/schemas", headers=[("Content-Type", "application/json")], body=TextData(text))], description="t")
    request = result.scenario["stages"][0]["request"]
    assert request["body"] == {"text": text.replace("{{y}}", "\\{{y}}")}
    assert request["headers"] == {"Content-Type": "application/json"}
    assert result.notes == [
        "Stage 'post_schemas': its JSON body is written as text: a json body could not hold its member '$ref', which the scenario file would read as a reference"
    ]
    # The file, read back as collection reads it, sends what was recorded.
    [sent] = sent_requests(result.scenario)
    assert sent.content == text.encode()


@pytest.mark.parametrize("key", ["$ref", "$include", "$merge"])
def test_reference_keys_never_reach_a_mapping(key, tmp_path):
    """Wherever a recorded name becomes a key of the scenario, one the
    file's loader would resolve is kept out: the file passes ``validate``,
    and sends what was recorded (a multipart part of that name, which no
    form can carry, is left out, with a note)."""
    requests = [
        RecordedRequest("GET", f"https://x.test/a?{key}=x.json&b=1", headers=[(key, "v")]),
        RecordedRequest("POST", "https://x.test/f", headers=[("Content-Type", "application/x-www-form-urlencoded")], body=TextData(f"{key}=x.json")),
        RecordedRequest("POST", "https://x.test/j", headers=[("Content-Type", "application/json")], body=TextData(json.dumps({"a": [{key: "x.json"}]}))),
        RecordedRequest("POST", "https://x.test/m", body=PartsData((Part(key, value="x.json"), Part("b", value="1")))),
    ]
    result = build_scenario(requests, description="t")
    assert _valid(result.scenario)
    query, form, body, multipart = sent_requests(result.scenario, tmp_path)
    assert query.url.query == f"{key}=x.json&b=1".encode()
    assert query.headers[key] == "v"
    assert form.content == f"{key}=x.json".encode()
    assert json.loads(body.content) == {"a": [{key: "x.json"}]}
    assert f'name="{key}"'.encode() not in multipart.content
    assert result.notes[-1] == f"Stage 'post_m': left out the multipart part named {key!r}, which the scenario file would read as a reference ($ref, $include, $merge)"


def test_header_named_as_a_reference_key_changes_case():
    """A header's name says nothing by its case: ``$ref`` is written ``$Ref``."""
    assert _request(headers=[("$ref", "1"), ("$merge", "2")]) == {"url": "/a", "headers": {"$Ref": "1", "$Merge": "2"}}


# --- stage names ---


def test_stage_names_are_unique():
    scenario = _scenario(
        RecordedRequest("GET", "https://x.test/users/42"),
        RecordedRequest("GET", "https://x.test/users/42"),
        RecordedRequest("GET", "https://x.test/users/42"),
        RecordedRequest("GET", "https://x.test/"),
        RecordedRequest("PROPFIND", "https://x.test/caf%C3%A9/a.b-c"),
        RecordedRequest("GET", "https://x.test/" + "segment/" * 20),
    )
    assert [stage["name"] for stage in scenario["stages"]] == [
        "get_users_42",
        "get_users_42_2",
        "get_users_42_3",
        "get_root",
        "propfind_caf_a_b_c",
        "get_segment_segment_segment_segment_segment_segment_segment",
    ]


def test_stage_names_skip_names_taken():
    """A name the numbering would give that another request already has
    (``/a/2`` is ``get_a_2``) is skipped."""
    scenario = _scenario(*(RecordedRequest("GET", f"https://x.test{path}") for path in ["/a", "/a/2", "/a", "/a", "/a/2"]))
    assert [stage["name"] for stage in scenario["stages"]] == ["get_a", "get_a_2", "get_a_3", "get_a_4", "get_a_2_2"]


def test_repeated_requests_build_in_linear_time():
    """A page polling one URL thousands of times: each repeat finds its
    stage name, and its placeholder's, at once, not by counting up through
    the names taken (which took seconds at 8000 requests)."""
    requests = [RecordedRequest("GET", f"https://x.test/poll?access_token=t{number}") for number in range(20_000)]
    result = build_scenario(requests, description="t")
    assert result.scenario["stages"][-1]["name"] == "get_poll_20000"
    assert result.placeholders[-1].var == "access_token_20000"


# --- bodies ---


@pytest.mark.parametrize(
    ("content_type", "body", "expected"),
    [
        # The body form sets this very type: the header goes without saying.
        pytest.param("application/json", TextData('{"a": [1, "{{x}}"]}'), {"body": {"json": {"a": [1, "\\{{x}}"]}}}, id="json"),
        pytest.param(
            "application/json; charset=utf-8", TextData("[1]"), {"headers": {"Content-Type": "application/json; charset=utf-8"}, "body": {"json": [1]}}, id="json-charset"
        ),
        pytest.param("application/vnd.api+json", TextData("null"), {"headers": {"Content-Type": "application/vnd.api+json"}, "body": {"json": None}}, id="json-suffix"),
        # What would not come back as sent is text.
        pytest.param("application/json", TextData("{bad"), {"headers": {"Content-Type": "application/json"}, "body": {"text": "{bad"}}, id="json-invalid"),
        pytest.param(
            "application/json", TextData('{"a": 1, "a": 2}'), {"headers": {"Content-Type": "application/json"}, "body": {"text": '{"a": 1, "a": 2}'}}, id="json-duplicate-member"
        ),
        pytest.param("application/json", TextData("[NaN]"), {"headers": {"Content-Type": "application/json"}, "body": {"text": "[NaN]"}}, id="json-nan"),
        # Too large for a float: infinity, which httpx would refuse to send.
        pytest.param("application/json", TextData('{"n": 1e400}'), {"headers": {"Content-Type": "application/json"}, "body": {"text": '{"n": 1e400}'}}, id="json-out-of-range"),
        pytest.param("application/x-www-form-urlencoded", TextData("a=1&a=2&b=x+y&c="), {"body": {"form": {"a": ["1", "2"], "b": "x y", "c": ""}}}, id="form"),
        # curl's -d sends a space as typed, which a server reads as one.
        pytest.param("application/x-www-form-urlencoded", TextData("scope=read write"), {"body": {"form": {"scope": "read write"}}}, id="form-space"),
        # What a form body would not send as it was is the text it was, its
        # secrets placeholders there: a repeated name apart from its first,
        # a name the file's loader would read as a reference.
        pytest.param(
            "application/x-www-form-urlencoded",
            TextData("a=1&b={{x}}&a=2&password=s3cret+pw"),
            {"headers": {"Content-Type": "application/x-www-form-urlencoded"}, "body": {"text": "a=1&b=\\{{x}}&a=2&password={{ quote(password) }}"}},
            id="form-interleaved",
        ),
        pytest.param(
            "application/x-www-form-urlencoded",
            TextData("$merge=a.json"),
            {"headers": {"Content-Type": "application/x-www-form-urlencoded"}, "body": {"text": "$merge=a.json"}},
            id="form-reference-key",
        ),
        # JSON sent with curl's default form type is no form.
        pytest.param(
            "application/x-www-form-urlencoded",
            TextData('{"a": "b=c"}'),
            {"headers": {"Content-Type": "application/x-www-form-urlencoded"}, "body": {"text": '{"a": "b=c"}'}},
            id="form-not-a-form",
        ),
        pytest.param("application/x-www-form-urlencoded", TextData(""), {"headers": {"Content-Type": "application/x-www-form-urlencoded"}, "body": {"text": ""}}, id="form-empty"),
        pytest.param(
            "application/x-www-form-urlencoded",
            TextData("a=1&&b"),
            {"headers": {"Content-Type": "application/x-www-form-urlencoded"}, "body": {"text": "a=1&&b"}},
            id="form-empty-piece",
        ),
        # Nested deeper than the import walks: sent as the text it is.
        pytest.param(
            "application/json",
            TextData(TOO_DEEP_TO_WALK.decode()),
            {"headers": {"Content-Type": "application/json"}, "body": {"text": TOO_DEEP_TO_WALK.decode()}},
            id="json-too-deep",
        ),
        pytest.param("text/plain", TextData("hi {{ x }}"), {"headers": {"Content-Type": "text/plain"}, "body": {"text": "hi \\{{ x }}"}}, id="text"),
        pytest.param(None, TextData("raw"), {"body": {"text": "raw"}}, id="text-untyped"),
        pytest.param(
            "multipart/form-data; boundary=zz",
            TextData('--zz\r\nContent-Disposition: form-data; name="a"\r\n\r\n1\r\n--zz--\r\n'),
            {"body": {"multipart": {"fields": {"a": "1"}}}},
            id="multipart-text",
        ),
        pytest.param(
            "multipart/form-data; boundary=zz",
            TextData("not multipart"),
            {"headers": {"Content-Type": "multipart/form-data; boundary=zz"}, "body": {"text": "not multipart"}},
            id="multipart-malformed",
        ),
        pytest.param("image/png", Base64Data("AAE="), {"headers": {"Content-Type": "image/png"}, "body": {"base64": "AAE="}}, id="base64"),
        pytest.param(
            "application/octet-stream",
            FileData("data/{{x}}.bin"),
            {"headers": {"Content-Type": "application/octet-stream"}, "body": {"binary": "data/\\{{x}}.bin"}},
            id="binary-file",
        ),
        pytest.param(
            "multipart/form-data",
            PartsData(
                (
                    Part("t", value="1"),
                    Part("t", value="2"),
                    Part("f", path="a.png"),
                    Part("g", path="b.txt", filename="c.txt", content_type="text/x"),
                    Part("h", path="n.txt", filename=""),
                    Part("i", value="v", content_type="text/y"),
                    Part("j", content="c", filename="j.txt"),
                    Part("k", base64="AA==", filename="k.bin"),
                    Part("f", path="d.png"),
                )
            ),
            {
                "body": {
                    "multipart": {
                        "fields": {"t": ["1", "2"]},
                        "files": {
                            "f": ["a.png", "d.png"],
                            "g": {"path": "b.txt", "filename": "c.txt", "content_type": "text/x"},
                            "h": {"path": "n.txt", "filename": ""},
                            "i": {"content": "v", "filename": "", "content_type": "text/y"},
                            "j": {"content": "c", "filename": "j.txt"},
                            "k": {"base64": "AA==", "filename": "k.bin"},
                        },
                    }
                }
            },
            id="parts",
        ),
        pytest.param("multipart/form-data", PartsData(()), {"body": {"multipart": {"fields": {}}}}, id="no-parts"),
    ],
)
def test_bodies(content_type, body, expected):
    headers = [("Content-Type", content_type)] if content_type else []
    assert _request(method="POST", headers=headers, body=body) == {"method": "POST", "url": "/a", **expected}


def test_files_read_are_listed():
    result = build_scenario(
        [
            RecordedRequest("POST", "https://x.test/a", body=FileData("body.bin")),
            RecordedRequest("POST", "https://x.test/b", body=PartsData((Part("f", path="a.png"), Part("g", path="body.bin")))),
        ],
        description="t",
    )
    assert result.files == ["body.bin", "a.png"]


def test_multipart_parts_keep_bytes_that_are_not_text():
    content = b'--zz\r\nContent-Disposition: form-data; name="f"; filename="b.bin"\r\nContent-Type: application/octet-stream\r\n\r\n\xff\x00\r\n--zz--\r\n'
    assert multipart_parts(content, "multipart/form-data; boundary=zz") == (Part("f", base64="/wA=", filename="b.bin", content_type="application/octet-stream"),)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b'--zz\r\nContent-Disposition: attachment; name="a"\r\n\r\n1\r\n--zz--\r\n', id="not-form-data"),
        pytest.param(b"--zz\r\nContent-Disposition: form-data\r\n\r\n1\r\n--zz--\r\n", id="nameless"),
        pytest.param(b"--zz--\r\n", id="no-part"),
    ],
)
def test_multipart_parts_refuse_what_is_no_form(content):
    assert multipart_parts(content, "multipart/form-data; boundary=zz") is None


# --- client settings ---


def test_client_settings_every_request_agrees_on():
    scenario = _scenario(
        RecordedRequest("GET", "https://x.test/a", timeout=5.0, follow_redirects=False, verify_tls=False),
        RecordedRequest("GET", "https://x.test/b", timeout=5.0, follow_redirects=False, verify_tls=False),
    )
    assert scenario["client"] == {"base_url": "https://x.test", "follow_redirects": False, "timeout": 5.0}
    assert scenario["ssl"] == {"verify": False}
    assert [set(stage["request"]) for stage in scenario["stages"]] == [{"url"}, {"url"}]


def test_client_settings_requests_disagree_on():
    """Redirects are followed by the requests that did; a timeout stays each
    request's own; TLS verification is the scenario's, so off for all once
    any turned it off, with a note."""
    result = build_scenario(
        [
            RecordedRequest("GET", "https://x.test/a", timeout=5.0, follow_redirects=False, verify_tls=False),
            RecordedRequest("GET", "https://x.test/b", follow_redirects=True),
        ],
        description="t",
    )
    scenario = result.scenario
    assert scenario["client"] == {"base_url": "https://x.test", "follow_redirects": False}
    assert [stage["request"] for stage in scenario["stages"]] == [{"url": "/a", "timeout": 5.0}, {"url": "/b", "allow_redirects": True}]
    assert scenario["ssl"] == {"verify": False}
    assert result.notes == ["TLS certificates go unchecked for every request (ssl.verify is the scenario's), though only some of them turned the check off."]


def test_auth_every_request_sends_is_the_scenarios():
    scenario = _scenario(
        RecordedRequest("GET", "https://x.test/a", headers=[("Authorization", "Bearer t")]),
        RecordedRequest("GET", "https://x.test/b", bearer="t"),
    )
    assert scenario["auth"] == {"bearer": "{{ api_token }}"}
    assert all("auth" not in stage["request"] for stage in scenario["stages"])


def test_auth_some_requests_send_stays_theirs():
    scenario = _scenario(RecordedRequest("GET", "https://x.test/a", bearer="t"), RecordedRequest("GET", "https://x.test/health"))
    assert "auth" not in scenario
    assert [stage["request"].get("auth") for stage in scenario["stages"]] == [{"bearer": "{{ api_token }}"}, None]


def test_scenario_layout():
    """What is sent where, then what it says, then how: the order a person
    reads a scenario in."""
    scenario = _scenario(
        RecordedRequest("POST", "https://x.test/a?q=1", headers=[("X", "1"), ("Content-Type", "text/plain")], body=TextData("b"), bearer="t", timeout=1.0, verify_tls=False),
        RecordedRequest("GET", "https://x.test/b", follow_redirects=True),
    )
    assert list(scenario) == ["description", "substitutions", "ssl", "client", "stages"]
    assert list(scenario["stages"][0]["request"]) == ["method", "url", "params", "headers", "auth", "body", "timeout"]
    assert list(scenario["stages"][0]) == ["name", "request", "response"]
    assert scenario["stages"][0]["response"] == [{"verify": {"status": "2xx"}}]
