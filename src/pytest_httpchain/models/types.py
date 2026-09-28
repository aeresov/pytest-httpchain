"""Validated type aliases for the scenario models: content validators (JMESPath,
regex, XML, GraphQL, base64, templates, import names, identifiers, schemas,
paths, URLs) and the ``SimpleNamespace``<->``dict`` round-trip that makes ``vars``
attribute-accessible in templates and JSON-serializable in bodies."""

import base64
import keyword
import re
import types
import xml.etree.ElementTree
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path, PurePath
from typing import Annotated, Any, Literal, NamedTuple, get_args
from urllib.parse import unquote

import graphql
import httpx
import jmespath
import jsonschema
import pydantic_core
from pydantic import (
    AfterValidator,
    AnyHttpUrl,
    AnyUrl,
    BeforeValidator,
    Field,
    JsonValue,
    PlainSerializer,
    StrictFloat,
    StrictInt,
    TypeAdapter,
    ValidatorFunctionWrapHandler,
    WithJsonSchema,
    WrapValidator,
)

from pytest_httpchain.constants import parse_user_function_name
from pytest_httpchain.templates import TEMPLATE_PATTERN, TEMPLATE_PATTERN_ECMA, is_complete_template


def create_string_validator(validation_func: Callable[[str], Any], error_message: str) -> Callable[[str], str]:
    """Factory for creating string validators."""

    def validator(v: str) -> str:
        try:
            validation_func(v)
        except Exception as e:
            raise ValueError(error_message) from e
        return v

    return validator


def validate_python_identifier(v: str) -> str:
    """Validate Python identifier and check for reserved keywords."""
    if not v.isidentifier():
        raise ValueError(f"Invalid Python variable name: '{v}'")

    if keyword.iskeyword(v) or v in keyword.softkwlist:
        raise ValueError(f"Python keyword is used as variable name: '{v}'")

    return v


def json_schema_validator_class(schema: dict[str, Any]) -> type[jsonschema.protocols.Validator]:
    """The validator class for a schema's declared dialect.

    Draft 2020-12 is pinned as the fallback — the dialect
    ``jsonschema.validate`` would pick — so meta-checking and instance
    validation always agree.
    """
    return jsonschema.validators.validator_for(schema, default=jsonschema.Draft202012Validator)


def check_json_schema(schema: dict[str, Any]) -> None:
    """Check JSON schema validity against its declared dialect's meta-schema."""
    json_schema_validator_class(schema).check_schema(schema)


def validate_json_schema_inline(v: dict[str, Any]) -> dict[str, Any]:
    """`check_json_schema` as a pydantic validator."""
    try:
        check_json_schema(v)
    except jsonschema.SchemaError as e:
        raise ValueError(f"Invalid JSON Schema: {e.message}") from e
    except Exception as e:
        raise ValueError(f"JSON Schema validation error: {e}") from e

    return v


class SchemaFileRef(NamedTuple):
    """A ``verify.body.schema`` file reference taken apart (`parse_schema_file_ref`):
    the file's ``path`` and the ``fragment`` after its ``#``, both as written
    (``""`` for none), and the JSON pointer's ``pointer`` segments, decoded."""

    path: str
    fragment: str
    pointer: tuple[str, ...]


# What makes a reference a URI, not a path: `http:`, `https:` or `file:`, in
# any form, or another scheme with an authority (`ftp://host/...`). Any other
# colon is a path's (`schemas:v1/user.json`, `user-2024-01-01T10:00.json`), and
# so is a Windows drive's (C:\schemas\user.json, one letter).
_URI = re.compile(r"(?:https?|file):|[a-z][a-z0-9+.-]+://", re.IGNORECASE)
# An RFC 6901 escape is ~0 (for ~) or ~1 (for /): any other ~ is not one.
_BAD_POINTER_ESCAPE = re.compile(r"~(?![01])")


