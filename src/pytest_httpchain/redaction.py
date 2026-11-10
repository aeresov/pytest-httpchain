"""Credential redaction for everything that shows an HTTP exchange or a
connection's messages.

One set of rules, shared by the report sections, the HAR export and the
failure messages that echo a header (verification and request errors): the
value of a listed header, and of a listed query parameter wherever a URL is
shown, prints as ``[REDACTED]``. Names stay visible, so a report still says
which credential was sent. Bodies are never touched: their shape is the
scenario's own, and guessing at it would hide data without making the output
safe.

A connection stage's messages are the exception, by key name: they have no
wire form the report shows, no headers or query to carry a credential apart
from the data, so the value under a listed message key, at any depth, prints
as ``[REDACTED]`` in the Connection Exchange section and in the failure
messages that quote a message (`Redaction.message`, `Redaction.message_text`).
What the stage's checks and saves see is never redacted.
"""

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import unquote_plus

import httpx

REDACTED = "[REDACTED]"

# The ini defaults (`httpchain_redact_headers`, `httpchain_redact_query_params`):
# the credential carriers common enough that a report showing them is a leak.
DEFAULT_REDACT_HEADERS = ("Authorization", "Proxy-Authorization", "Cookie", "Set-Cookie", "X-API-Key", "API-Key", "X-Auth-Token")
DEFAULT_REDACT_QUERY_PARAMS = ("access_token", "refresh_token", "id_token", "api_key", "apikey", "client_secret", "password", "token")
# And `httpchain_redact_message_keys`: both lists, lowercased, as a message's
# keys are matched (case-insensitively).
DEFAULT_REDACT_MESSAGE_KEYS = tuple(dict.fromkeys(name.lower() for name in (*DEFAULT_REDACT_HEADERS, *DEFAULT_REDACT_QUERY_PARAMS)))

# Headers whose value is a URL, which can carry the same query credentials a
# request URL does: a redirect's Location is how OAuth hands a code or token over.
_URL_HEADERS = frozenset({"location", "content-location", "referer"})

# A folded Set-Cookie value ("a=1; Path=/, b=2", as `Headers.get` joins repeated
# lines) splits before each "name=": the comma in an Expires date is followed by
# a day number and a space, never by "=".
_SET_COOKIE_SEPARATOR = re.compile(r"(,\s*)(?=[^;,=\s]+=)")

