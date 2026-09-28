"""Built-in helper functions for template expressions: time, encoding, hashing
and URL building, the values a scenario otherwise needed a fixture for.

Each returns a plain value (text, a number, JSON data), never an object of a
class of its own, so an expression gains values to work with and nothing to
reach through. A wrong argument raises a TypeError or ValueError naming the
function, which ``_eval_expr`` reports as the template's `TemplatesError`.
"""

import base64
import binascii
import hashlib
import hmac
import json
import re
import time
import urllib.parse
from collections.abc import Mapping
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any


def _kind(value: Any) -> str:
    """How an error names a value's type: a ``vars`` object renders as a
    SimpleNamespace, which the author wrote as an object."""
    if value is None:
        return "None"
    if isinstance(value, SimpleNamespace):
        return "an object"
    return type(value).__name__


def _to_bytes(function: str, value: Any) -> bytes:
    """``value`` as bytes: text is encoded as UTF-8. Anything else is refused
    rather than turned into text: a hash or a signature of ``1.0`` is one of
    ``"1.0"`` or ``"1"``, and which one is the author's call, made with str()."""
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, bytes | bytearray):
        return bytes(value)
    raise TypeError(f"{function}() takes text or bytes, not {_kind(value)}")


def _require_bool(function: str, name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{function}() {name} must be true or false, not {_kind(value)}")
    return value


# One strftime directive: ``%``, glibc's flags and width, an E/O modifier, and
# the conversion character (``%%`` included, so its ``%`` never starts another).
_STRFTIME_DIRECTIVE = re.compile(r"%[-_0^#]*[0-9]*[EO]?(.?)", re.DOTALL)


def now(fmt: str | None = None) -> str:
    """The current UTC time: ISO 8601 with its offset, or ``strftime(fmt)``.

    Always with microseconds (``2026-09-27T12:34:56.789012+00:00``): isoformat
    drops them when they are 0, so one call in a million would render shorter
    than the rest.

    ``%s`` is refused. It is no Python directive but the C library's, which
    reads the time it formats as local time: on a host not on UTC, epoch
    seconds off by the local offset, with no error (and Windows has no ``%s``).
    ``timestamp()`` is the epoch value.
    """
    moment = datetime.now(UTC)
    if fmt is None:
        return moment.isoformat(timespec="microseconds")
    if not isinstance(fmt, str):
        raise TypeError(f"now() takes a strftime format as text, not {_kind(fmt)}")
    if any(directive.group(1) == "s" for directive in _STRFTIME_DIRECTIVE.finditer(fmt)):
        raise ValueError("now() cannot format %s, which the C library reads as local time; for Unix seconds, use timestamp()")
    return moment.strftime(fmt)


def timestamp() -> int:
    """The current Unix time in whole seconds."""
    return time.time_ns() // 1_000_000_000


def timestamp_ms() -> int:
    """The current Unix time in whole milliseconds."""
    return time.time_ns() // 1_000_000


def b64encode(value: str | bytes, urlsafe: bool = False) -> str:
    """Base64 of ``value`` (text encoded as UTF-8), padded; ``urlsafe`` uses
    ``-`` and ``_`` for ``+`` and ``/``."""
    data = _to_bytes("b64encode", value)
    encode = base64.urlsafe_b64encode if _require_bool("b64encode", "urlsafe", urlsafe) else base64.b64encode
    return encode(data).decode("ascii")


def b64decode(value: str | bytes, urlsafe: bool = False) -> str:
    """The UTF-8 text a base64 string encodes.

    Padding is optional, as RFC 4648 allows where the length says where the data
    ends: a JWT segment or other URL-safe base64 usually comes without it. A
    character outside the alphabet fails, whitespace included, where the
    standard library by default skips it and decodes what is left.
    """
    urlsafe = _require_bool("b64decode", "urlsafe", urlsafe)
    if isinstance(value, str) and not value.isascii():
        raise ValueError("b64decode() got text that is not base64: it holds a character outside ASCII")
    data = _to_bytes("b64decode", value)
    try:
        decoded = base64.b64decode(data + b"=" * (-len(data) % 4), altchars=b"-_" if urlsafe else None, validate=True)
    except binascii.Error as e:
        hint = "; for URL-safe base64, pass urlsafe=true" if not urlsafe and (b"-" in data or b"_" in data) else ""
        raise ValueError(f"b64decode() got text that is not base64 ({e}){hint}") from None
    try:
        return decoded.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"b64decode() decoded {len(decoded)} bytes that are not UTF-8 text") from None


def _namespace_to_dict(value: Any) -> Any:
    # json.dumps calls this for any value it cannot encode itself, and encodes
    # what it returns in its place, circular-reference check included.
    if isinstance(value, SimpleNamespace):
        return vars(value)
    raise TypeError(f"json_dumps() cannot encode {_kind(value)} as JSON")