def parse_schema_file_ref(ref: str) -> SchemaFileRef:
    """Take a ``body.schema`` file reference apart, or say why it is not one.

    ``./openapi.json#/components/schemas/User`` is a local file, then, after
    the first ``#``, an RFC 6901 JSON pointer in URI fragment form:
    percent-decoded first (``%20`` is a space), then split at each ``/``, each
    segment unescaped (``~1`` is ``/``, ``~0`` is ``~``). An empty fragment, or
    none, selects the whole document. Anything that is not a pointer after the
    ``#``, such as a plain-name anchor, is refused, and so is a URI (an
    ``http:``, ``https:`` or ``file:`` one, or any with ``//`` after its
    scheme): remote schemas are never fetched. A path cannot hold a ``#``,
    then, and ``./`` before one that starts like a URI keeps it a path.

    Shared by the model, which refuses a bad reference at load, the runtime
    and ``validate --deep``, which read the file and follow the pointer, so
    all three take a reference apart the same way.
    """
    path, _, fragment = ref.partition("#")
    if not path:
        raise ValueError(f"A schema file reference must name a file before its '#', got {ref!r}")
    if uri := _URI.match(path):
        if uri.group().lower().startswith("http"):
            raise ValueError(f"A schema file must be a local file: remote schemas are not fetched, got {ref!r}")
        raise ValueError(f"A schema file must be a local file path, not a URI, got {ref!r}")
    if not fragment:
        return SchemaFileRef(path, fragment, ())
    if not fragment.startswith("/"):
        raise ValueError(f"The part after '#' must be a JSON pointer starting with '/' (such as '#/components/schemas/User'), got {ref!r}")
    try:
        decoded = unquote(fragment, errors="strict")
    except UnicodeDecodeError as e:
        raise ValueError(f"The JSON pointer's percent-encoding is not UTF-8, got {ref!r}") from e
    segments = decoded[1:].split("/")
    for segment in segments:
        if _BAD_POINTER_ESCAPE.search(segment):
            raise ValueError(f"A '~' in a JSON pointer must be followed by 0 (for '~') or 1 (for '/'), got {ref!r}")
    return SchemaFileRef(path, fragment, tuple(segment.replace("~1", "/").replace("~0", "~") for segment in segments))


def validate_schema_file_ref(value: Any, handler: ValidatorFunctionWrapHandler) -> str:
    """Validate a ``body.schema`` file reference, keeping it as written: a
    `Path` would normalize the pointer after its ``#`` (``//`` is an empty key,
    a trailing ``/`` one more). A path object a template renders (a fixture's)
    stands for its text. A string with a template in it is the engine's to
    render, left to ``PartialTemplateStr``, as a URL's is (`_literal_url`)."""
    v: str = handler(str(value) if isinstance(value, PurePath) else value)
    if template := re.search(TEMPLATE_PATTERN, v):
        raise ValueError(f"Not a literal file reference: it contains a template expression at position {template.start()}")
    parse_schema_file_ref(v)
    return v


validate_jmespath_expression = create_string_validator(jmespath.compile, "Invalid JMESPath expression")


def validate_jmespath_key(v: str) -> str:
    """A ``verify.jmespath`` key: a JMESPath expression, which is never rendered.

    Only values are substituted, so a template in a key reaches JMESPath as
    written, and ``{{`` is not JMESPath: a key with one that fails to compile
    is refused saying why. One that compiles (``'{{ x }}'``, a JMESPath string
    literal) is HTTPCHAIN029's to report, as a templated key is anywhere.
    """
    try:
        jmespath.compile(v)
    except Exception as e:
        if re.search(TEMPLATE_PATTERN, v):
            raise ValueError("Invalid JMESPath expression: a key is never rendered, only the value it maps to, so it cannot hold a template") from e
        raise ValueError("Invalid JMESPath expression") from e
    return v


validate_regex_pattern = create_string_validator(re.compile, "Invalid regular expression")


