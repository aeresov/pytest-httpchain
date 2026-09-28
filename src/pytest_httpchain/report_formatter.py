"""HTTP request/response formatting for the report sections.

Header values and URL query values are shown through a `Redaction`, which
defaults to the ``httpchain_redact_*`` ini defaults; bodies are shown as sent.
"""

import json
import shlex

import httpx

from pytest_httpchain.redaction import DEFAULT_REDACTION, Redaction
from pytest_httpchain.utils import request_content

_MAX_BODY_CHARS = 1000


def _is_textual_content_type(content_type: str) -> bool:
    """True when the Content-Type looks like text we can safely display."""
    ct = content_type.lower()
    return ct.startswith("text/") or "json" in ct or "xml" in ct or "x-www-form-urlencoded" in ct


def _message_lines(start_line: str, headers: httpx.Headers, body: str | None, redaction: Redaction) -> str:
    """Assemble one HTTP message: start line, headers, blank line, optional body."""
    lines = [start_line, *(f"{name}: {value}" for name, value in redaction.header_items(headers)), ""]
    if body is not None:
        lines.append(body)
    return "\n".join(lines)


def format_request(request: httpx.Request, redaction: Redaction = DEFAULT_REDACTION) -> str:
    """Format an httpx Request for display."""
    content = request_content(request)
    body = None
    if content is None:
        body = "<Streaming body (e.g. multipart file upload): consumed on send, not captured>"
    elif content:
        try:
            decoded = content.decode()
        except UnicodeDecodeError:
            body = f"<Binary content: {len(content)} bytes>"
        else:
            # A JSON body that fails to parse is malformed text, not binary.
            if "application/json" in request.headers.get("content-type", ""):
                try:
                    decoded = json.dumps(json.loads(decoded), indent=2, ensure_ascii=False)
                except json.JSONDecodeError:
                    pass
            body = _format_body_text(decoded)

    return _message_lines(f"{request.method} {redaction.url(request.url)}", request.headers, body, redaction)


def format_response(response: httpx.Response, redaction: Redaction = DEFAULT_REDACTION) -> str:
    """Format an httpx Response for display."""
    body = None
    if response.content:
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            try:
                body = _format_body_text(json.dumps(response.json(), indent=2, ensure_ascii=False))
            except (json.JSONDecodeError, UnicodeDecodeError):
                # .json() raises UnicodeDecodeError too, for undecodable bytes.
                body = _format_body_text(response.text)
        elif _is_textual_content_type(content_type):
            body = _format_body_text(response.text)
        else:
            body = f"<Binary content: {len(response.content)} bytes>"

    http_version = response.http_version or "HTTP/1.1"
    return _message_lines(f"{http_version} {response.status_code} {response.reason_phrase}", response.headers, body, redaction)


def _format_body_text(text: str) -> str:
    if len(text) > _MAX_BODY_CHARS:
        return f"{text[:_MAX_BODY_CHARS]}... (truncated)"
    return text


# The headers curl writes itself: Host from the URL, Content-Length and
# Transfer-Encoding for the body it sends, Connection for its own connection.
# And Accept-Encoding, with `--compressed`, where it is httpx's own
# (`_DEFAULT_CODINGS`).
_CURL_WRITES = frozenset({"host", "content-length", "transfer-encoding", "connection"})

# The codings of the Accept-Encoding httpx sends unless told otherwise: gzip
# and deflate, then br and zstd where their decoders are installed. curl's
# --compressed asks for those of them it was built to decode.
_DEFAULT_CODINGS = frozenset({"gzip", "deflate", "br", "zstd"})

# An Authorization header answering a Digest challenge: its response is
# computed from that challenge's nonce, so it is good for that request alone.
_DIGEST_SCHEME = "digest "

# The longest textual body a command spells out. Not `_MAX_BODY_CHARS`: a
# command cannot cut a body short, or it would send another one.
_MAX_CURL_BODY_CHARS = 10_000


