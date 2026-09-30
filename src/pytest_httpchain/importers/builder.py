"""A starter scenario from recorded requests, shared by ``import har`` and
``import curl``.

The readers (`importers.har`, `importers.curl`) turn their source into
`RecordedRequest`s, each the request as it was sent; `build_scenario` writes
them in the scenario dialect, one stage each, in order:

- the origin all of them share becomes ``client.base_url``, and their URLs
  relative to it; a query string becomes ``params`` where a mapping sends it
  as it was sent (see `_mappable`), and stays in the URL otherwise;
- headers are kept as recorded, less what the client writes itself
  (`TRANSPORT_HEADERS`, HTTP/2 pseudo-headers, a ``Host`` naming the URL's
  host) and those a body or auth form stands for; with several requests, a
  header every one of them sends identically moves to ``client.headers``;
- a body becomes the form its Content-Type names: ``json``, ``form``,
  ``multipart``, else the raw ``text`` (``base64`` or ``binary`` where the
  source gave bytes or a file); a JSON or form body a mapping would not send
  as it was sent is ``text`` too;
- Basic and Bearer credentials become the ``basic`` and ``bearer`` auth
  shorthands, the scenario's ``auth`` when every request carries the same.

No secret is written: an Authorization value, a password, the cookies, and a
header, query parameter, form field or JSON member that the redaction rules
hide (`redaction.DEFAULT_REDACTION`) become ``{{ name }}`` placeholders, each
a scenario ``vars`` entry read from the environment (``{{ env('NAME') }}``,
the docs' way of passing a secret), and each a whole value where it can be,
so that one left unset fails the stage rather than send the text ``None``.
Inside text kept as written (a query in the URL, a body sent as text), a
placeholder is a call that fails the stage too (``{{ quote(name) }}``), or
sends ``null`` as a JSON body member does (``{{ json_dumps(name) }}``).
`ImportResult.placeholders` lists them for the user to fill in.

What is written literally is escaped (`templates.escape`), so a recorded
``{{`` is sent as recorded, not rendered as a template; ``closed`` where a
placeholder follows it on its line, which an escape would run on into. No
mapping written
holds a recorded name that is a reference key (`jsonref.REF_KEYS`: ``$ref``,
``$include``, ``$merge``), which the file's loader would resolve: such a query
stays in the URL, such a JSON or form body is text, a header's name is
written ``$Ref`` (its case says nothing), and a multipart part of that name is
left out with a note.
"""

import base64
import binascii
import email.parser
import email.policy
import json
import keyword
import math
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, unquote_plus, urlsplit

from pytest_httpchain.errors import HttpChainError
from pytest_httpchain.jsonref import REF_KEYS
from pytest_httpchain.redaction import DEFAULT_REDACTION, REDACTED, Redaction
from pytest_httpchain.templates import TEMPLATE_BUILTINS, contains_template, escape, unescape
from pytest_httpchain.validation import ValidateResult, validate_scenario


class ImportSourceError(HttpChainError):
    """What an import reads cannot become a scenario: not a HAR file, a curl
    command without a URL, nothing left once filtered. Its message is the
    whole explanation, which the command line prints as its ``error:``."""


@dataclass(frozen=True)
class Part:
    """One part of a multipart body as recorded: a text field (``value``),
    or a file, read from ``path`` (as written, relative to wherever the
    command ran) or recorded as text (``content``) or bytes (``base64``). A
    filename of ``""`` is a part sent without one."""

    name: str
    value: str | None = None
    path: str | None = None
    content: str | None = None
    base64: str | None = None
    filename: str | None = None
    content_type: str | None = None


@dataclass(frozen=True)
class TextData:
    """A body recorded as text, which its Content-Type header tells how to read."""

    text: str


@dataclass(frozen=True)
class Base64Data:
    """A body recorded as bytes that are not text, base64-encoded."""

    data: str


@dataclass(frozen=True)
class FileData:
    """A body read from a file as it is (curl's ``--data-binary @file``)."""

    path: str


@dataclass(frozen=True)
class PartsData:
    """A multipart/form-data body recorded part by part."""

    parts: tuple[Part, ...]


type RecordedBody = TextData | Base64Data | FileData | PartsData


@dataclass
class RecordedRequest:
    """A request as it was sent, in the terms a scenario needs.

    ``headers`` are as recorded, in order, a repeated name repeated.
    ``cookies`` are the cookies sent, beside any ``Cookie`` header (curl's
    ``-b``, a HAR entry's cookie list). ``user`` is curl's ``-u``: a user
    name and a password, None where curl would have asked for it. ``status``
    is the status the response had, where one was recorded.
    """

    method: str
    url: str
    headers: list[tuple[str, str]] = field(default_factory=list)
    cookies: list[tuple[str, str]] = field(default_factory=list)
    body: RecordedBody | None = None
    user: tuple[str, str | None] | None = None
    digest: bool = False
    bearer: str | None = None
    follow_redirects: bool = True
    verify_tls: bool = True
    timeout: float | None = None
    status: int | None = None