def validate_regex_group_name(v: str) -> str:
    """A regex group's name, which ``re`` takes only as a Python identifier,
    so a name that cannot be one fails at load whatever the pattern."""
    if not v.isidentifier():
        raise ValueError(f"Invalid group name {v!r}: a group is a number (0 for the whole match) or the name of a (?P<name>...) group")
    return v


def regex_group(pattern: re.Pattern[str], group: int | str | None) -> int | str:
    """The group of ``pattern`` a ``save.regex`` entry saves from a match:
    ``group`` when set, and otherwise group 1 when the pattern has any
    groups, else the whole match (0). A ValueError says why ``pattern`` has
    no such group.

    Shared by the model, which checks a literal pattern's group at load, and
    `response_steps.process_save`, which checks one a template rendered: the
    same groups are refused in the same words, before any match is tried.
    """
    if group is None:
        return 1 if pattern.groups else 0
    if isinstance(group, int):
        if group > pattern.groups:
            count = "no groups" if not pattern.groups else "1 group" if pattern.groups == 1 else f"{pattern.groups} groups"
            raise ValueError(f"regex '{pattern.pattern}' has no group {group} (it has {count}; 0 is the whole match)")
    elif group not in pattern.groupindex:
        names = f"its named groups: {', '.join(map(repr, pattern.groupindex))}" if pattern.groupindex else "it has no named groups"
        raise ValueError(f"regex '{pattern.pattern}' has no group named {group!r} ({names})")
    return group


validate_xml = create_string_validator(xml.etree.ElementTree.fromstring, "Invalid XML")

validate_graphql_query = create_string_validator(graphql.parse, "Invalid GraphQL query")

validate_base64 = create_string_validator(lambda v: base64.b64decode(v, validate=True), "Invalid base64 encoding")


def validate_template_expression(v: str) -> str:
    if not is_complete_template(v):
        raise ValueError(f"Must be a complete template expression like '{{{{ expr }}}}', got: {v!r}")
    return v


def _check_partial_template_str(v: str, got: str) -> str:
    matches = list(re.finditer(TEMPLATE_PATTERN, v))
    if not matches:
        raise ValueError(f"Must contain at least one template expression like '{{{{ expr }}}}'{got}")

    for match in matches:
        if not match.group("expr").strip():
            raise ValueError(f"Template expression cannot be empty at position {match.start()}")
    return v


def validate_partial_template_str(v: str) -> str:
    return _check_partial_template_str(v, f", got: {v!r}")


def validate_unquoted_partial_template_str(v: str) -> str:
    """`validate_partial_template_str` for a value that can carry credentials,
    whose message does not quote it (see `validate_proxy_url`)."""
    return _check_partial_template_str(v, "")


def validate_function_import_name(v: str) -> str:
    """Validate a ``module.path:function_name`` against the grammar the importer
    accepts, so a bare name fails here rather than at runtime import."""
    parse_user_function_name(v)
    return v


def convert_dict_to_namespace(v: Any) -> Any:
    """Recursively turn dicts into ``SimpleNamespace``, so ``{{ var.attr }}``
    works in templates."""
    match v:
        case dict():
            return types.SimpleNamespace(**{key: convert_dict_to_namespace(value) for key, value in v.items()})
        case list():
            return [convert_dict_to_namespace(item) for item in v]
        case _:
            return v


