"""HTTP request/response formatting for the report sections, and a
connection stage's exchange (`format_connection_exchange`).

Header values and URL query values are shown through a `Redaction`, which
defaults to the ``httpchain_redact_*`` ini defaults; bodies are shown as sent.
A connection's messages are shown with the values of its listed message keys
redacted (`Redaction.message`).
"""

import codecs
import email.message
import json
import shlex
from typing import Any

import httpx

from pytest_httpchain.connections import LATE_WINDOW, Arrival, ConnectionExchange, count, dumped_match
from pytest_httpchain.redaction import DEFAULT_REDACTION, Redaction
from pytest_httpchain.utils import JSON_PARSE_ERRORS, request_content
from pytest_httpchain.wire_codec import MSGPACK_DECODE_ERRORS, is_msgpack_content_type, unpack_msgpack

_MAX_BODY_CHARS = 1000

# How much of a multipart part `_part_text` decodes at a time.
_DECODE_CHUNK = 1 << 20

_PRETTY_JSON = json.JSONEncoder(indent=2, ensure_ascii=False)


def _msgpack_display(value: Any) -> Any:
    if isinstance(value, bytes):
        shown = value[:64].hex()
        suffix = f"... ({len(value)} bytes)" if len(value) > 64 else ""
        return f"<bytes: {shown}{suffix}>"
    raise TypeError(f"Cannot display {type(value).__name__} as MessagePack data")


_PRETTY_MSGPACK = json.JSONEncoder(indent=2, ensure_ascii=False, default=_msgpack_display)


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
    content_type = request.headers.get("content-type", "")
    body = None
    if content is None:
        body = "<Streaming body: not captured>"
    elif (parts := _format_multipart(content, content_type)) is not None:
        body = _format_body_text(parts)
    elif content:
        if is_msgpack_content_type(content_type):
            try:
                body = _format_encoded(_PRETTY_MSGPACK, unpack_msgpack(content))
            except MSGPACK_DECODE_ERRORS:
                body = f"<Binary content: {len(content)} bytes>"
            return _message_lines(f"{request.method} {redaction.url(request.url)}", request.headers, body, redaction)
        try:
            decoded = content.decode()
        except UnicodeDecodeError:
            body = f"<Binary content: {len(content)} bytes>"
        else:
            body = _format_body_text(decoded)
            if "application/json" in content_type.lower():
                try:
                    body = _format_json(json.loads(decoded))
                except JSON_PARSE_ERRORS:
                    # A JSON body that fails to parse is malformed text, not binary.
                    pass

    return _message_lines(f"{request.method} {redaction.url(request.url)}", request.headers, body, redaction)


def _format_multipart(content: bytes, content_type: str) -> str | None:
    """A multipart body shown part by part: each part's headers, then its
    content as text, or in place of binary content its size, which would
    otherwise hide the text parts too behind one ``<Binary content>``.

    None for a body that is not multipart, or not delimited by the boundary
    its Content-Type names (a stage's own header can name another, or none),
    which is then shown as any other body is.

    Walked by offsets rather than split: the report shows ``_MAX_BODY_CHARS``
    of it, so a part past them is only found, not decoded (whether the body
    is multipart is still a question of all of it), and a part shown is
    decoded no further than it is shown (`_part_text`). Split and decoded, a
    large upload was copied four times over for a report of a thousand
    characters.
    """
    header = email.message.Message()
    header["content-type"] = content_type
    boundary = header.get_boundary()
    delimiter = f"--{boundary}".encode()
    if header.get_content_maintype() != "multipart" or not boundary or not content.startswith(delimiter):
        return None
    # A part is its delimiter's CRLF, its headers, an empty line and its
    # content, up to the CRLF before the next delimiter; after the last part
    # the delimiter is followed by "--".
    separator = b"\r\n" + delimiter
    lines: list[str] = []
    shown = 0  # the lines' length so far, a newline after each
    start = len(delimiter)
    while (end := content.find(separator, start)) != -1:
        headers_start = start + 2 if content.startswith(b"\r\n", start) else start
        headers_end = content.find(b"\r\n\r\n", headers_start, end)
        if headers_end == -1:
            return None
        # Each line of the part starts past `shown`, so its first `room`
        # characters reach past what is shown, and a part after them is not.
        if (room := _MAX_BODY_CHARS + 1 - shown) > 0:
            # Cut past any character shown: one takes 4 bytes at most, and
            # one the cut splits (3 bytes at most) is replaced.
            headers = content[headers_start : min(headers_end, headers_start + 4 * room + 8)]
            part = [f"--{boundary}", headers.decode(errors="replace").replace("\r\n", "\n"), "", _part_text(content, headers_end + 4, end, room)]
            lines += part
            shown += sum(len(line) + 1 for line in part)
        start = end + len(separator)
    if not content.startswith(b"--", start):
        return None
    lines.append(f"--{boundary}--")
    return "\n".join(lines)