@dataclass
class Placeholder:
    """A secret left out of the scenario: the ``vars`` entry that stands for
    it, the environment variable that entry reads, what it was and which
    stages use it."""

    var: str
    env: str
    what: str
    stages: list[str] = field(default_factory=list)


@dataclass
class ImportResult:
    """The scenario, the placeholders to fill in, the files it reads (relative
    to its own directory once written) and notes on what was left out."""

    scenario: dict[str, Any]
    placeholders: list[Placeholder]
    files: list[str]
    notes: list[str]


# Headers the client writes itself, or that belong to one connection (the
# hop-by-hop ones): the body's length, the connection's, the codings httpx
# decodes (Accept-Encoding, which it sends on its own), and a proxy's
# credentials, which go in client.proxy's URL. `Host` too, unless it names
# another host than the URL (a virtual host), which is then kept.
TRANSPORT_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "proxy-connection",
        "transfer-encoding",
        "te",
        "trailer",
        "upgrade",
        "accept-encoding",
        "proxy-authorization",
    }
)

_DEFAULT_PORTS = {"http": 80, "https": 443}

# The media types a body form sets itself, whose Content-Type header then goes
# without saying: httpx labels a `json` body application/json and a `form`
# body application/x-www-form-urlencoded, exactly those.
_JSON = "application/json"
_FORM = "application/x-www-form-urlencoded"
_MULTIPART = "multipart/form-data"

# Characters a form encoding never leaves bare, but for a space, which curl's
# -d sends as typed (`scope=read write`) and a server reads as one: text
# holding one (a JSON document sent with curl's default form type) is no form,
# and kept as text.
_FORM_TEXT = re.compile(r"[^\t\n\r\f\v\"{}<>\\^`|]*")

# A stage name's words: method and path segments, lowercase and underscored.
_NAME_WORD = re.compile(r"[^0-9A-Za-z]+")
_MAX_STAGE_NAME = 60

_NOT_JSON = object()

# How deep a JSON body the import writes as `json`: deeper, it is the text it
# was, sent as recorded. The walks below spend a frame or two per level, and
# so do the loader and the model that read the scenario back.
_MAX_JSON_DEPTH = 100

# The order a stage's request is written in: what is sent where, then what it
# says, then how.
_REQUEST_KEYS = ("method", "url", "params", "headers", "auth", "body", "timeout", "allow_redirects")


def media_type(content_type: str | None) -> str:
    """A Content-Type's media type, lowercase and without parameters."""
    return (content_type or "").split(";", 1)[0].strip().lower()


def parse_cookie_header(value: str) -> list[tuple[str, str]]:
    """A ``Cookie`` header's pairs, in order; a piece without ``=`` is a
    nameless cookie, all value (as `redaction` reads one)."""
    pairs = []
    for piece in value.split(";"):
        name, equals, cookie = piece.strip().partition("=")
        if equals:
            pairs.append((name.strip(), cookie.strip()))
        elif name:
            pairs.append(("", name))
    return pairs


def _is_json_type(media: str) -> bool:
    return media == _JSON or media.endswith("+json")


def _loads_json(text: str) -> Any:
    """``text`` as a JSON value, or `_NOT_JSON`: not JSON, a duplicate member
    (one would be lost), or ``NaN`` and the infinities, which no JSON body
    holds, a number too large for a float among them."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        members = dict(items)
        if len(members) != len(items):
            raise ValueError("duplicate member")
        return members

    def constant(name: str) -> Any:
        raise ValueError(f"{name} is not JSON")

    def number(literal: str) -> float:
        # 1e400 is JSON, but a float past the largest is infinity, which is
        # not: httpx would refuse to send it.
        value = float(literal)
        if not math.isfinite(value):
            raise ValueError(f"{literal} is out of range")
        return value

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)
    except (ValueError, RecursionError):
        return _NOT_JSON


def _form_pairs(text: str) -> list[tuple[str, str]] | None:
    """A form-encoded body's fields, or None for text that is not one: a
    character a form encoding never leaves bare, a piece without ``=``, an
    empty piece, or an escape that is not UTF-8."""
    if not text or not _FORM_TEXT.fullmatch(text):
        return None
    try:
        return parse_qsl(text, keep_blank_values=True, strict_parsing=True, errors="strict")
    except (ValueError, UnicodeDecodeError):
        return None


def multipart_parts(content: bytes, content_type: str) -> tuple[Part, ...] | None:
    """A multipart/form-data body taken apart, or None for one that is not
    well-formed (no part, a part that is no named form-data, a boundary that
    does not delimit it). A part's text that is not UTF-8 is kept as base64."""
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(b"Content-Type: " + content_type.encode("latin-1", "replace") + b"\r\n\r\n" + content)
    if message.defects or not message.is_multipart():
        return None
    parts = []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if part.get_content_disposition() != "form-data" or not isinstance(name, str) or part.defects:
            return None
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            return None
        filename = part.get_filename()
        declared_type = part.get("content-type")
        try:
            text = payload.decode()
        except UnicodeDecodeError:
            parts.append(Part(name, base64=base64.b64encode(payload).decode("ascii"), filename=filename or "", content_type=declared_type))
            continue
        if filename is None and declared_type is None:
            parts.append(Part(name, value=text))
        else:
            parts.append(Part(name, content=text, filename=filename or "", content_type=declared_type))
    return tuple(parts) or None