def convert_namespace_to_dict(v: Any) -> Any:
    """Normalize ``SimpleNamespace`` back to dicts at every depth, so the value
    is JSON-serializable. Lists and dicts are copied, keys in their order.

    Iterative: it runs as a validator on JSON bodies, inline schemas and
    ``verify.jmespath`` operands, where the loader accepts a value of any depth,
    and a recursive walk overflowed on one nested past the recursion limit. A
    value that contains itself (a fixture's self-referencing list) is refused
    with a ValueError, which pydantic reports, where the recursion overflowed
    and a plain loop would never end.
    """
    containers = (types.SimpleNamespace, dict, list)
    if not isinstance(v, containers):
        return v
    root: list[Any] = [None]
    # The containers being copied, from the root down to the one in hand: a
    # member among them is a cycle, while one met again elsewhere is only
    # shared and is copied once more, as the recursive walk did.
    in_progress: set[int] = set()
    # (value to copy, the container its copy goes in, the key there), or the
    # id of a container whose members are all copied.
    pending: list[tuple[Any, Any, Any] | int] = [(v, root, 0)]
    while pending:
        entry = pending.pop()
        if isinstance(entry, int):
            in_progress.discard(entry)
            continue
        value, parent, slot = entry
        if id(value) in in_progress:
            raise ValueError("the value contains itself, so it cannot be converted to JSON")
        match value:
            case types.SimpleNamespace() | dict():
                copy: Any = {}
                members: Iterable[tuple[Any, Any]] = (vars(value) if isinstance(value, types.SimpleNamespace) else value).items()
            case _:
                copy = [None] * len(value)
                members = enumerate(value)
        parent[slot] = copy
        in_progress.add(id(value))
        pending.append(id(value))
        for key, member in members:
            # Every key is placed now, so the copy keeps the original's order.
            copy[key] = member
            if isinstance(member, containers):
                pending.append((member, copy, key))
    return root[0]


def convert_namespace_items_to_dict(v: Any) -> Any:
    """Turn a sequence's ``SimpleNamespace`` items into dicts, one level only:
    the values keep their shape, so a nested ``vars`` object stays
    attribute-accessible.

    A sequence is whatever pydantic's lax ``list`` takes, i.e. any iterable but
    text and mappings: a template can render a tuple (``{{ tuple(combos) }}``)
    and a user function an iterator, and one this skipped would reach the
    ``list[dict]`` it feeds with its namespaces intact."""
    if isinstance(v, Iterable) and not isinstance(v, (str, bytes, bytearray, Mapping)):
        return [dict(vars(item)) if isinstance(item, types.SimpleNamespace) else item for item in v]
    return v


VariableName = Annotated[str, AfterValidator(validate_python_identifier)]
FunctionImportName = Annotated[str, AfterValidator(validate_function_import_name)]
JMESPathExpression = Annotated[str, AfterValidator(validate_jmespath_expression)]
JMESPathKey = Annotated[str, AfterValidator(validate_jmespath_key)]
JSONSchemaInline = Annotated[dict[str, Any], AfterValidator(validate_json_schema_inline)]
SerializablePath = Annotated[Path, PlainSerializer(lambda x: str(x), return_type=str)]
SchemaFileRefStr = Annotated[str, WrapValidator(validate_schema_file_ref), WithJsonSchema({"type": "string", "minLength": 1})]
RegexPattern = Annotated[str, AfterValidator(validate_regex_pattern)]
# A regex group a `save.regex` entry saves: its number or its name.
RegexGroupNumber = Annotated[StrictInt, Field(ge=0)]
RegexGroupName = Annotated[str, AfterValidator(validate_regex_group_name)]
XMLString = Annotated[str, AfterValidator(validate_xml)]
GraphQLQuery = Annotated[str, AfterValidator(validate_graphql_query)]
TemplateExpression = Annotated[str, AfterValidator(validate_template_expression)]
PartialTemplateStr = Annotated[str, AfterValidator(validate_partial_template_str)]
# The template branch beside `BaseUrlStr` and `ProxyUrlStr`: a URL they refuse
# is refused here as well, and must not be quoted here either.
UnquotedPartialTemplateStr = Annotated[str, AfterValidator(validate_unquoted_partial_template_str)]

