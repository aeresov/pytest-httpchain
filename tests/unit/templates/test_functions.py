"""The built-in helper functions (templates/functions.py), called the way a
scenario calls them: through a template."""

import re
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from pytest_httpchain.templates import CALL_ONLY_BUILTINS, TEMPLATE_BUILTINS, TemplatesError, call_form, functions, walk
from tests.unit.helpers import TOO_DEEP_TO_PARSE, on_bounded_stack

# RFC 4231 test case 2.
HMAC_KEY, HMAC_MESSAGE = "Jefe", "what do ya want for nothing?"
HMAC_HEX = "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
HMAC_BASE64 = "W9zBRr9gdU5qBCQmCJV1x1oAPwidJzmDnexYuWTsOEM="


class _FrozenDatetime(datetime):
    """A clock stopped on a whole second, where isoformat() alone drops the
    microseconds."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 9, 27, 12, 34, 56, tzinfo=tz)


@pytest.fixture
def frozen_clock(monkeypatch):
    monkeypatch.setattr(functions, "datetime", _FrozenDatetime)
    monkeypatch.setattr(functions.time, "time_ns", lambda: 1_790_512_496_789_012_345)


def test_every_helper_is_a_template_builtin():
    """The validator reads TEMPLATE_BUILTINS to tell a typo from an
    engine-provided name, so every helper is in it."""
    assert set(functions.HELPER_FUNCTIONS) <= TEMPLATE_BUILTINS


class TestTime:
    @pytest.mark.parametrize(
        ("template", "expected"),
        [
            # Microseconds always, so every value has one width.
            pytest.param("{{ now() }}", "2026-09-27T12:34:56.000000+00:00", id="iso-8601"),
            pytest.param("{{ now(null) }}", "2026-09-27T12:34:56.000000+00:00", id="null-format-is-iso"),
            pytest.param("{{ now('%Y-%m-%d %H:%M:%S %z') }}", "2026-09-27 12:34:56 +0000", id="strftime"),
            pytest.param("{{ now(fmt='%d/%m/%Y') }}", "27/09/2026", id="keyword"),
            pytest.param("{{ timestamp() }}", 1_790_512_496, id="timestamp"),
            pytest.param("{{ timestamp_ms() }}", 1_790_512_496_789, id="timestamp-ms"),
        ],
    )
    def test_frozen_clock(self, frozen_clock, template, expected):
        result = walk(template, {})
        assert result == expected
        assert type(result) is type(expected)

    def test_now_reads_the_utc_clock(self):
        before = datetime.now(UTC)
        result = walk("{{ now() }}", {})
        after = datetime.now(UTC)
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}\+00:00", result)
        assert before <= datetime.fromisoformat(result) <= after

    def test_timestamps_read_the_same_clock(self):
        before = time.time()
        seconds, millis = walk("{{ [timestamp(), timestamp_ms()] }}", {})
        after = time.time()
        assert int(before) <= seconds <= millis / 1000 <= after

    def test_format_must_be_text(self):
        with pytest.raises(TemplatesError, match=r"^TypeError in expression '\{\{ now\(5\) \}\}': now\(\) takes a strftime format as text, not int$"):
            walk("{{ now(5) }}", {})

    @pytest.mark.parametrize("fmt", ["%s", "%-s", "at %Y %s", "%%%s"])
    def test_epoch_seconds_directive_is_refused(self, fmt):
        """The C library's %s formats the UTC time as though it were local: on
        a host east or west of UTC, epoch seconds off by the offset."""
        with pytest.raises(TemplatesError, match=r"now\(\) cannot format %s, which the C library reads as local time; for Unix seconds, use timestamp\(\)$"):
            walk("{{ now('" + fmt + "') }}", {})

    def test_escaped_percent_s_is_literal_text(self, frozen_clock):
        assert walk("{{ now('%Y %%s') }}", {}) == "2026 %s"


class TestBase64:
    @pytest.mark.parametrize(
        ("template", "context", "expected"),
        [
            pytest.param("{{ b64encode('hé') }}", {}, "aMOp", id="text-is-utf-8"),
            pytest.param("{{ b64encode(raw) }}", {"raw": b"\xfb\xff"}, "+/8=", id="bytes"),
            pytest.param("{{ b64encode(raw, urlsafe=true) }}", {"raw": b"\xfb\xff"}, "-_8=", id="urlsafe"),
            # urlsafe is positional too, as quote's safe and hmac_sha256's encoding are.
            pytest.param("{{ b64encode(raw, true) }}", {"raw": b"\xfb\xff"}, "-_8=", id="urlsafe-positional"),
            pytest.param("{{ b64decode('Pz8-', true) }}", {}, "??>", id="decode-urlsafe-positional"),
            pytest.param("{{ b64encode('') }}", {}, "", id="empty"),
            pytest.param("{{ b64decode('aMOp') }}", {}, "hé", id="decode"),
            pytest.param("{{ b64decode(raw) }}", {"raw": b"aMOp"}, "hé", id="decode-bytes"),
            # Padding is optional: a JWT segment comes without it.
            pytest.param("{{ b64decode('YQ') }}", {}, "a", id="unpadded"),
            pytest.param("{{ b64decode('YQ=') }}", {}, "a", id="short-padded"),
            pytest.param("{{ b64decode('eyJzdWIiOiJhbGljZSIsIm4iOjF9', urlsafe=true) }}", {}, '{"sub":"alice","n":1}', id="urlsafe-unpadded"),
            pytest.param("{{ b64decode(b64encode(text)) }}", {"text": "ünïcode ✓"}, "ünïcode ✓", id="round-trip"),
        ],
    )
    def test_values(self, template, context, expected):
        assert walk(template, context) == expected

    @pytest.mark.parametrize(
        ("template", "context", "message"),
        [
            pytest.param("{{ b64encode(1) }}", {}, r"TypeError .*: b64encode\(\) takes text or bytes, not int$", id="encode-number"),
            pytest.param("{{ b64encode(obj) }}", {"obj": SimpleNamespace(a=1)}, r"b64encode\(\) takes text or bytes, not an object$", id="encode-vars-object"),
            pytest.param("{{ b64encode('a', urlsafe='yes') }}", {}, r"b64encode\(\) urlsafe must be true or false, not str$", id="urlsafe-not-bool"),
            pytest.param("{{ b64decode('!!!!') }}", {}, r"ValueError .*: b64decode\(\) got text that is not base64 \(", id="outside-the-alphabet"),
            # The standard library would skip the space and decode the rest.
            pytest.param("{{ b64decode('YW Jj') }}", {}, r"b64decode\(\) got text that is not base64 \(", id="whitespace"),
            pytest.param("{{ b64decode('a') }}", {}, r"b64decode\(\) got text that is not base64 \(", id="impossible-length"),
            pytest.param("{{ b64decode('-_8=') }}", {}, r"not base64 \(.*\); for URL-safe base64, pass urlsafe=true$", id="urlsafe-hint"),
            pytest.param("{{ b64decode('aé==') }}", {}, r"b64decode\(\) got text that is not base64: it holds a character outside ASCII$", id="non-ascii"),
            pytest.param("{{ b64decode('/w==') }}", {}, r"b64decode\(\) decoded 1 bytes that are not UTF-8 text$", id="not-utf-8"),
            pytest.param("{{ b64decode(None) }}", {}, r"b64decode\(\) takes text or bytes, not None$", id="decode-none"),
        ],
    )
    def test_errors(self, template, context, message):
        with pytest.raises(TemplatesError, match=message):
            walk(template, context)


def _self_containing() -> SimpleNamespace:
    """A fixture's value can contain itself; JSON cannot. A namespace one
    reaches the encoder through its fallback hook."""
    loop = SimpleNamespace()
    loop.me = loop
    return loop


class TestJson:
    @pytest.mark.parametrize(
        ("template", "context", "expected"),
        [
            # A vars object is encoded as the object it was written as, at any depth.
            pytest.param(
                "{{ json_dumps(payload) }}",
                {"payload": SimpleNamespace(a=1, b=[SimpleNamespace(c=None), (True, "x")])},
                '{"a": 1, "b": [{"c": null}, [true, "x"]]}',
                id="vars-object",
            ),
            pytest.param("{{ json_dumps({'k': 'é'}) }}", {}, '{"k": "\\u00e9"}', id="json-dumps-defaults"),
            pytest.param("{{ json_dumps(null) }}", {}, "null", id="null"),
            pytest.param("{{ json_loads(text)['a'][1] }}", {"text": '{"a": [1, 2]}'}, 2, id="loads"),
            pytest.param("{{ json_loads(raw) }}", {"raw": b'{"a": true}'}, {"a": True}, id="loads-bytes"),
            pytest.param("{{ json_loads('null') }}", {}, None, id="loads-null"),
            pytest.param("{{ json_loads(json_dumps(payload)) }}", {"payload": SimpleNamespace(a=SimpleNamespace(b=[1]))}, {"a": {"b": [1]}}, id="round-trip"),
        ],
    )
    def test_values(self, template, context, expected):
        result = walk(template, context)
        assert result == expected
        assert type(result) is type(expected)

    @pytest.mark.parametrize(
        ("template", "context", "message"),
        [
            pytest.param("{{ json_dumps(set([1])) }}", {}, r"^TypeError .*: json_dumps\(\) cannot encode set as JSON$", id="not-encodable"),
            pytest.param("{{ json_dumps(raw) }}", {"raw": b"x"}, r"json_dumps\(\) cannot encode bytes as JSON$", id="bytes"),
            pytest.param("{{ json_dumps(loop) }}", {"loop": _self_containing()}, r"^ValueError .*: Circular reference detected$", id="circular"),
            pytest.param("{{ json_loads('{') }}", {}, r"^ValueError .*: json_loads\(\) got text that is not JSON: Expecting property name", id="not-json"),
            pytest.param("{{ json_loads(raw) }}", {"raw": b"\x80"}, r"json_loads\(\) got bytes that are not JSON text: 'utf-8' codec", id="undecodable-bytes"),
            pytest.param("{{ json_loads(1) }}", {}, r"^TypeError .*: json_loads\(\) takes text or bytes, not int$", id="number"),
        ],
    )
    def test_errors(self, template, context, message):
        with pytest.raises(TemplatesError, match=message):
            walk(template, context)

    def test_loads_nested_past_the_decoder_fails_as_templates_error(self):
        """The decoder's RecursionError is a stage failure like any other."""
        with pytest.raises(TemplatesError, match=r"^RecursionError in expression '\{\{ json_loads\(raw\) \}\}'"):
            on_bounded_stack(walk, "{{ json_loads(raw) }}", {"raw": TOO_DEEP_TO_PARSE})


