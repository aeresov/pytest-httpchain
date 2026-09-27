"""Translation of resolved scenario models into httpx call arguments: the
client's, once per scenario, and a request's, once per iteration.

Both builders take already-resolved models (walking templates is the carrier's
job) and the scenario's directory, which relative paths resolve against.
"""

import base64
import ssl
from pathlib import Path
from typing import Any
from urllib.parse import unquote_plus

import httpx

from pytest_httpchain.errors import RequestError
from pytest_httpchain.models import (
    Base64Body,
    BinaryBody,
    FilesBody,
    FormBody,
    GraphQLBody,
    JsonBody,
    Request,
    SSLConfig,
    TextBody,
    UserFunctionCall,
    XmlBody,
)
from pytest_httpchain.userfunc import UserFunctionError, call_user_function
from pytest_httpchain.utils import resolve_scenario_path


def build_ssl_verify(config: SSLConfig, scenario_dir: Path | None) -> bool | ssl.SSLContext:
    """Translate ``SSLConfig`` into httpx's supported ``verify=`` forms.

    httpx 0.28 deprecates ``verify=<str>`` and ``cert=``: anything beyond a
    plain boolean must arrive as a ready context. A non-bool ``verify`` is a
    scenario-relative CA bundle file or directory; a client cert needs a context
    to load into, so a bool ``verify`` is expanded via
    ``httpx.create_ssl_context``, keeping httpx's own trust-store semantics.
    """
    verify = config.verify
    if not isinstance(verify, bool):
        ca = resolve_scenario_path(scenario_dir, verify)
        ctx = ssl.create_default_context(capath=ca) if ca.is_dir() else ssl.create_default_context(cafile=ca)
    elif config.cert is None:
        return verify
    else:
        ctx = httpx.create_ssl_context(verify=verify)

    if config.cert is not None:
        if isinstance(config.cert, list | tuple):
            certfile, keyfile = (resolve_scenario_path(scenario_dir, p) for p in config.cert)
            ctx.load_cert_chain(certfile, keyfile)
        else:
            ctx.load_cert_chain(resolve_scenario_path(scenario_dir, config.cert))
    return ctx


def build_client_kwargs(config: SSLConfig, auth: UserFunctionCall | None, scenario_dir: Path | None) -> dict[str, Any]:
    """Arguments for the scenario's shared client. ``auth`` is invoked here
    because httpx wants the resulting flow, not the call description."""
    kwargs: dict[str, Any] = {"verify": build_ssl_verify(config, scenario_dir), "http2": True}
    if auth is not None:
        kwargs["auth"] = call_user_function(auth)
    return kwargs


def _read_file(path: Path, declared: Any, missing: str, unreadable: str) -> bytes:
    """Read a scenario-referenced file, reporting I/O failures as `RequestError`
    against the path as written in the scenario."""
    try:
        return path.read_bytes()
    except FileNotFoundError as e:
        raise RequestError(f"{missing}: {declared}") from e
    except OSError as e:
        raise RequestError(f"{unreadable} '{declared}': {e}") from e


def _merge_query(query: str, params: dict[str, Any]) -> str:
    """The URL's raw ``query`` with ``params`` merged in.

    Key order and precedence are httpx's ``URL.copy_merge_params``: the URL's
    parameters first, then new keys in declared order, and a key in both takes
    the params value(s) at its first place in the URL, its other occurrences
    dropped. The merge works on the raw ``&``-separated segments, though: a
    segment whose key params does not name goes out as written, and only keys
    are decoded, to match. copy_merge_params decodes the whole query into a
    dict and encodes it again, which changed parameters params never names:
    ``q=%E9`` (not UTF-8) went out as ``q=%EF%BF%BD``, ``a=1&b=2&a=3`` as
    ``a=1&a=3&b=2``, and ``a=1;b=2`` as ``a=1%3Bb%3D2``.
    """
    pending = {key: str(httpx.QueryParams({key: value})) for key, value in params.items()}
    segments = []
    for segment in query.split("&"):
        key = unquote_plus(segment.partition("=")[0])
        if key not in params:
            segments.append(segment)
        elif key in pending:
            segments.append(pending.pop(key))
    segments.extend(pending.values())
    # Empty segments (from `&&`, a bare `?`, or an empty list value) carry no parameter.
    return "&".join(segment for segment in segments if segment)


def build_request_kwargs(request_model: Request, scenario_dir: Path | None = None) -> dict[str, Any]:
    """Arguments for one ``client.request(...)`` call from a resolved `Request`."""
    url = request_model.url
    if request_model.params:
        # httpx's params= *replaces* the URL's own query, so `/items?page=2`
        # with params {"limit": 10} went out as `/items?limit=10`. Merged into
        # the URL instead, which moves parsing — and its InvalidURL — here
        # from client.request. httpx.URL has already normalized the query to
        # what it would send without params, so the kept segments match that.
        try:
            parsed = httpx.URL(url)
            query = _merge_query(parsed.query.decode("ascii"), request_model.params)
            url = str(parsed.copy_with(query=query.encode("ascii") if query else None))
        except httpx.InvalidURL as e:
            raise RequestError(f"Invalid request URL: {e}") from e

    request_kwargs: dict[str, Any] = {
        "method": request_model.method,
        "url": url,
        "headers": request_model.headers,
        "timeout": request_model.timeout,
        "follow_redirects": request_model.allow_redirects,
    }

    if request_model.auth:
        try:
            request_kwargs["auth"] = call_user_function(request_model.auth)
        except UserFunctionError as e:
            raise RequestError(f"Failed to configure authentication: {e}") from e

    match request_model.body:
        case None:
            pass

        case JsonBody(json=None):
            # httpx reads json=None as "no body", so a declared null — literal,
            # or a template that rendered to None — went out exactly like an
            # undeclared body. Sent as the JSON document it is instead.
            request_kwargs["content"] = b"null"
            if not any(name.lower() == "content-type" for name in request_model.headers):
                request_kwargs["headers"] = {**request_model.headers, "Content-Type": "application/json"}

        case JsonBody(json=data):
            request_kwargs["json"] = data

        case GraphQLBody(graphql=gql):
            request_kwargs["json"] = {"query": gql.query, "variables": gql.variables}

        case FormBody(form=data):
            request_kwargs["data"] = data

        case XmlBody(xml=data) | TextBody(text=data):
            request_kwargs["content"] = data

        case Base64Body(base64=encoded_data):
            request_kwargs["content"] = base64.b64decode(encoded_data)

        case BinaryBody(binary=file_path):
            path = resolve_scenario_path(scenario_dir, file_path)
            request_kwargs["content"] = _read_file(path, file_path, "Binary file not found", "Cannot read binary file")

        case FilesBody(files=file_paths):
            files_list = []
            for field_name, file_path in file_paths.items():
                path = resolve_scenario_path(scenario_dir, file_path)
                content = _read_file(path, file_path, "File not found for upload", "Cannot read file for upload")
                files_list.append((field_name, (path.name, content)))
            request_kwargs["files"] = files_list

        case _:
            raise RuntimeError(f"Unhandled request body type: {type(request_model.body).__name__}")

    return request_kwargs