# Editor-schema only: these tighten the `string` branch of `concrete | template`
# fields so an editor flags e.g. timeout "abc", without affecting runtime
# validation. ECMA-262 spelling, since JSON Schema `pattern` is a JS regex.
_COMPLETE_TEMPLATE_PATTERN = rf"^\s*{TEMPLATE_PATTERN_ECMA}\s*$"
_NUMBER_OR_TEMPLATE_PATTERN = rf"(?:{_COMPLETE_TEMPLATE_PATTERN})|(?:^[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)$)"

# For fields whose concrete strings are covered by an enum/bool branch already.
TemplateExpressionOnly = Annotated[
    str,
    AfterValidator(validate_template_expression),
    WithJsonSchema({"type": "string", "pattern": _COMPLETE_TEMPLATE_PATTERN}),
]
# For `Any`-typed template fields: the runtime type stays permissive (a verify
# expression re-validates as a boolean after rendering), while the editor schema
# says what the field actually accepts. Without it the field emits `items: {}`
# and an editor cannot flag the mistake that matters most here — forgetting the
# `{{ }}`, which leaves a plain string where a boolean condition belongs.
TemplateExpressionSchema = Annotated[
    Any,
    WithJsonSchema({"type": "string", "pattern": _COMPLETE_TEMPLATE_PATTERN}),
]
# For numeric fields, whose stringified form the runtime coerces ("30" -> 30.0).
NumberOrTemplate = Annotated[
    str,
    AfterValidator(validate_template_expression),
    WithJsonSchema({"type": "string", "pattern": _NUMBER_OR_TEMPLATE_PATTERN}),
]

# Any RFC 9110 token is a legal method (PROPFIND, PURGE, vendor verbs). The
# ``HTTPMethod`` branch before it only feeds the JSON Schema's verb autocompletion:
# the smart union keeps a str input a plain str (only the default is the enum).
_HTTP_METHOD_TOKEN_PATTERN = r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$"


def validate_http_method_token(v: str) -> str:
    """Validate an HTTP method as an RFC 9110 token."""
    if not re.fullmatch(_HTTP_METHOD_TOKEN_PATTERN, v):
        raise ValueError(f"Invalid HTTP method token: {v!r}")
    return v


HttpMethodToken = Annotated[
    str,
    AfterValidator(validate_http_method_token),
    WithJsonSchema({"type": "string", "pattern": _HTTP_METHOD_TOKEN_PATTERN}),
]

# The WHATWG parser behind pydantic's URL types, as a check only.
_WHATWG_HTTP_URL = TypeAdapter(AnyHttpUrl)

# What WHATWG strips from both ends of a URL. Not ``str.strip()``'s set, which
# also takes U+00A0, U+3000, U+2028, ...: WHATWG keeps those, and httpx
# percent-encodes them just as WHATWG does.
_C0_CONTROL_OR_SPACE = "".join(map(chr, range(0x21)))


# RFC 3986's scheme. A URL starting with one is absolute; anything else is a
# relative reference (whose first segment cannot hold a ':' for this reason).
_URL_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")

# The proxy schemes httpx accepts (the socks ones need its `socks` extra).
_PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")


def is_relative_url(url: str) -> bool:
    """Whether ``url`` is a relative reference, which ``client.base_url``
    completes, rather than an absolute URL.

    The one test the model, the request builder and the validator share, so
    all three read a URL the same way.
    """
    return not _URL_SCHEME.match(url)


def _literal_url(value: Any, handler: ValidatorFunctionWrapHandler) -> str:
    """The URL string a wrap validator judges.

    A pydantic URL object, which a single-expression template can render to
    (a pydantic-settings field, say), stands for its string, as it did when
    the field was ``HttpUrl``. A string with a template in it is the engine's
    to render, so it is not a literal URL here: it is left to
    ``PartialTemplateStr``, which refuses an empty ``{{ }}`` that would
    otherwise pass here and fail only when rendered.
    """
    v: str = handler(str(value) if isinstance(value, AnyUrl | pydantic_core.Url) else value)
    if template := re.search(TEMPLATE_PATTERN, v):
        raise ValueError(f"Not a literal URL: it contains a template expression at position {template.start()}")
    return v