def _part_text(content: bytes, start: int, end: int, room: int) -> str:
    """A multipart part's content, ``content[start:end]``, as its first
    ``room`` characters of text, or in place of binary content its size.

    Whether it is text is still a question of all of it, as for any body, so
    it is decoded throughout, but a chunk at a time and keeping no more than
    it shows: decoded whole, a large file would be copied as a whole again.
    """
    decoder = codecs.getincrementaldecoder("utf-8")()
    text = ""
    try:
        for offset in range(start, end, _DECODE_CHUNK):
            text += decoder.decode(content[offset : min(offset + _DECODE_CHUNK, end)])[: room - len(text)]
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return f"<Binary content: {end - start} bytes>"
    return text


def format_response(response: httpx.Response, redaction: Redaction = DEFAULT_REDACTION) -> str:
    """Format an httpx Response for display."""
    body = None
    if response.content:
        content_type = response.headers.get("Content-Type", "")
        codec = response.extensions.get("httpchain_response_codec", "auto")
        if codec == "msgpack" or (codec == "auto" and is_msgpack_content_type(content_type)):
            try:
                body = _format_encoded(_PRETTY_MSGPACK, unpack_msgpack(response.content))
            except MSGPACK_DECODE_ERRORS:
                body = f"<Binary content: {len(response.content)} bytes>"
        elif "application/json" in content_type.lower():
            try:
                body = _format_json(response.json())
            except JSON_PARSE_ERRORS:
                # Undecodable or too deeply nested bodies show as text too.
                body = _format_body_text(response.text)
        elif _is_textual_content_type(content_type):
            body = _format_body_text(response.text)
        else:
            body = f"<Binary content: {len(response.content)} bytes>"

    http_version = response.http_version or "HTTP/1.1"
    return _message_lines(f"{http_version} {response.status_code} {response.reason_phrase}", response.headers, body, redaction)


def _format_json(value: Any) -> str:
    """``value`` pretty-printed, rendering only as much as the report shows.

    Indentation grows with depth, so a deeply nested body renders in full to
    a size quadratic in its own (a 10 kB body 5,000 levels deep renders to
    50 MB) only to be cut to ``_MAX_BODY_CHARS``. The encoder streams, so stop
    once past the cap.
    """
    return _format_encoded(_PRETTY_JSON, value)


def _format_encoded(encoder: json.JSONEncoder, value: Any) -> str:
    chunks: list[str] = []
    length = 0
    for chunk in encoder.iterencode(value):
        chunks.append(chunk)
        length += len(chunk)
        if length > _MAX_BODY_CHARS:
            break
    return _format_body_text("".join(chunks))


def _format_body_text(text: str) -> str:
    if len(text) > _MAX_BODY_CHARS:
        return f"{text[:_MAX_BODY_CHARS]}... (truncated)"
    return text


# How many of the messages a wait considered, or saw late, a report lists: the last ones.
_MAX_LISTED_MESSAGES = 20


def _message_display(value: Any) -> Any:
    """A message value JSON has no form for: bytes as hex, as a MessagePack
    body shows them, anything else as its repr."""
    if isinstance(value, bytes | bytearray):
        return _msgpack_display(bytes(value))
    return repr(value)


_PRETTY_MESSAGE = json.JSONEncoder(indent=2, ensure_ascii=False, default=_message_display)


def _format_message(message: Any, redaction: Redaction) -> str:
    """One message, pretty JSON through ``redaction``, cut as a body is. One
    JSON cannot show (a key that is no string, a container holding itself) is
    its repr; one neither can (a value whose repr raises), a line saying so:
    the section shows the rest."""
    shown = redaction.message(message)
    try:
        return _format_encoded(_PRETTY_MESSAGE, shown)
    except Exception:
        pass
    try:
        return _format_body_text(repr(shown))
    except Exception as e:
        return f"<message cannot be shown: {type(e).__name__}>"


def _declared(match: Any) -> str:
    """An ``expect.match`` as the scenario declares it: an expression as
    written, an object of expectations as JSON, its templates unrendered."""
    if match is None:
        return "any message"
    if isinstance(match, str):
        return match
    return _format_encoded(_PRETTY_JSON, dumped_match(match))


def _arrivals(title: str, arrivals: tuple[Arrival, ...], relative_to: str, redaction: Redaction, *, late: bool = False) -> list[str]:
    """A list of messages a wait saw, the last `_MAX_LISTED_MESSAGES` of
    them, each with its place, its arrival time and why it did not match."""
    lines = [f"{title}:"]
    if len(arrivals) > _MAX_LISTED_MESSAGES:
        lines.append(f"({len(arrivals) - _MAX_LISTED_MESSAGES} earlier not shown)")
        arrivals = arrivals[-_MAX_LISTED_MESSAGES:]
    for arrival in arrivals:
        heading = f"#{arrival.seq} at {arrival.at:+.3f} s {relative_to}"
        if late:
            heading += " (late)"
        if arrival.miss is not None:
            heading += f": {redaction.message_text(arrival.miss.reason, arrival.message)}"
        lines.extend((heading, _format_message(arrival.message, redaction)))
    return lines