# "scheme://authority": the authority runs up to the first "/", "?" or "#".
_URL_AUTHORITY = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?P<authority>[^/?#]*)")


class Redaction:
    """Which header, query-parameter and message-key values to hide, by
    case-insensitive name.

    Empty sets redact nothing. An empty value is shown as it is: an empty
    ``Authorization`` hides nothing, and is worth seeing when a template
    rendered to "".
    """

    __slots__ = ("headers", "message_keys", "query_params")

    def __init__(self, headers: Iterable[str] = (), query_params: Iterable[str] = (), message_keys: Iterable[str] = ()) -> None:
        self.headers = frozenset(name.lower() for name in headers)
        self.query_params = frozenset(name.lower() for name in query_params)
        self.message_keys = frozenset(name.lower() for name in message_keys)

    def redacts_header(self, name: str) -> bool:
        return name.lower() in self.headers

    def redacts_query_param(self, name: str) -> bool:
        """``name`` as decoded: ``api_key`` for a query's ``api%5Fkey``."""
        return name.lower() in self.query_params

    def header(self, name: str, value: str) -> str:
        """One header's value as it may be shown.

        ``Cookie`` keeps each cookie's name and ``Set-Cookie`` its cookie's
        name and attributes; any other listed header is hidden whole. A header
        carrying a URL has that URL's query redacted.
        """
        lowered = name.lower()
        if lowered in self.headers:
            if lowered == "cookie":
                return ";".join(_redact_cookie_pair(pair) for pair in value.split(";"))
            if lowered == "set-cookie":
                return _redact_set_cookie(value)
            return REDACTED if value else value
        if lowered in _URL_HEADERS:
            return self.url(value)
        return value

    def header_items(self, headers: httpx.Headers) -> list[tuple[str, str]]:
        """Every header line as it may be shown. ``multi_items()``, so a repeated
        header (notably Set-Cookie, which must never be comma-folded) stays the
        separate wire lines it was sent as."""
        return [(name, self.header(name, value)) for name, value in headers.multi_items()]

    def query_param(self, name: str, value: str) -> str:
        """One decoded query parameter's value as it may be shown."""
        return REDACTED if value and self.redacts_query_param(name) else value

    def url(self, url: str | httpx.URL) -> str:
        """A URL as it may be shown, otherwise exactly as written.

        The listed parameters' values are replaced in the query and in a
        ``key=value`` fragment (OAuth's implicit flow returns its token there).
        The userinfo is redacted along with ``Authorization``: httpx sends it
        as that header's Basic credentials.
        """
        text = str(url)
        before_fragment, hash_mark, fragment = text.partition("#")
        before_query, question_mark, query = before_fragment.partition("?")
        if "authorization" in self.headers:
            before_query = _URL_AUTHORITY.sub(_redact_userinfo, before_query, count=1)
        return f"{before_query}{question_mark}{self._query(query)}{hash_mark}{self._query(fragment)}"

    def error_text(self, text: str, headers: httpx.Headers) -> str:
        """An error message about a request with ``headers``, as it may be shown.

        A header value the message quotes is replaced by that value as the
        report shows it: h11 refuses a value with a newline or a CR (a token
        read from a file keeps its trailing newline) as ``Illegal header value
        b'Bearer <token>\\n'``. Only the quoted forms ``repr()`` writes, of the
        bytes sent and of the text: an unquoted value could match any part of
        the message, as a test key ``1`` does in ``127.0.0.1``.
        """
        for name, value in headers.multi_items():
            shown = self.header(name, value)
            if shown != value:
                text = text.replace(repr(value.encode(headers.encoding)), repr(shown.encode(headers.encoding)))
                text = text.replace(repr(value), repr(shown))
        return text

    def redacts_message_key(self, key: str) -> bool:
        return key.lower() in self.message_keys

    def message(self, value: Any) -> Any:
        """A connection message (or what ``send()`` returned) as it may be
        shown: a copy, in which the value of each listed key, at any depth,
        is ``[REDACTED]``, unless it is empty (null, ``""``, ``b""``, ``[]``
        or ``{}``), which hides nothing. With no key listed, ``value`` itself.

        Iterative, so a deep message is no RecursionError; a container met
        again is the same copy, so a shared one, or one holding itself, is
        copied once. Dicts and lists (a tuple too) are walked, as JSON has
        them; any other value is kept as it is.
        """
        if not self.message_keys or not isinstance(value, Mapping | list | tuple):
            return value
        copies: dict[int, Any] = {}
        root = copies[id(value)] = {} if isinstance(value, Mapping) else []
        pending: list[tuple[Any, Any]] = [(value, root)]
        while pending:
            original, copied = pending.pop()
            members = original.items() if isinstance(original, Mapping) else enumerate(original)
            for key, member in members:
                if isinstance(original, Mapping) and isinstance(key, str) and self.redacts_message_key(key) and not _empty(member):
                    shown = REDACTED
                elif isinstance(member, Mapping | list | tuple):
                    shown = copies.get(id(member))
                    if shown is None:
                        shown = copies[id(member)] = {} if isinstance(member, Mapping) else []
                        pending.append((member, shown))
                else:
                    shown = member
                if isinstance(copied, dict):
                    copied[key] = shown
                else:
                    copied.append(shown)
        return root

    def message_text(self, text: str, *messages: Any) -> str:
        """A failure line quoting values of ``messages``, as it may be shown:
        each string or bytes a listed key holds in them, at any depth, is
        replaced where the line quotes it, as JSON or ``repr()`` write a
        string (``"s3cret"``, ``'s3cret'``) and as a report writes bytes
        (``0x`` and their hex). Only those quoted forms, as `error_text`: an
        unquoted value could match any part of the line. A long string a
        line shows cut (``"eyJhbGci... (322 characters)``) is replaced by the
        start of it that is shown. A number a listed key holds is replaced
        where it stands alone, if it is written in four characters or more
        (a PIN, an id): a shorter one is as likely an index or a count the
        line gives."""
        secrets = self._message_secrets(messages)
        for secret in sorted((secret for secret in secrets if isinstance(secret, str | bytes)), key=len, reverse=True):
            if isinstance(secret, str):
                for quoted, hidden in ((json.dumps(secret, ensure_ascii=False), json.dumps(REDACTED)), (repr(secret), repr(REDACTED))):
                    text = _hide_cut(text.replace(quoted, hidden), quoted)
            else:
                text = text.replace(f"0x{secret[:64].hex()}", REDACTED).replace(repr(secret), repr(REDACTED))
        for secret in secrets:
            if isinstance(secret, int | float) and len(written := json.dumps(secret)) >= _MIN_NUMBER_LENGTH:
                text = re.sub(rf"(?<![\w.+-]){re.escape(written)}(?![\w.])", REDACTED, text)
        return text

    def _message_secrets(self, messages: Iterable[Any]) -> set[str | bytes | int | float]:
        """The non-empty strings and bytes, and the numbers, listed keys hold in ``messages``,
        at any depth, under them too: iteratively, each container once (and
        once more where a listed key holds it)."""
        secrets: set[str | bytes | int | float] = set()
        if not self.message_keys:
            return secrets
        seen: set[tuple[int, bool]] = set()
        # Each value, and whether a listed key holds it, or a value that one does.
        pending: list[tuple[Any, bool]] = [(message, False) for message in messages]
        while pending:
            value, listed = pending.pop()
            if isinstance(value, Mapping | list | tuple):
                if (id(value), listed) in seen:
                    continue
                seen.add((id(value), listed))
                if isinstance(value, Mapping):
                    pending.extend((member, listed or (isinstance(key, str) and self.redacts_message_key(key))) for key, member in value.items())
                else:
                    pending.extend((member, listed) for member in value)
            elif listed and ((isinstance(value, str | bytes) and value) or (isinstance(value, int | float) and not isinstance(value, bool))):
                secrets.add(value)
        return secrets

    def _query(self, query: str) -> str:
        """``query`` with the listed parameters' values replaced, the rest (and
        its encoding) left as written."""
        if not self.query_params or "=" not in query:
            return query
        pairs = []
        for pair in query.split("&"):
            key, equals, value = pair.partition("=")
            if value and unquote_plus(key).lower() in self.query_params:
                pair = f"{key}{equals}{REDACTED}"
            pairs.append(pair)
        return "&".join(pairs)


# What a value shown cut ends with (`response_steps._shown`, `connections.shown_message`).
_CUT_MARKER = "... ("
# The shortest start of a quoted secret taken for it before a cut, its quote
# included, and the shortest number hidden where it stands alone.
_MIN_CUT_START = 4
_MIN_NUMBER_LENGTH = 4


def _hide_cut(text: str, quoted: str) -> str:
    """``text`` with ``quoted`` (a secret as a line quotes it) hidden where
    the line shows only its start, cut: before each cut marker, the longest
    start of it that the text ends with there, of `_MIN_CUT_START`
    characters or more, is replaced, its opening quote kept."""
    start = 0
    while (at := text.find(_CUT_MARKER, start)) != -1:
        shown = next((k for k in range(min(len(quoted) - 1, at), _MIN_CUT_START - 1, -1) if text.startswith(quoted[:k], at - k)), 0)
        if shown:
            text = f"{text[: at - shown]}{quoted[0]}{REDACTED}{text[at:]}"
            at += 1 + len(REDACTED) - shown
        start = at + len(_CUT_MARKER)
    return text


def _empty(value: Any) -> bool:
    """A message value that hides nothing: null, or an empty string, bytes, list or dict."""
    return value is None or (isinstance(value, str | bytes | Mapping | list | tuple) and not value)


def _redact_cookie_pair(pair: str) -> str:
    """``name=value`` keeps its name; a pair without ``=`` is a nameless
    cookie, all value."""
    name, equals, value = pair.partition("=")
    if equals:
        return f"{name}={REDACTED}" if value.strip() else pair
    stripped = pair.strip()
    return pair.replace(stripped, REDACTED, 1) if stripped else pair


def _redact_set_cookie(value: str) -> str:
    """Each cookie keeps its name and attributes (Path, Expires, ...) and
    loses its value, folded lines included."""
    pieces = _SET_COOKIE_SEPARATOR.split(value)
    # re.split with one capturing group alternates cookie, separator, cookie.
    for i in range(0, len(pieces), 2):
        pair, semicolon, attributes = pieces[i].partition(";")
        pieces[i] = f"{_redact_cookie_pair(pair)}{semicolon}{attributes}"
    return "".join(pieces)


def _redact_userinfo(match: re.Match[str]) -> str:
    """``user:[REDACTED]@``: the user name stays, as a header's name does. With
    no password the user name is the whole credential (a token passed as the
    user name, which httpx sends as Basic ``<token>:``), so it goes instead."""
    # rpartition: a raw "@" in the password belongs to the userinfo, as the
    # last "@" is where the host starts.
    userinfo, at, host = match["authority"].rpartition("@")
    user, colon, password = userinfo.partition(":")
    if password:
        return f"{match['scheme']}{user}:{REDACTED}@{host}"
    if user:
        return f"{match['scheme']}{REDACTED}{colon}@{host}"
    return match[0]


# The rules a caller gets without configuration: the ini defaults, so a report
# built outside the plugin (a test, a later exporter) is no less careful.
DEFAULT_REDACTION = Redaction(DEFAULT_REDACT_HEADERS, DEFAULT_REDACT_QUERY_PARAMS, DEFAULT_REDACT_MESSAGE_KEYS)
NO_REDACTION = Redaction()