def _invalid_url(e: httpx.InvalidURL, v: str, quote: bool) -> ValueError:
    """httpx's parse error, as a URL check reports it.

    Unquoted, a URL with an ``@`` in it loses httpx's reason too: the reason
    quotes the part httpx could not parse, and that can be the credentials
    before the ``@``. A ``/`` in a password ends the authority early, so
    ``http://user:pa/ss@host`` is ``Invalid port: 'pa'`` to httpx.
    """
    if quote or "@" not in v:
        return ValueError(f"Invalid URL: {e}")
    return ValueError("Invalid URL (httpx's reason is not shown, as it can quote the credentials before the '@')")


def _check_http_url(v: str, *, quote: bool = True) -> None:
    """Check an absolute http(s) URL with a host, as written.

    pydantic's ``HttpUrl`` handed back the URL WHATWG-normalized, and that is
    what was sent: ``/static/%2e%2e/ok`` went out as ``/ok``, a ``\\`` in the
    path as ``/``, and a URL over 2083 characters was refused. Its parser
    still judges the URL here — scheme, host and port, with its own errors —
    but its result is dropped. Where it would read the URL differently from
    httpx, which sends it, the URL is refused instead of repaired:

    - surrounding C0 controls or spaces (stripped by WHATWG; httpx sends a
      space as ``%20`` and refuses a control, so one message covers both);
    - anything httpx does not read as an http(s) URL with a host
      (``http:/x``, a control character anywhere, a host only a browser's
      IDNA mapping accepts);
    - a ``\\`` in the authority, where WHATWG ends it and httpx does not, so
      ``http://a:1\\@b:2/`` is ``a:1`` to the check and ``b:2`` on the wire;
    - a percent-encoded host, which WHATWG decodes and httpx sends undecoded.

    The last two keep the host and port judged here the ones httpx connects
    to. Other host spellings go to the resolver as written: ``127.1`` and
    ``[0:0::1]`` reach the address WHATWG read; ``127.0.0.1.``, which WHATWG
    trims, fails when sent.

    ``quote=False`` keeps the URL out of the messages (see `validate_proxy_url`).
    """
    got = f", got: {v!r}" if quote else ""
    _WHATWG_HTTP_URL.validate_python(v)
    if v != v.strip(_C0_CONTROL_OR_SPACE):
        raise ValueError(f"URL must not start or end with a space or control character{got}")
    try:
        url = httpx.URL(v)
    except httpx.InvalidURL as e:
        raise _invalid_url(e, v, quote) from e
    if url.scheme not in ("http", "https") or not url.host:
        raise ValueError(f"URL must start with 'http://' or 'https://' and a host{got}")
    # httpx found a host, so `v` is `scheme://authority...`; httpx's authority
    # runs to the first `/`, `?` or `#`.
    authority = re.split(r"[/?#]", v.split("://", 1)[1], maxsplit=1)[0]
    if "\\" in authority:
        raise ValueError(f"URL must not contain '\\' before its path (a browser reads it as '/', httpx as part of the host){got}")
    if "%" in url.host:
        raise ValueError(f"URL host must not be percent-encoded (httpx sends it undecoded){got}")


def _check_relative_url(v: str) -> None:
    """Check a relative reference, which httpx appends to ``client.base_url``'s
    path as written (see `validate_http_url_reference`).

    Refused where httpx would not send what is written: surrounding C0
    controls or spaces, as for an absolute URL; a leading ``//``, a
    network-path reference whose host httpx drops, keeping only its path; a
    leading ``:``, which httpx reads as an empty scheme and drops; and
    anything httpx cannot parse.
    """
    if v != v.strip(_C0_CONTROL_OR_SPACE):
        raise ValueError(f"URL must not start or end with a space or control character, got: {v!r}")
    if v.startswith("//"):
        raise ValueError(f"URL must not start with '//' without a scheme (httpx would drop the host and append the path to client.base_url), got: {v!r}")
    if v.startswith(":"):
        raise ValueError(f"Relative URL must not start with ':' (httpx drops it), got: {v!r}")
    try:
        httpx.URL(v)
    except httpx.InvalidURL as e:
        raise ValueError(f"Invalid URL: {e}") from e