def _base64_bytes(data: str) -> bytes | None:
    """The bytes base64 text encodes, or None for text that is no base64
    (which the scenario then refuses, naming it)."""
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return None


def _joined_text(pieces: list[tuple[str, str | None]], separator: str) -> str:
    """Recorded text and placeholders as one string: each piece's text,
    escaped, then its template, if any, the pieces joined by
    ``separator``. Text a template follows on its line is escaped
    ``closed``, since an escape would otherwise run on into the template:
    one before any of the templates, since a template of a later piece may
    follow it on the same line too."""
    written = []
    last_template = max((index for index, (_, template) in enumerate(pieces) if template is not None), default=-1)
    for index, (text, template) in enumerate(pieces):
        written.append(escape(text, closed=index <= last_template) + (template or ""))
    return separator.join(written)


def _var_name(base: str) -> str:
    """``base`` as a variable name a template can read: lowercase words
    joined by ``_``, never a keyword, a built-in's name or ``response``,
    which would shadow what the engine provides."""
    name = _NAME_WORD.sub("_", base).strip("_").lower() or "secret"
    if name[0].isdigit():
        name = f"v_{name}"
    if keyword.iskeyword(name) or keyword.issoftkeyword(name) or name in TEMPLATE_BUILTINS or name == "response":
        name = f"{name}_value"
    return name


def _origin(scheme: str, netloc: str) -> str:
    """``scheme://host[:port]`` as written, without the userinfo."""
    return f"{scheme}://{netloc.rpartition('@')[2]}"


def _host_header_matches(value: str, scheme: str, netloc: str) -> bool:
    """Whether a ``Host`` header names the URL's own host, as httpx would
    write it (the default port left out), so it goes without saying."""
    host = netloc.rpartition("@")[2].lower()
    default = f":{_DEFAULT_PORTS.get(scheme, '')}"
    return value.strip().lower().removesuffix(default) == host.removesuffix(default)