# How each way a wait can end reads in the report (`connections.ExpectResult`).
_RESULTS = {
    "taken": "took a message",
    "seen": "a message matched, where none should",
    "timeout": "timed out, no message matched",
    "absent": "no message matched, as expected",
    "closed": "the connection ended, as expected",
    "ended": "the connection ended while the stage waited",
}


def format_connection_exchange(exchange: ConnectionExchange, redaction: Redaction = DEFAULT_REDACTION) -> str:
    """A connection stage's report section: the connection and its session;
    what ``connect()`` returned when the stage opened it; the message sent and
    what ``send()`` returned; the wait, its match as declared, its timeout
    and how long it took, and how it ended: the message taken or, when none
    matched, the messages considered and why each did not match, those that
    arrived just after the deadline, and how many were left out; how the
    session's reader ended, if it has; and the adapter's ``debug_info()``,
    verbatim.

    Each message is shown through ``redaction`` (`Redaction.message`), as
    pretty JSON cut at the body cap, bytes as hex.
    """
    session = f", open {exchange.generation}, {exchange.session_age:.3f} s old" if exchange.generation is not None and exchange.session_age is not None else ", no session"
    lines = [f"Connection '{exchange.connection}' (adapter {exchange.adapter}){session}"]
    if exchange.opened:
        if exchange.connected is None:
            lines.append("Opened: connect() returned nothing")
        else:
            lines.extend(("Opened: connect() returned", _format_message(exchange.connected, redaction)))
    if exchange.sent is not None:
        lines.extend(("Sent:", _format_message(exchange.sent, redaction)))
        if exchange.sent_return is not None:
            lines.extend(("send() returned:", _format_message(exchange.sent_return, redaction)))
    relative_to = "after the send" if exchange.sent is not None else "from the wait"
    if exchange.mode is not None:
        declared = _declared(exchange.match)
        match exchange.mode:
            case "closed":
                lines.append("Expect: the connection to end")
            case "absent":
                lines.extend(("Expect: no message matching", declared) if "\n" in declared else (f"Expect: no message matching {declared}",))
            case _:
                lines.extend(("Expect:", declared) if "\n" in declared else (f"Expect: {declared}",))
        timing = f"Timeout: {exchange.timeout:g} s" if exchange.timeout is not None else "Timeout: not rendered"
        if exchange.waited is not None:
            timing += f", waited {exchange.waited:.3f} s"
        lines.append(timing)
        if exchange.result is None:
            lines.append("Result: the wait did not finish (see the failure message)")
        elif exchange.result == "timeout" and exchange.mode == "closed":
            lines.append("Result: timed out, the connection did not end")
        else:
            lines.append(f"Result: {_RESULTS[exchange.result]}")
        if (taken := exchange.taken) is not None:
            lines.extend((f"#{taken.seq} at {taken.at:+.3f} s {relative_to}", _format_message(taken.message, redaction)))
        if exchange.considered:
            lines.extend(_arrivals(f"Considered, not matching ({count(len(exchange.considered), 'message')})", exchange.considered, relative_to, redaction))
        if exchange.late:
            lines.extend(_arrivals(f"Late, within {LATE_WINDOW:g} s after the deadline, left queued", exchange.late, relative_to, redaction, late=True))
        if exchange.result is not None:
            left_out = [f"{count(exchange.earlier, 'message')} queued before the send"] if exchange.sent is not None else []
            left_out.append(
                f"{exchange.dropped} dropped by inbox_limit" + (f" ({exchange.unseen} of them during the wait, before it could look at them)" if exchange.unseen else "")
            )
            lines.append(f"Not considered: {', '.join(left_out)}")
    elif exchange.dropped:
        lines.append(f"Not considered: {exchange.dropped} dropped by inbox_limit")
    if exchange.end is not None and not exchange.end.after_stop:
        lines.append(f"Reader: {exchange.end.describe()}")
    if exchange.debug_info is not None:
        lines.extend(("debug_info():", exchange.debug_info))
    return "\n".join(lines)


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
    one httpx streamed, which `request_content` does not capture (the plugin
    sends none: its multipart bodies are bytes, sent with their boundary as
    any other body is); and a Digest ``Authorization``, which answered one
    challenge and is left out, with a comment to have curl answer a new one.

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
        if lowered == "content-type" and content is None and value.lower().startswith("multipart/"):
            # Its boundary is the uncaptured body's: -F writes its own.
            continue
        shown = redaction.header(name, value)
        redacted = redacted or shown != value
        # `Name:` would remove a header curl writes, `Name;` sends it empty.
        options.append(f"-H {_shell_word(f'{name}: {shown}' if shown else f'{name};')}")

    data: str | None = None
    if content is None:
        if content_type is not None and content_type.lower().startswith("multipart/"):
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