class TestUrl:
    @pytest.mark.parametrize(
        "params",
        [
            pytest.param({"q": "a b&c=d/é", "page": 2}, id="text-and-numbers"),
            pytest.param({"tag": ["x", "y"], "empty": [], "pair": ("a", 1)}, id="lists-repeat-the-key"),
            pytest.param({"on": True, "off": False, "none": None, "ratio": 1.5}, id="booleans-and-none"),
        ],
    )
    def test_urlencode_encodes_as_request_params(self, params):
        """``urlencode`` is what ``request.params`` sends for the same object,
        so the two can build the same query."""
        assert walk("{{ urlencode(params) }}", {"params": params}) == str(httpx.QueryParams(params))

    def test_urlencode_takes_a_vars_object(self):
        assert walk("{{ urlencode(query) }}", {"query": SimpleNamespace(q="a b", tags=["x", "y"])}) == "q=a+b&tags=x&tags=y"

    def test_urlencode_takes_bytes_as_they_are(self):
        """Bytes from a fixture or function are percent-encoded as they are, as
        `quote` takes them: httpx would send their repr (``b'xy'``)."""
        query = {"a": b"xy", "b": bytearray(b"\xff z"), "c": ["x", b"y"]}
        assert walk("{{ urlencode(query) }}", {"query": query}) == "a=xy&b=%FF+z&c=x&c=y"

    @pytest.mark.parametrize(
        ("template", "context", "message"),
        [
            pytest.param("{{ urlencode('a=1') }}", {}, r"^TypeError .*: urlencode\(\) takes an object, not str$", id="not-an-object"),
            # httpx would send the Python repr of a nested value.
            pytest.param("{{ urlencode({'filter': {'a': 1} }) }}", {}, r"'filter' holds dict$", id="nested-dict"),
            pytest.param("{{ urlencode(query) }}", {"query": SimpleNamespace(filter=SimpleNamespace(a=1))}, r"'filter' holds an object$", id="nested-vars-object"),
            pytest.param("{{ urlencode({'ids': [[1, 2]]}) }}", {}, r"'ids' holds list$", id="nested-list"),
            # A helper passed uncalled, which the `}}` gotcha makes easy to
            # write in a dict(...) call: its repr is no query value.
            pytest.param(
                "{{ urlencode(dict(category='x', sort=timestamp)) }}",
                {},
                r"urlencode\(\) got a function for 'sort', not a value: call it for its value$",
                id="uncalled-helper",
            ),
            pytest.param("{{ urlencode({'at': [now]}) }}", {}, r"got a function for 'at'", id="uncalled-helper-in-a-list"),
        ],
    )
    def test_urlencode_errors(self, template, context, message):
        with pytest.raises(TemplatesError, match=message):
            walk(template, context)

    @pytest.mark.parametrize(
        ("template", "context", "expected"),
        [
            # One path segment: `/` is encoded too.
            pytest.param("{{ quote('a b/c?d=é&e') }}", {}, "a%20b%2Fc%3Fd%3D%C3%A9%26e", id="segment"),
            pytest.param("{{ quote('a b/c', '/') }}", {}, "a%20b/c", id="safe"),
            pytest.param("{{ quote('a/b', safe='/') }}", {}, "a/b", id="safe-keyword"),
            pytest.param("{{ quote(raw) }}", {"raw": b"\xff"}, "%FF", id="bytes"),
        ],
    )
    def test_quote(self, template, context, expected):
        assert walk(template, context) == expected

    @pytest.mark.parametrize(
        ("template", "message"),
        [
            pytest.param("{{ quote(42) }}", r"^TypeError .*: quote\(\) takes text or bytes, not int$", id="number"),
            pytest.param("{{ quote('a', 1) }}", r"quote\(\) safe must be text, not int$", id="safe-not-text"),
        ],
    )
    def test_quote_errors(self, template, message):
        with pytest.raises(TemplatesError, match=message):
            walk(template, {})