def _grouped(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Pairs as a mapping in first-seen order, a repeated name's values as a
    list (sent as the name repeated, as httpx sends a list)."""
    grouped: dict[str, list[Any]] = {}
    for name, value in pairs:
        grouped.setdefault(name, []).append(value)
    return {name: values[0] if len(values) == 1 else values for name, values in grouped.items()}


def _mappable(names: list[str]) -> bool:
    """Whether fields of these names, in this order, are sent as they were
    from a mapping (`_grouped`): none of them a reference key (`REF_KEYS`),
    which the scenario file's loader would resolve, and the fields of a
    repeated name side by side, since a mapping sends a name's values
    together, at its first place (``a=1&b=2&a=3`` would go out as
    ``a=1&a=3&b=2``)."""
    seen: set[str] = set()
    previous = None
    for name in names:
        if name in REF_KEYS:
            return False
        if name != previous:
            if name in seen:
                return False
            seen.add(name)
            previous = name
    return True


def _decoded_pairs(pieces: list[str]) -> list[tuple[str, str]] | None:
    """``name=value`` pieces decoded, or None where one's escapes are not
    UTF-8, which a mapping would not send back as they were. A piece without
    ``=`` is an empty value."""
    pairs = []
    for piece in pieces:
        name, _, value = piece.partition("=")
        try:
            pairs.append((unquote_plus(name, errors="strict"), unquote_plus(value, errors="strict")))
        except UnicodeDecodeError:
            return None
    return pairs


def _json_shape(value: Any) -> tuple[int, str | None]:
    """How deep a JSON value is nested, and the first reference key
    (`REF_KEYS`) any of its objects has as a member's name, or None. A walk
    of its own, with no frame per level, for a value of any depth."""
    depth, found = 0, None
    pending: list[tuple[Any, int]] = [(value, 0)]
    while pending:
        item, level = pending.pop()
        depth = max(depth, level)
        if isinstance(item, dict):
            if found is None:
                found = next((name for name in item if name in REF_KEYS), None)
            pending.extend((member, level + 1) for member in item.values())
        elif isinstance(item, list):
            pending.extend((member, level + 1) for member in item)
    return depth, found


def _header_name(name: str) -> str:
    """A header's name as written: a reference key (`REF_KEYS`), which the
    scenario file's loader would resolve, with its first letter capitalized
    (``$Ref``), since a header name's case says nothing."""
    return name[:2].upper() + name[2:] if name in REF_KEYS else name


class _Builder:
    """One import's state: the placeholders so far, and the files and notes."""

    def __init__(self, redaction: Redaction) -> None:
        self.redaction = redaction
        # By (the name it was found under, value): one secret every stage
        # sends is one placeholder, and so is each name's value a redacted
        # source hid, which is `[REDACTED]` for every secret alike.
        self._secrets: dict[tuple[str, str], Placeholder] = {}
        self._vars: set[str] = set()
        self._stage_names: set[str] = set()
        # The last suffix taken per name, so that a request repeated n times
        # (a page polling) finds its next free name at once, not in n tries.
        self._suffixes: dict[str, int] = {}
        self.files: list[str] = []
        self.notes: list[str] = []

    def _unique(self, name: str, taken: set[str]) -> str:
        """``name``, or ``name_2``, ``name_3``... the first not ``taken``,
        which it then is."""
        counter = self._suffixes.get(name, 1)
        unique = name if counter == 1 else f"{name}_{counter}"
        while unique in taken:
            counter += 1
            unique = f"{name}_{counter}"
        self._suffixes[name] = counter
        taken.add(unique)
        return unique

    # --- placeholders ---

    def placeholder(self, base: str, value: str, what: str, stage: str) -> str:
        """The ``vars`` entry standing for ``value`` in ``stage``."""
        name = _var_name(base)
        entry = self._secrets.get((name, value))
        if entry is None:
            var = self._unique(name, self._vars)
            entry = self._secrets[(name, value)] = Placeholder(var, var.upper(), what)
        # Stages are built in order, so one already listed is the last.
        if not entry.stages or entry.stages[-1] != stage:
            entry.stages.append(stage)
        return entry.var

    def secret(self, base: str, value: str, what: str, stage: str) -> str:
        """The whole-value placeholder standing for ``value`` in ``stage``."""
        return "{{ " + self.placeholder(base, value, what, stage) + " }}"

    @property
    def placeholders(self) -> list[Placeholder]:
        return list(self._secrets.values())

    def is_secret_field(self, name: str, value: str) -> bool:
        """Whether a named value (query parameter, form or multipart field)
        is a secret: not empty, and named as a query parameter the
        redaction rules hide."""
        return bool(value) and self.redaction.redacts_query_param(name)

    def field_value(self, name: str, value: str, what: str, stage: str) -> str:
        """A multipart field's value: a placeholder for a secret one (an
        unset one fails the stage: a field is no null), else the value,
        escaped."""
        if self.is_secret_field(name, value):
            return self.secret(name, value, f"{what} {name!r}", stage)
        return escape(value)

    def fields_text(self, text: str, what: str, stage: str) -> str:
        """``&``-separated fields kept as the text they were (a query left in
        the URL, a form body sent as text), escaped, with the value of each
        field the redaction rules hide a placeholder URL-encoded as it
        renders (``quote``, which an unset one fails: None is no text).
        The text before a placeholder is escaped ``closed``: a recorded
        ``{{`` that no ``}}`` follows would otherwise cover the placeholder,
        sent then as its text."""
        pieces: list[tuple[str, str | None]] = []
        for piece in text.split("&"):
            raw_name, equals, raw_value = piece.partition("=")
            name = unquote_plus(raw_name)
            if equals and raw_value and self.redaction.redacts_query_param(name):
                var = self.placeholder(name, unquote_plus(raw_value), f"{what} {name!r}", stage)
                pieces.append((raw_name + "=", "{{ quote(" + var + ") }}"))
            else:
                pieces.append((piece, None))
        return _joined_text(pieces, "&")

    def hidden_header(self, name: str, value: str) -> str | None:
        """What a header stands for as a secret, or None for one the
        redaction rules show as it is: they hide a listed header's whole
        value, and the credentials in a URL-valued one's URL (a Referer's
        ``?access_token=``). A ``[REDACTED]`` counts as the value it hid."""
        probe = value.replace(REDACTED, "x")
        if self.redaction.header(name, probe) == probe:
            return None
        if self.redaction.redacts_header(name):
            return f"the {name} header"
        return f"the {name} header, whose URL holds a credential"

    # --- stage names ---

    def stage_name(self, method: str, path: str) -> str:
        """``<method>_<path segments>``, unique: ``get_users_42``, then
        ``get_users_42_2`` for the same request again."""
        words = [word for word in _NAME_WORD.split(unquote(path).lower()) if word] or ["root"]
        name = "_".join([_NAME_WORD.sub("_", method.lower()).strip("_") or "request", *words])[:_MAX_STAGE_NAME].rstrip("_")
        return self._unique(name, self._stage_names)

    # --- auth ---

    def credentials(self, scheme: str, user: str, password: str | None, stage: str) -> dict[str, Any]:
        """``{scheme: {username, password}}`` with the password a
        placeholder. Without a password curl asks for one, so it is still a
        placeholder; with an empty one the user name is the credential (an
        API key passed as ``-u key:``, or as a URL's ``<key>@``), so the user
        name is instead. Both empty (a URL's ``@host``) hide nothing."""
        if password == "" and not user:
            return {scheme: {"username": "", "password": ""}}
        if password == "" and user:
            username = self.secret("api_user", user, "the user name of credentials without a password", stage)
            return {scheme: {"username": username, "password": ""}}
        # A password curl would ask for has no value to tell apart by: one
        # placeholder per user.
        key = password if password is not None else f"\0{user}"
        return {scheme: {"username": escape(user), "password": self.secret("api_password", key, f"the password of user {user!r}", stage)}}

    def authorization(self, value: str, stage: str) -> tuple[dict[str, Any] | None, str | None]:
        """An Authorization header as ``(auth, header value)``: the basic or
        bearer shorthand, or for any other scheme the header kept with its
        value a placeholder."""
        scheme, _, credentials = value.strip().partition(" ")
        credentials = credentials.strip()
        if scheme.lower() == "bearer" and credentials:
            return {"bearer": self.secret("api_token", credentials, "the bearer token", stage)}, None
        if scheme.lower() == "basic" and credentials:
            try:
                user, colon, password = base64.b64decode(credentials, validate=True).decode().partition(":")
            except (binascii.Error, ValueError):
                colon = ""
            if colon:
                return self.credentials("basic", user, password, stage), None
        return None, self.secret("authorization", value, "the Authorization header", stage)

    # --- bodies ---

    def _json(self, value: Any, text: Callable[[str], str], secret: Callable[[str, str | int | float], Any]) -> Any:
        """A JSON body's value, each string passed through ``text``, and each
        string or number held by a member the redaction rules would hide as a
        query parameter (``password``, ``token``, in a list under it too)
        replaced by ``secret(name, value)``. An object under such a name is
        judged by its own members' names: in a JSON Schema document,
        ``"password": {"type": "string"}`` holds none."""

        def walk(item: Any, name: str | None) -> Any:
            if isinstance(item, dict):
                return {key: walk(member, key if self.redaction.redacts_query_param(key) else None) for key, member in item.items()}
            if isinstance(item, list):
                return [walk(member, name) for member in item]
            if name is not None and ((isinstance(item, str) and item) or (isinstance(item, int | float) and not isinstance(item, bool))):
                return secret(name, item)
            return text(item) if isinstance(item, str) else item

        return walk(value, None)

    def json_value(self, value: Any, stage: str) -> Any:
        """A ``json`` body: strings escaped, and a secret member's string a
        placeholder, its number one read as JSON (``json_loads``), which an
        unset one fails, so that it is sent as the number it was."""

        def secret(name: str, member: str | int | float) -> str:
            if isinstance(member, str):
                return self.secret(name, member, f"JSON body member {name!r}", stage)
            return "{{ json_loads(" + self.placeholder(name, json.dumps(member), f"JSON body member {name!r}", stage) + ") }}"

        return self._json(value, escape, secret)

    def json_text(self, text: str, value: Any, stage: str) -> str:
        """A JSON body as ``text``, for one a ``json`` body cannot hold (a
        member named ``$ref``): the text as recorded, or, with secrets in it,
        the value written again as JSON with each secret a placeholder
        encoding it as JSON as it renders (``json_dumps``, which sends an
        unset one as ``null``, as a ``json`` body does; ``json_loads`` for a
        number, which fails)."""
        # A string no recorded one is: a NUL is `\u0000` in the text.
        marker = "\x00"
        while json.dumps(marker)[1:-1] in text:
            marker += "\x00"
        templates: dict[str, str] = {}

        def secret(name: str, member: str | int | float) -> str:
            sentinel = f"{marker}{len(templates)}{marker}"
            if isinstance(member, str):
                var = self.placeholder(name, member, f"JSON body member {name!r}", stage)
                templates[json.dumps(sentinel)] = "{{ json_dumps(" + var + ") }}"
            else:
                var = self.placeholder(name, json.dumps(member), f"JSON body member {name!r}", stage)
                templates[json.dumps(sentinel)] = "{{ json_loads(" + var + ") }}"
            return sentinel

        walked = self._json(value, lambda string: string, secret)
        if not templates:
            return escape(text)
        # The text between the placeholders escaped piece by piece, each
        # before one closed: a `}}` of the JSON after a placeholder (an
        # object's end) closes no escape before it.
        dumped = json.dumps(walked, ensure_ascii=False)
        pieces: list[tuple[str, str | None]] = []
        start = 0
        for found in re.finditer("|".join(re.escape(quoted) for quoted in templates), dumped):
            pieces.append((dumped[start : found.start()], templates[found.group(0)]))
            start = found.end()
        pieces.append((dumped[start:], None))
        return _joined_text(pieces, "")

    def multipart(self, parts: tuple[Part, ...], stage: str) -> dict[str, Any]:
        """``{"fields": ..., "files": ...}``: a text field without a type or
        filename is a field, anything else a file object (a path alone
        where that says all). A secret's text, a field's or a file object's
        ``content`` or ``base64`` (a field sent with a type is one), is a placeholder,
        which fails the stage unset (a field or a file's only source is no
        null). A part named as a reference key cannot be written, and is
        left out with a note."""
        fields: list[tuple[str, Any]] = []
        files: list[tuple[str, Any]] = []
        for part in parts:
            if part.name in REF_KEYS:
                self.notes.append(f"Stage {stage!r}: left out the multipart part named {part.name!r}, which the scenario file would read as a reference ($ref, $include, $merge)")
                continue
            if part.value is not None and part.filename is None and part.content_type is None:
                fields.append((part.name, self.field_value(part.name, part.value, "multipart field", stage)))
                continue
            spec: dict[str, Any] = {}
            if part.path is not None:
                self.files.append(part.path)
                spec["path"] = escape(part.path)
            elif part.base64 is not None:
                # Bytes that are no text (a password sent in another
                # encoding) are as secret as text: the placeholder holds
                # them base64-encoded, as recorded.
                if self.is_secret_field(part.name, part.base64):
                    spec["base64"] = self.secret(part.name, part.base64, f"multipart part {part.name!r}, base64-encoded", stage)
                else:
                    spec["base64"] = part.base64
            elif self.is_secret_field(part.name, content := part.value if part.value is not None else part.content or ""):
                spec["content"] = self.secret(part.name, content, f"multipart part {part.name!r}", stage)
            else:
                spec["content"] = escape(content)
            if part.filename is not None:
                spec["filename"] = escape(part.filename)
            elif part.value is not None:
                # A text field sent with a type has no filename.
                spec["filename"] = ""
            if part.content_type is not None:
                spec["content_type"] = part.content_type
            files.append((part.name, spec["path"] if list(spec) == ["path"] else spec))
        body: dict[str, Any] = {}
        if fields or not files:
            body["fields"] = _grouped(fields)
        if files:
            body["files"] = _grouped(files)
        return body

    def body(self, recorded: RecordedBody | None, content_type: str | None, stage: str) -> tuple[dict[str, Any] | None, bool]:
        """The request's ``body``, and whether its Content-Type header goes
        without saying: the form sets that very type (see `_JSON`), or a
        multipart body's own boundary replaces the one recorded."""
        media = media_type(content_type)
        exact = (content_type or "").strip().lower()
        match recorded:
            case None:
                return None, False
            case FileData(path=path):
                self.files.append(path)
                return {"binary": escape(path)}, False
            case Base64Data(data=data):
                # A multipart body with a part that is no text (a file's
                # bytes) is recorded whole as base64: taken apart, its
                # fields are judged as a text one's are.
                if media == _MULTIPART and content_type and (content := _base64_bytes(data)) is not None and (parts := multipart_parts(content, content_type)) is not None:
                    return {"multipart": self.multipart(parts, stage)}, True
                return {"base64": data}, False
            case PartsData(parts=parts):
                return {"multipart": self.multipart(parts, stage)}, media == _MULTIPART
            case TextData(text=text):
                if _is_json_type(media) and (value := _loads_json(text)) is not _NOT_JSON:
                    depth, reference = _json_shape(value)
                    if depth <= _MAX_JSON_DEPTH:
                        if reference is None:
                            return {"json": self.json_value(value, stage)}, exact == _JSON
                        self.notes.append(
                            f"Stage {stage!r}: its JSON body is written as text: a json body could not hold its member {reference!r},"
                            " which the scenario file would read as a reference"
                        )
                        return {"text": self.json_text(text, value, stage)}, False
                if media == _FORM:
                    if (pairs := _form_pairs(text)) is not None and _mappable([name for name, _ in pairs]) and not self.any_secret(pairs):
                        return {"form": _grouped([(name, escape(value)) for name, value in pairs])}, exact == _FORM
                    # Text sent as a form that no form body sends as it was
                    # (a JSON value in a field, as curl's -d sends it), and
                    # one with a secret, which a form body would send empty
                    # unset: the secret a placeholder that fails the stage.
                    return {"text": self.fields_text(text, "form field", stage)}, False
                if media == _MULTIPART and content_type and (parts := multipart_parts(text.encode(), content_type)) is not None:
                    return {"multipart": self.multipart(parts, stage)}, True
                return {"text": escape(text)}, False

    # --- one request ---

    def request(self, recorded: RecordedRequest, base_url: str | None) -> tuple[str, dict[str, Any]]:
        """The stage name and its ``request``."""
        url = urlsplit(recorded.url)
        path = url.path or "/"
        stage = self.stage_name(recorded.method, path)
        request: dict[str, Any] = {}
        if recorded.method != "GET":
            request["method"] = recorded.method

        # Relative to base_url, unless the path starts `//`, which would read
        # as a host: absolute, that one URL ignores base_url.
        target = path if base_url is not None and not path.startswith("//") else f"{_origin(url.scheme, url.netloc)}{path}"
        params, query = self.query(url.query, stage)
        empty_query = not url.query and recorded.url.partition("#")[0].endswith("?")
        # Closed before a query holding a placeholder.
        request["url"] = escape(target, closed=query is not None and contains_template(query)) + (f"?{query or ''}" if query is not None or empty_query else "")
        if params:
            request["params"] = params

        # (name, value, whether it is recorded text): text is escaped once a
        # repeated header's values are joined (`_joined_text`).
        headers: list[tuple[str, str, bool]] = []
        cookies = list(recorded.cookies)
        authorization = None
        for name, value in recorded.headers:
            lowered = name.lower()
            if lowered.startswith(":"):
                continue
            if lowered == "host":
                if not _host_header_matches(value, url.scheme, url.netloc):
                    headers.append((name, value, True))
            elif lowered == "proxy-authorization":
                self.notes.append(f"Stage {stage!r}: its Proxy-Authorization header is left out: a proxy's credentials go in client.proxy's URL.")
            elif lowered in TRANSPORT_HEADERS:
                continue
            elif lowered == "cookie":
                cookies += parse_cookie_header(value)
            elif lowered == "authorization":
                authorization = authorization or value
            elif (what := self.hidden_header(name, value)) is not None:
                headers.append((_header_name(name), self.secret(name, value, what, stage), False))
            else:
                headers.append((_header_name(name), value, True))

        auth = None
        if authorization is not None:
            auth, header = self.authorization(authorization, stage)
            if header is not None:
                headers.append(("Authorization", header, False))
        elif recorded.bearer:
            auth = {"bearer": self.secret("api_token", recorded.bearer, "the bearer token", stage)}
        elif recorded.user is not None or url.username is not None:
            # A URL's user name without a password is sent with an empty one
            # (`https://<token>@host`, an API key); only -u asks for one.
            user, password = recorded.user or (unquote(url.username or ""), unquote(url.password or ""))
            auth = self.credentials("digest" if recorded.digest else "basic", user, password, stage)

        if cookies:
            # One placeholder for the whole header, not one per cookie: an
            # unset variable renders None, which fails the stage for a whole
            # template and would be sent as the text `None` inside a longer
            # one, where the report's redaction hides it.
            names = ", ".join(dict.fromkeys(name or "a nameless cookie" for name, _ in cookies))
            value = "; ".join(f"{name}={cookie}" if name else cookie for name, cookie in cookies)
            headers.append(("Cookie", self.secret("cookie", value, f"the cookies {names}", stage), False))

        content_type = next((value if text else unescape(value) for name, value, text in headers if name.lower() == "content-type"), None)
        body, implied = self.body(recorded.body, content_type, stage)
        # One field per name, as RFC 9110 folds a repeated one: a mapping
        # holds a name once.
        merged: dict[str, tuple[str, list[tuple[str, str | None]]]] = {}
        for name, value, text in headers:
            lowered = name.lower()
            if lowered == "content-type" and implied:
                continue
            merged.setdefault(lowered, (name, []))[1].append((value, None) if text else ("", value))
        if merged:
            request["headers"] = {name: _joined_text(values, ", ") for name, values in merged.values()}
        if auth is not None:
            request["auth"] = auth
        if body is not None:
            request["body"] = body
        if recorded.timeout is not None:
            request["timeout"] = recorded.timeout
        return stage, request

    def query(self, query: str, stage: str) -> tuple[dict[str, Any] | None, str | None]:
        """A query string as ``(params, None)``, or as ``(None, text)`` to
        keep it in the URL as written where ``params`` would not send it as
        it was: an escape that is not UTF-8, a name a mapping cannot hold or
        a repeated one apart from its first (`_mappable`); and where it
        holds a secret, since ``params`` sends a value rendered to null
        empty, where the URL's placeholder fails the stage unset
        (`fields_text`). A piece without ``=`` is sent as an empty value."""
        if not query:
            return None, None
        pieces = query.split("&")
        # An empty component (a leading, repeated or trailing ampersand) is
        # lost when a query is rewritten as params. Keep such queries intact.
        pairs = _decoded_pairs(pieces) if all(pieces) else None
        if pairs is not None and _mappable([name for name, _ in pairs]) and not self.any_secret(pairs):
            return _grouped([(name, escape(value)) for name, value in pairs]), None
        return None, self.fields_text(query, "query parameter", stage)

    def any_secret(self, pairs: list[tuple[str, str]]) -> bool:
        """Whether any of the fields is a secret (`is_secret_field`)."""
        return any(self.is_secret_field(name, value) for name, value in pairs)


def build_scenario(requests: list[RecordedRequest], *, description: str, redaction: Redaction = DEFAULT_REDACTION) -> ImportResult:
    """The scenario for ``requests``, one stage each, in order (see the module
    docstring for what maps to what). ``description`` is the scenario's.

    A request whose ``status`` was recorded verifies it; one without (a curl
    command) verifies a ``2xx``. Client settings (redirects, timeouts, TLS
    verification) are the client's when every request agrees on them: a HAR
    file records each redirect as an entry of its own, so its requests do not
    follow redirects, and neither does curl without ``-L``.
    """
    if not requests:
        raise ImportSourceError("There are no requests to import")
    builder = _Builder(redaction)
    origins = {_origin(url.scheme, url.netloc) for url in (urlsplit(recorded.url) for recorded in requests)}
    base_url = next(iter(origins)) if len(origins) == 1 else None

    stages: list[dict[str, Any]] = []
    for recorded in requests:
        name, request = builder.request(recorded, base_url)
        status: int | str = recorded.status if recorded.status is not None else "2xx"
        stages.append({"name": name, "request": request, "response": [{"verify": {"status": status}}]})
    requests_out = [stage["request"] for stage in stages]

    client: dict[str, Any] = {}
    if base_url is not None:
        client["base_url"] = escape(base_url)
    auth = None
    if len(stages) > 1:
        # What every request sends alike is the scenario's to send.
        if (first := requests_out[0].get("auth")) is not None and all(request.get("auth") == first for request in requests_out):
            auth = first
            for request in requests_out:
                del request["auth"]
        if shared := _shared_headers([request.get("headers", {}) for request in requests_out]):
            client["headers"] = shared
            names = {name.lower() for name in shared}
            for request in requests_out:
                if remaining := {name: value for name, value in request.pop("headers").items() if name.lower() not in names}:
                    request["headers"] = remaining
    if any(not recorded.follow_redirects for recorded in requests):
        client["follow_redirects"] = False
        for request, recorded in zip(requests_out, requests, strict=True):
            if recorded.follow_redirects:
                request["allow_redirects"] = True
    if len(timeouts := {recorded.timeout for recorded in requests}) == 1 and (timeout := timeouts.pop()) is not None:
        client["timeout"] = timeout
        for request in requests_out:
            del request["timeout"]

    for stage in stages:
        stage["request"] = {key: stage["request"][key] for key in _REQUEST_KEYS if key in stage["request"]}

    scenario: dict[str, Any] = {"description": description}
    placeholders = builder.placeholders
    if placeholders:
        scenario["substitutions"] = [{"vars": {entry.var: "{{ env('" + entry.env + "') }}" for entry in placeholders}}]
    if not all(recorded.verify_tls for recorded in requests):
        scenario["ssl"] = {"verify": False}
        if any(recorded.verify_tls for recorded in requests):
            builder.notes.append("TLS certificates go unchecked for every request (ssl.verify is the scenario's), though only some of them turned the check off.")
    if client:
        scenario["client"] = client
    if auth is not None:
        scenario["auth"] = auth
    scenario["stages"] = stages
    return ImportResult(scenario, placeholders, list(dict.fromkeys(builder.files)), builder.notes)


def _shared_headers(headers: list[dict[str, str]]) -> dict[str, str]:
    """The headers every request sends with the same value, by name
    case-insensitively, the first request's spelling kept; never a
    Content-Type, which belongs to a body."""
    first, *rest = headers
    shared = {}
    for name, value in first.items():
        if name.lower() == "content-type":
            continue
        if all(any(other.lower() == name.lower() and other_value == value for other, other_value in request.items()) for request in rest):
            shared[name] = value
    return shared


def scenario_text(scenario: dict[str, Any]) -> str:
    """The scenario as the file an import writes: JSON, indented.

    Raises `ImportSourceError` for a lone surrogate (a JSON ``"\\ud800"`` in
    a HAR file, a curl ``$'\\ud800'``), which is no text: no request sends
    it, and no UTF-8 file holds it.
    """
    text = json.dumps(scenario, indent=4, ensure_ascii=False) + "\n"
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as e:
        line = text.count("\n", 0, e.start) + 1
        raise ImportSourceError(f"What was recorded holds {text[e.start : e.end]!r}, a lone surrogate, which is no text (line {line} of the scenario)") from None
    return text


def validate_text(text: str) -> ValidateResult:
    """Validate the text of a scenario file as ``validate`` validates the
    file: written to a directory of its own and loaded from there, through
    the reader, the reference resolver (a recorded ``$ref`` that reached a
    mapping would be resolved, and fail) and the model, then the semantic
    checks. The directory is the references' root, so that one reaches no
    file of the user's."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "test_imported.http.json"
        path.write_text(text, encoding="utf-8")
        return validate_scenario(path, root_path=Path(directory))