def validate_http_url_reference(value: Any, handler: ValidatorFunctionWrapHandler) -> str:
    """Validate a request URL, keeping it as written: an absolute http(s) URL
    (`_check_http_url`), or a relative reference (`_check_relative_url`),
    which the scenario's ``client.base_url`` completes.

    Whether there is a base_url is not this field's to know: the validator
    reports a relative URL without one (HTTPCHAIN034), and so does the request
    builder, for a template that renders to one. Anything without a scheme is
    a relative reference, so ``not-a-url`` is a path now; ``ftp://x`` or
    ``http:/x`` still fail as absolute URLs.
    """
    v = _literal_url(value, handler)
    if v and is_relative_url(v):
        _check_relative_url(v)
    else:
        _check_http_url(v)
    return v


def validate_base_url(value: Any, handler: ValidatorFunctionWrapHandler) -> str:
    """Validate ``client.base_url``: an absolute http(s) URL without a query or
    fragment. httpx appends a relative URL to the base URL's raw path, query
    included, so ``https://h/v1?x=1`` would put every stage's path in its
    query. Its messages do not quote it, as a proxy's do not."""
    v = _literal_url(value, handler)
    _check_http_url(v, quote=False)
    if "?" in v or "#" in v:
        raise ValueError("base_url must not have a query or fragment (the stage's URL would be appended after it; put default query parameters in client.params)")
    return v


def validate_proxy_url(value: Any, handler: ValidatorFunctionWrapHandler) -> str:
    """Validate a proxy URL as httpx accepts one: an http, https, socks5 or
    socks5h URL with a host. The socks schemes need httpx's ``socks`` extra,
    which the client reports when it is missing.

    The messages do not quote the URL, nor do those of ``client.base_url``:
    their userinfo is credentials (httpx sends it as ``Proxy-Authorization``
    or ``Authorization``), and the value is usually rendered from a secret,
    at scenario initialization, whose failure message is also the skip reason
    of every later stage. ``ClientConfig`` hides pydantic's ``input_value``
    for the same reason.
    """
    v = _literal_url(value, handler)
    if v != v.strip(_C0_CONTROL_OR_SPACE):
        raise ValueError("URL must not start or end with a space or control character")
    try:
        url = httpx.URL(v)
    except httpx.InvalidURL as e:
        raise _invalid_url(e, v, quote=False) from e
    if url.scheme not in _PROXY_SCHEMES or not url.host:
        raise ValueError("Proxy URL must start with 'http://', 'https://', 'socks5://' or 'socks5h://' and a host")
    return v


# Passed to httpx as written; the schemas keep HttpUrl's `format: uri` hint.
HttpUrlReferenceStr = Annotated[
    str,
    WrapValidator(validate_http_url_reference),
    WithJsonSchema({"type": "string", "format": "uri-reference", "minLength": 1}),
]
BaseUrlStr = Annotated[
    str,
    WrapValidator(validate_base_url),
    WithJsonSchema({"type": "string", "format": "uri", "minLength": 1}),
]
ProxyUrlStr = Annotated[
    str,
    WrapValidator(validate_proxy_url),
    WithJsonSchema({"type": "string", "format": "uri", "minLength": 1}),
]

# Nonstandard codes (nginx 499) must be assertable. Sits after ``HTTPStatus``.
StatusCode = Annotated[int, Field(ge=100, le=599)]