def format_curl(request: httpx.Request, redaction: Redaction = DEFAULT_REDACTION) -> str:
    """A curl command sending ``request`` again, for a POSIX shell: its method,
    its URL and headers as `format_request` shows them, and its body.

    Every value (the URL, each header, a body) is quoted with ``shlex.quote``,
    in single quotes even where it would leave a word bare, so that quotes,
    newlines and non-ASCII text in it reach curl as they are. There is always
    ``--compressed`` beside an ``Accept-Encoding``, for curl to decode the
    answer as httpx does, and it stands for httpx's own list of codings
    (`_is_default_accept_encoding`); one the request set is kept as set,
    which curl then sends in place of its own. A value the redaction hides stays
    ``[REDACTED]``, and a comment above the command says to fill it in: a
    report is no place for a working credential. What the command cannot hold
    gets a comment too: a binary body or one longer than
    `_MAX_CURL_BODY_CHARS`, which the command reads from a file instead, and
    one httpx streamed (a multipart upload), which `request_content` does not
    capture; and a Digest ``Authorization``, which answered one challenge and
    is left out, with a comment to have curl answer a new one.

    The client's settings are not part of a request, so none are given: no
    TLS options (the command checks certificates as curl does by default), no
    proxy (curl reads the proxy environment variables, not ``client.proxy``).
    """
    notes: list[str] = []
    shown_url = redaction.url(request.url)
    redacted = shown_url != str(request.url)
    content = request_content(request)
    content_type = request.headers.get("content-type")

    options: list[str] = []
    if any(bracket in shown_url for bracket in "[]{}"):
        # curl reads [1-3] and {a,b} in a URL as ranges to request each of:
        # a query's filter[id]=1, or the [REDACTED] above.
        options.append("--globoff")

    compressed = False
    for name, value in request.headers.multi_items():
        lowered = name.lower()
        if lowered == "host":
            # Unless it is a Host the request set itself, for a virtual host.
            if value == request.url.netloc.decode("ascii"):
                continue
        elif lowered == "content-length":
            # curl writes it for the body it sends, or the one a comment says
            # to add; for none at all (a bodyless POST's 0) it writes none.
            if content != b"":
                continue
        elif lowered in _CURL_WRITES:
            continue
        if lowered == "accept-encoding":
            # httpx decodes whatever encoding the answer comes in; curl only
            # with --compressed, which asks for its own list unless -H gives one.
            compressed = True
            if _is_default_accept_encoding(value):
                continue
        if lowered == "authorization" and value[: len(_DIGEST_SCHEME)].lower() == _DIGEST_SCHEME:
            notes.append("The Digest Authorization answered one challenge and is left out: add --digest -u 'user:password' to answer a new one.")
            continue
        if lowered == "content-type" and content is None and value.startswith("multipart/"):
            # Its boundary is the uncaptured body's: -F writes its own.
            continue
        shown = redaction.header(name, value)
        redacted = redacted or shown != value
        # `Name:` would remove a header curl writes, `Name;` sends it empty.
        options.append(f"-H {_shell_word(f'{name}: {shown}' if shown else f'{name};')}")

    data: str | None = None
    if content is None:
        if content_type is not None and content_type.startswith("multipart/"):
            notes.append("The multipart body was not captured: add each part with -F 'name=@file'.")
        else:
            notes.append("The streamed body was not captured: add it with --data-binary @file.")
    elif content:
        text = _curl_text(content)
        if text is None:
            notes.append(f"The body is {len(content)} bytes of binary data: save it as body.bin to send it.")
            data = "--data-binary @body.bin"
        elif len(text) > _MAX_CURL_BODY_CHARS:
            notes.append(f"The body, {len(text)} characters, is too long to show: save it as body.txt to send it.")
            data = "--data-binary @body.txt"
        else:
            data = f"--data-raw {_shell_word(text)}"
    if data is not None and content_type is None:
        # Given a body without one, curl labels it a form.
        options.append(f"-H {_shell_word('Content-Type:')}")
    if compressed:
        options.append("--compressed")
    if data is not None:
        options.append(data)

    if redacted:
        notes.insert(0, "[REDACTED] stands for a value this report hides: fill it in before running.")
    # -X HEAD leaves curl waiting for a body; --head refuses one to send.
    method = "--head" if request.method == "HEAD" and data is None else f"-X {shlex.quote(request.method)}"
    command = " \\\n  ".join([f"curl {method} {_shell_word(shown_url)}", *options])
    return "\n".join([*(f"# {note}" for note in notes), command])


def _is_default_accept_encoding(value: str) -> bool:
    """Whether an Accept-Encoding is the one httpx sends on its own: which of
    `_DEFAULT_CODINGS` it lists depends on what is installed, gzip and deflate
    always among them. Any other was set on purpose (``identity`` to test an
    uncompressed answer, ``br`` alone, an empty one) and is sent as set:
    `--compressed` would ask for every coding curl decodes instead."""
    codings = [coding.strip().lower() for coding in value.split(",")]
    return len(codings) == len(set(codings)) and {"gzip", "deflate"} <= set(codings) <= _DEFAULT_CODINGS


def _curl_text(content: bytes) -> str | None:
    """A body as the text a shell argument can carry, or None: not UTF-8, or
    holding a NUL, which ends a C string and so an argument."""
    try:
        text = content.decode()
    except UnicodeDecodeError:
        return None
    return None if "\x00" in text else text


def _shell_word(text: str) -> str:
    """``text`` as one single-quoted shell word."""
    quoted = shlex.quote(text)
    # Bare only when every character is safe outside quotes, so inside too.
    return quoted if quoted.startswith("'") else f"'{quoted}'"
