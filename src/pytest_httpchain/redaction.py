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
What the stage's checks and saves see is never redacted. A failure line
`Redaction.message_text` redacts shows bytes and long text as `shown_bytes` and
`cut` write them: the forms it looks for a secret in, each value taken by its
real type, as the line writes it.
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

    def cookie_value(self, header: str, value: str) -> str:
        """One cookie's value as it may be shown where it is listed apart
        from the ``Cookie`` or ``Set-Cookie`` header it came in (``header``),
        as a HAR entry's cookies are: hidden when that header is listed."""
        return REDACTED if value and self.redacts_header(header) else value

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
        string (``"s3cret"``, ``'s3cret'``) and as a report or ``repr()``
        writes bytes (``0x`` and their hex, `shown_bytes`; ``b's3cret'``).
        Only those quoted forms, as `error_text`: an unquoted value could
        match any part of the line. A number a listed key holds is replaced
        where it stands alone, if it is written in four characters or more
        (a PIN, an id): a shorter one is as likely an index or a count the
        line gives.

        Where a line shows a value cut (`cut`), the start of it that is
        shown is replaced, whether the line cut the value or the JSON around
        it: a long string (``"eyJhbGci... (322 characters)``), bytes cut in
        their hex (``"0x00112233... (203 characters)``) or in their
        ``repr()`` (``b's3cr... (``, which becomes ``'[REDACTED]... (``), a
        number cut in its digits or right after them (``4111... (253
        characters)``), where the marker's dot keeps it from standing alone.
        A start of fewer than four characters, as the line writes them, is
        as likely another value's: it stays."""
        secrets = self._message_secrets(messages)
        longest_first = sorted((secret for secret in secrets if isinstance(secret, str | bytes)), key=len, reverse=True)
        for secret in longest_first:
            if isinstance(secret, str):
                for quoted, hidden in ((json.dumps(secret, ensure_ascii=False), json.dumps(REDACTED)), (repr(secret), repr(REDACTED))):
                    text = _hide_cut(text.replace(quoted, hidden), quoted[:-1], f"{quoted[0]}{REDACTED}")
            else:
                text = text.replace(shown_bytes(secret), REDACTED).replace(repr(secret), repr(REDACTED))
        # Bytes that a cut shows the start of, once every string and bytes
        # are hidden where the line quotes them whole: one of those may hold
        # a cut marker itself, which is no place to look for a start before.
        # A line with no cut has none to look for.
        bytes_secrets = {secret for secret in secrets if isinstance(secret, bytes)}
        sizes = sorted({len(secret) for secret in bytes_secrets})
        for secret in longest_first if _CUT_MARKER in text else ():
            if isinstance(secret, bytes):
                text = _hide_cut(_hide_cut(text, shown_bytes(secret)), repr(secret), repr(REDACTED)[:-1])
                # Shorter bytes that are the start of these are hidden by
                # now, there too: what the line shows after them goes as well.
                # Only another secret's size can be one, and each is looked up
                # among the bytes alone: ASCII bytes and a string hash alike,
                # and comparing the two is a BytesWarning.
                for size in sizes:
                    if size >= min(len(secret), SHOWN_BYTES):
                        break
                    if secret[:size] in bytes_secrets:
                        text = _hide_cut(text, f"{REDACTED}{secret[size:SHOWN_BYTES].hex()}", shortest=len(REDACTED) + 1)
        numbers = [written for secret in secrets if isinstance(secret, int | float) and len(written := json.dumps(secret)) >= _MIN_NUMBER_LENGTH]
        # Hiding one number can leave the next standing alone ("1234-1234",
        # the first hidden): repeated until the line holds still, so that the
        # order the numbers are found in decides nothing. Each round that
        # changes the line replaces four or more of the characters a number is
        # written in (digits, a sign, a point, an exponent, Infinity's letters)
        # with [REDACTED], which holds none of them, so the rounds end.
        while numbers:
            before = text
            for written in numbers:
                text = re.sub(rf"{_NUMBER_START.pattern}{re.escape(written)}(?![\w.])", REDACTED, text)
                text = _hide_cut(text, written, shortest=_MIN_NUMBER_LENGTH, alone=True)
            if text == before:
                break
        return text

    def _message_secrets(self, messages: Iterable[Any]) -> list[str | bytes | int | float]:
        """The non-empty strings and bytes, and the numbers, listed keys hold in ``messages``,
        at any depth, under them too: iteratively, each container once (and
        once more where a listed key holds it).

        Each once, in the order found, which decides between two of one
        length: a string or bytes by its type and value, a number by the form
        a line writes it in. A string and bytes of the same ASCII text hash
        alike, so a set would compare them (a BytesWarning); and two equal
        numbers written each their own way (1234 and 1234.0, 0.0 and -0.0)
        would be one, and only the first found hidden. A number too long to
        write (past Python's digit limit) no line can show: it is left out.

        Each value, and each key, by its real type, which is how a line
        writes it (`response_steps._shown`), never by what it claims to be,
        and a string, bytes or number kept as an exact copy made by its base
        type's own method: one posing as another type passes isinstance, and
        read as what it claims it is misread (json.dumps refuses a real int
        claiming str, a real str claiming bytes has no ``.hex()``), or, left
        out, a real bytes subclass ``expect.match`` keeps is shown. A value
        whose real type is none of these (a proxy, a ``Mock(spec=...)``)
        holds no secret to look for: no message holds one
        (`connections.checked_message`), and pydantic takes one a template
        rendered as an exact value, or refuses it with its input hidden."""
        found: dict[tuple[object, object], str | bytes | int | float] = {}
        if not self.message_keys:
            return []
        seen: set[tuple[int, bool]] = set()
        # Each value, and whether a listed key holds it, or a value that one does.
        pending: list[tuple[Any, bool]] = [(message, False) for message in messages]
        while pending:
            value, listed = pending.pop()
            kind = type(value)
            if issubclass(kind, Mapping | list | tuple):
                if (id(value), listed) in seen:
                    continue
                seen.add((id(value), listed))
                if issubclass(kind, Mapping):
                    pending.extend((member, listed or (issubclass(type(key), str) and self.redacts_message_key(str.__str__(key)))) for key, member in value.items())
                else:
                    pending.extend((member, listed) for member in value)
            elif listed and issubclass(kind, str | bytes):
                exact = str.__str__(value) if issubclass(kind, str) else bytes.__bytes__(value)
                if exact:
                    found.setdefault((type(exact), exact), exact)
            elif listed and issubclass(kind, int | float) and kind is not bool:
                number = int.__int__(value) if issubclass(kind, int) else float.__float__(value)
                try:
                    found.setdefault(("number", json.dumps(number)), number)
                except ValueError:  # an int past the digit limit
                    pass
        return list(found.values())

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


# How many bytes of a bytes value a failure line shows (`shown_bytes`).
SHOWN_BYTES = 64
# What a value shown cut ends with (`cut`), where `_hide_cut` looks for the
# start of a secret.
_CUT_MARKER = "... ("
# The shortest start of a secret taken for it before a cut, counted as the
# line writes it (a string's quote, the ``0x`` of bytes), and the shortest
# number hidden where it stands alone, or as a start before a cut.
_MIN_CUT_START = 4
_MIN_NUMBER_LENGTH = 4
# Where a number that stands alone starts: after no word character, point or
# sign, which would make it the end of a longer number or of a word.
_NUMBER_START = re.compile(r"(?<![\w.+-])")


def shown_bytes(value: bytes) -> str:
    """Bytes as a failure line shows them: ``0x`` and the hex of their first
    `SHOWN_BYTES`, the form `Redaction.message_text` hides a listed key's
    bytes in. Whether there are more is the line's to say."""
    return f"0x{value[:SHOWN_BYTES].hex()}"


def cut(text: str, limit: int) -> str:
    """``text`` as a failure line shows it: whole, or its first ``limit``
    characters and how many it has (``"eyJhbGci... (322 characters)``), a cut
    `_hide_cut` hides the start of a secret before."""
    return text if len(text) <= limit else f"{text[:limit]}{_CUT_MARKER}{len(text)} characters)"


def _hide_cut(text: str, written: str, hidden: str = REDACTED, *, shortest: int = _MIN_CUT_START, alone: bool = False) -> str:
    """``text`` with ``written`` hidden where the line shows only its start,
    cut: before each cut marker, the longest start of it that the text ends
    with there, of ``shortest`` characters or more, is replaced by
    ``hidden``. ``written`` is a secret as a line writes it: a quoted string
    without its closing quote (``hidden`` is then its opening quote and
    ``[REDACTED]``), bytes, a number, or bytes as `Redaction.message_text`
    has left them where their own start is a shorter secret, hidden already.

    ``alone`` is for a number: its start must stand alone on its left
    (`_NUMBER_START`), as the whole number must. That start may be the
    whole number, which the marker's dot keeps from standing alone on its
    right.

    A start is looked for where the text holds the first ``shortest``
    characters of ``written``, leftmost (the longest start) first, within
    ``written``'s length of the marker: one comparison each, where trying
    every length from the longest down cost one per character of
    ``written``, at every marker."""
    if len(written) < shortest:
        return text
    head = written[:shortest]
    start = 0
    while (at := text.find(_CUT_MARKER, start)) != -1:
        shown = 0
        place = text.find(head, max(0, at - len(written)), at)
        while place != -1:
            if written.startswith(text[place:at]) and (not alone or _NUMBER_START.match(text, place)):
                shown = at - place
                break
            place = text.find(head, place + 1, at)
        if shown:
            text = f"{text[: at - shown]}{hidden}{text[at:]}"
            at += len(hidden) - shown
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