def json_dumps(value: Any) -> str:
    """``value`` as JSON text, with ``json.dumps``' defaults (``", "`` and
    ``": "`` separators, non-ASCII escaped). A ``vars`` object is encoded as
    the object it was written as, at any depth."""
    return json.dumps(value, default=_namespace_to_dict)


def json_loads(text: str | bytes) -> Any:
    """The JSON value ``text`` holds, objects as dicts, as a JMESPath save
    holds them."""
    if not isinstance(text, str | bytes | bytearray):
        raise TypeError(f"json_loads() takes text or bytes, not {_kind(text)}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"json_loads() got text that is not JSON: {e}") from None
    except UnicodeDecodeError as e:
        # Bytes are decoded as the UTF-8, -16 or -32 their first bytes suggest.
        raise ValueError(f"json_loads() got bytes that are not JSON text: {e}") from None


def _query_value(key: str, value: Any) -> str | bytes:
    """A query value as text, as httpx renders ``request.params``: ``true`` and
    ``false`` for booleans, empty for None. Bytes are percent-encoded as they
    are, as ``quote`` takes them, where httpx would send their Python repr
    (``b'xy'``). A nested object or list has no query form (httpx would send
    its repr too), and a function is a helper or user function passed
    uncalled, so both are refused."""
    match value:
        case True:
            return "true"
        case False:
            return "false"
        case None:
            return ""
        case str():
            return value
        case bytes() | bytearray():
            return bytes(value)
        case Mapping() | SimpleNamespace() | list() | tuple() | set() | frozenset():
            raise TypeError(f"urlencode() takes text, a number, a boolean or None for each key, or a list of them; '{key}' holds {_kind(value)}")
        case _ if callable(value):
            raise TypeError(f"urlencode() got a function for '{key}', not a value: call it for its value")
        case _:
            return str(value)


def urlencode(mapping: Mapping[str, Any] | SimpleNamespace) -> str:
    """A query string (``a=1&b=x+y``) from an object, encoded as
    ``request.params`` is: a list value repeats its key, in order. Bytes are
    percent-encoded as they are."""
    items = vars(mapping) if isinstance(mapping, SimpleNamespace) else mapping
    if not isinstance(items, Mapping):
        raise TypeError(f"urlencode() takes an object, not {_kind(mapping)}")
    pairs = []
    for key, value in items.items():
        for item in value if isinstance(value, list | tuple) else [value]:
            pairs.append((str(key), _query_value(key, item)))
    return urllib.parse.urlencode(pairs)


def quote(text: str | bytes, safe: str = "") -> str:
    """``text`` percent-encoded for a URL (UTF-8), every reserved character
    included, ``/`` too, unless listed in ``safe``: one path segment."""
    if not isinstance(text, str | bytes | bytearray):
        raise TypeError(f"quote() takes text or bytes, not {_kind(text)}")
    if not isinstance(safe, str):
        raise TypeError(f"quote() safe must be text, not {_kind(safe)}")
    return urllib.parse.quote(text, safe=safe)


def sha256(value: str | bytes) -> str:
    """Hex SHA-256 digest of ``value`` (text encoded as UTF-8)."""
    return hashlib.sha256(_to_bytes("sha256", value)).hexdigest()


def md5(value: str | bytes) -> str:
    """Hex MD5 digest of ``value`` (text encoded as UTF-8). A checksum, not a
    security measure: flagged so, so that a FIPS-mode interpreter allows it."""
    return hashlib.md5(_to_bytes("md5", value), usedforsecurity=False).hexdigest()


def hmac_sha256(key: str | bytes, message: str | bytes, encoding: str = "hex") -> str:
    """HMAC-SHA256 of ``message`` under ``key`` (text encoded as UTF-8), as hex
    or as (padded, standard) base64."""
    digest = hmac.digest(_to_bytes("hmac_sha256", key), _to_bytes("hmac_sha256", message), "sha256")
    match encoding:
        case "hex":
            return digest.hex()
        case "base64":
            return base64.b64encode(digest).decode("ascii")
        case _:
            raise ValueError(f"hmac_sha256() encoding must be 'hex' or 'base64', not {encoding!r}")


HELPER_FUNCTIONS = {
    "now": now,
    "timestamp": timestamp,
    "timestamp_ms": timestamp_ms,
    "b64encode": b64encode,
    "b64decode": b64decode,
    "json_dumps": json_dumps,
    "json_loads": json_loads,
    "urlencode": urlencode,
    "quote": quote,
    "sha256": sha256,
    "md5": md5,
    "hmac_sha256": hmac_sha256,
}
