"""Credential redaction for everything that shows an HTTP exchange.

One set of rules, shared by the report sections, the HAR export and the
failure messages that echo a header (verification and request errors): the
value of a listed header, and of a listed query parameter wherever a URL is
shown, prints as ``[REDACTED]``. Names stay visible, so a report still says
which credential was sent. Bodies are never touched: their shape is the
scenario's own, and guessing at it would hide data without making the output
safe.
"""

import re
from collections.abc import Iterable
from urllib.parse import unquote_plus

import httpx

REDACTED = "[REDACTED]"

# The ini defaults (`httpchain_redact_headers`, `httpchain_redact_query_params`):
# the credential carriers common enough that a report showing them is a leak.
DEFAULT_REDACT_HEADERS = ("Authorization", "Proxy-Authorization", "Cookie", "Set-Cookie", "X-API-Key", "API-Key", "X-Auth-Token")
DEFAULT_REDACT_QUERY_PARAMS = ("access_token", "refresh_token", "id_token", "api_key", "apikey", "client_secret", "password", "token")

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
    """Which header and query-parameter values to hide, by case-insensitive name.

    Empty sets redact nothing. An empty value is shown as it is: an empty
    ``Authorization`` hides nothing, and is worth seeing when a template
    rendered to "".
    """

    __slots__ = ("headers", "query_params")

    def __init__(self, headers: Iterable[str] = (), query_params: Iterable[str] = ()) -> None:
        self.headers = frozenset(name.lower() for name in headers)
        self.query_params = frozenset(name.lower() for name in query_params)

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
DEFAULT_REDACTION = Redaction(DEFAULT_REDACT_HEADERS, DEFAULT_REDACT_QUERY_PARAMS)
NO_REDACTION = Redaction()