class TestHashing:
    @pytest.mark.parametrize(
        ("template", "context", "expected"),
        [
            pytest.param("{{ sha256('abc') }}", {}, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", id="sha256"),
            pytest.param("{{ sha256('hé') }}", {}, "7dfbe0eab96510b11c9a2671d83019cd52953211294db5f917ffa0b7cc84f534", id="sha256-utf-8"),
            pytest.param("{{ sha256(raw) }}", {"raw": b"abc"}, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", id="sha256-bytes"),
            pytest.param("{{ md5('abc') }}", {}, "900150983cd24fb0d6963f7d28e17f72", id="md5"),
            pytest.param("{{ hmac_sha256(key, message) }}", {"key": HMAC_KEY, "message": HMAC_MESSAGE}, HMAC_HEX, id="hmac-hex"),
            pytest.param("{{ hmac_sha256(key, message, 'base64') }}", {"key": HMAC_KEY, "message": HMAC_MESSAGE}, HMAC_BASE64, id="hmac-base64"),
            pytest.param("{{ hmac_sha256(key, message, encoding='hex') }}", {"key": HMAC_KEY.encode(), "message": HMAC_MESSAGE.encode()}, HMAC_HEX, id="hmac-bytes"),
        ],
    )
    def test_digests(self, template, context, expected):
        assert walk(template, context) == expected

    @pytest.mark.parametrize(
        ("template", "message"),
        [
            # A number has more than one text form: the author picks it with str().
            pytest.param("{{ sha256(1) }}", r"^TypeError .*: sha256\(\) takes text or bytes, not int$", id="sha256-number"),
            pytest.param("{{ md5(null) }}", r"md5\(\) takes text or bytes, not None$", id="md5-none"),
            pytest.param("{{ hmac_sha256(1, 'm') }}", r"hmac_sha256\(\) takes text or bytes, not int$", id="hmac-key"),
            pytest.param("{{ hmac_sha256('k', ['m']) }}", r"hmac_sha256\(\) takes text or bytes, not list$", id="hmac-message"),
            pytest.param("{{ hmac_sha256('k', 'm', 'b64') }}", r"^ValueError .*: hmac_sha256\(\) encoding must be 'hex' or 'base64', not 'b64'$", id="hmac-encoding"),
        ],
    )
    def test_errors(self, template, message):
        with pytest.raises(TemplatesError, match=message):
            walk(template, {})


# env is a function of the engine's own now, not os.environ's bound method.
@pytest.mark.parametrize("name", sorted({*functions.HELPER_FUNCTIONS, "env"}))
@pytest.mark.parametrize("attribute", ["__globals__", "__code__", "__module__"])
def test_helper_attributes_stay_out_of_reach(name, attribute):
    """A helper is reachable as a name, like every built-in, but simpleeval
    still refuses its dunder attributes: through ``__globals__`` it would
    hand out the modules the helpers import."""
    with pytest.raises(TemplatesError, match=r"access to __attributes"):
        walk("{{ " + name + "." + attribute + " }}", {})


@pytest.mark.parametrize(
    ("template", "context", "expected"),
    [
        # The runtime looks a name up among the user's first.
        pytest.param("{{ timestamp }}", {"timestamp": 1_700_000_000}, 1_700_000_000, id="user-value-shadows-a-read"),
        pytest.param("{{ now() }}", {"now": lambda: "frozen"}, "frozen", id="user-function-shadows-a-call"),
        # A value is no function: the call still reaches the built-in.
        pytest.param("{{ quote('a b') }}", {"quote": "a saved quote"}, "a%20b", id="user-value-leaves-the-call-alone"),
        # get() and exists() are merged over the user's callables: never shadowed.
        pytest.param("{{ get('x', 1) }}", {"get": lambda *_: "mine"}, 1, id="user-function-never-shadows-get"),
        pytest.param("{{ exists('x') }}", {"exists": lambda *_: "mine"}, False, id="user-function-never-shadows-exists"),
    ],
)
def test_user_names_shadow_helpers(template, context, expected):
    assert walk(template, context) == expected


def _user_clock() -> str:
    return "frozen"


@pytest.mark.parametrize(
    ("template", "name", "call"),
    [
        pytest.param("{{ now }}", "now", "now()", id="whole-template"),
        pytest.param("sent at {{ now }}", "now", "now()", id="interpolated"),
        pytest.param("{{ timestamp if ready else 0 }}", "timestamp", "timestamp()", id="chosen"),
        # The call a built-in cannot be called without arguments for reads so.
        pytest.param("{{ sha256 }}", "sha256", "sha256(...)", id="takes-arguments"),
        # The older built-ins of no use as a value are refused alike.
        pytest.param("X-Request-Id: {{ uuid4 }}", "uuid4", "uuid4()", id="uuid4"),
        pytest.param("{{ rand }}", "rand", "rand()", id="rand"),
        pytest.param("{{ randint }}", "randint", "randint(...)", id="randint"),
        pytest.param("e={{ env }}", "env", "env(...)", id="env"),
    ],
)
def test_uncalled_builtin_is_refused(template, name, call):
    """Written without its parentheses, a built-in would render as its repr
    (`<function now at 0x...>`) into a URL, a header or a body, with no error."""
    message = rf"^Uncalled function in expression '\{{\{{ .* \}}\}}': no value named '{name}' is defined here, so {name} is the built-in function, not a value; "
    with pytest.raises(TemplatesError, match=message + f"if the built-in is meant, call it: {re.escape(call)}$"):
        walk(template, {"ready": True})


def test_call_form_covers_every_call_only_builtin():
    """The runtime refusal and HTTPCHAIN035 both write the call to make."""
    assert {name: call_form(name) for name in CALL_ONLY_BUILTINS} == {
        **{name: f"{name}()" for name in ("now", "timestamp", "timestamp_ms", "uuid4", "rand")},
        **{name: f"{name}(...)" for name in ("b64encode", "b64decode", "json_dumps", "json_loads", "urlencode", "quote", "sha256", "md5", "hmac_sha256", "env", "randint")},
    }


def test_env_repr_holds_no_environment(monkeypatch):
    """env was os.environ.get, whose repr is os.environ's: every variable with
    its value. Where the function still reaches a request as text, uncalled
    inside an expression (HTTPCHAIN035 warns of it), none of them goes along."""
    monkeypatch.setenv("HTTPCHAIN_TEST_SECRET", "hunter2")
    rendered = walk("{{ str(env) }} {{ [env] }}", {})
    assert "hunter2" not in rendered
    assert "HTTPCHAIN_TEST_SECRET" not in rendered


@pytest.mark.parametrize(
    ("template", "context", "expected"),
    [
        # Handed to a function, a helper is a function, as it should be.
        pytest.param("{{ sorted(['b', 'a'], key=sha256) }}", {}, ["b", "a"], id="key-function"),
        pytest.param("{{ call(timestamp) > 0 }}", {"call": lambda f: f()}, True, id="user-function-argument"),
        # The user's own `now`, value or function, is theirs.
        pytest.param("{{ now }}", {"now": "a saved now"}, "a saved now", id="user-value"),
        pytest.param("{{ now is clock }}", {"now": _user_clock, "clock": _user_clock}, True, id="user-function"),
    ],
)
def test_helper_used_as_a_function_is_not_refused(template, context, expected):
    assert walk(template, context) == expected