# A status class, "2xx" for any code 200-299. The x's take either case and are
# kept lowercase: one spelling for the consumer, and for its failure message.
_STATUS_CLASS_PATTERN = r"^[1-5][xX]{2}$"
StatusClass = Annotated[str, Field(pattern=_STATUS_CLASS_PATTERN), AfterValidator(str.lower)]


def is_status_class(value: object) -> bool:
    """True for a status class (``"2xx"``), which a validated ``verify.status``
    holds beside codes and, where a template rendered to template text, text
    that is neither."""
    return isinstance(value, str) and re.fullmatch(_STATUS_CLASS_PATTERN, value) is not None


# `verify.jmespath` operands, compared with values a JSON body holds.
# A number as JSON has it: an int or a float, never a bool (an int to Python)
# and never text, so a template rendering "5" is refused, not compared as 5.
JsonNumber = StrictInt | StrictFloat
# A length is a count: a non-negative int, as strictly.
JsonLength = Annotated[StrictInt, Field(ge=0)]
# The JSON types a `type` matcher names, spelled as JSON Schema spells them.
JsonTypeName = Literal["string", "number", "integer", "boolean", "array", "object", "null"]
JSON_TYPE_NAMES: tuple[str, ...] = get_args(JsonTypeName)

Base64String = Annotated[str, AfterValidator(validate_base64)]


def _is_form_scalar(value: Any) -> bool:
    """Text, a number or a boolean (an int to Python): what a form field sends."""
    return isinstance(value, str | int | float)


def _form_kind(value: Any) -> str:
    """How a multipart field's message names what it got, as the scenario
    would have written it."""
    if value is None:
        return "null"
    if isinstance(value, dict | types.SimpleNamespace):
        return "an object"
    return type(value).__name__


def validate_multipart_field_value(value: Any) -> Any:
    """A ``body.multipart.fields`` value: text, a number or a boolean, or a
    list of them, each sent as a field of its own under the one name.

    One validator for the whole value, rather than a union of pydantic's
    strict types, so that a value no member takes (a field a template rendered
    to None, an object) is refused in one sentence naming it, not in five. A
    tuple a template renders is a list; a set, whose order is arbitrary, is not.
    """
    if _is_form_scalar(value):
        return value
    if isinstance(value, list | tuple):
        for i, item in enumerate(value):
            if not _is_form_scalar(item):
                raise ValueError(f"A multipart field's list holds text, numbers or booleans, got {_form_kind(item)} at [{i}]")
        return list(value)
    raise ValueError(f"A multipart field is text, a number or a boolean, or a list of them, got {_form_kind(value)}")


_FORM_SCALAR_SCHEMA: dict[str, Any] = {"type": ["string", "number", "boolean"]}
MultipartFieldValue = Annotated[
    Any,
    AfterValidator(validate_multipart_field_value),
    WithJsonSchema({"anyOf": [_FORM_SCALAR_SCHEMA, {"type": "array", "items": _FORM_SCALAR_SCHEMA}]}),
]

# A control character would end the part's Content-Type header line early, or
# start another: httpx writes the value into the part's headers as it is. A
# tab is whitespace a header value may hold.
_CONTROL_CHARACTER = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


def validate_part_content_type(value: str) -> str:
    """A multipart file's ``content_type``: written into its part's headers as
    given, so without a control character, which would break the part."""
    if control := _CONTROL_CHARACTER.search(value):
        raise ValueError(f"A content type must not contain a control character, got {control.group()!r} at position {control.start()}")
    return value


PartContentType = Annotated[str, Field(min_length=1), AfterValidator(validate_part_content_type)]
NamespaceFromDict = Annotated[Any, AfterValidator(convert_dict_to_namespace)]
# Accepts a SimpleNamespace or a dict; always yields a dict.
NamespaceOrDict = Annotated[dict[str, JsonValue], BeforeValidator(convert_namespace_to_dict)]
