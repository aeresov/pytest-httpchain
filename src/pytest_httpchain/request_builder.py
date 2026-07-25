"""Translation of resolved scenario models into httpx call arguments: the
client's, once per scenario, and a request's, once per iteration.

Both builders take already-resolved models (walking templates is the carrier's
job) and the scenario's directory, which relative paths resolve against.
"""

import base64
from pathlib import Path
from typing import Any

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


def normalize_cert(cert: Any) -> str | tuple[str, ...]:
    """Stringify client-cert paths: httpx unpacks a non-tuple cert into
    ``load_cert_chain(*cert)``, which a bare ``Path`` does not survive."""
    if isinstance(cert, list | tuple):
        return tuple(str(p) for p in cert)
    return str(cert)


def build_client_kwargs(ssl: SSLConfig, auth: UserFunctionCall | None, scenario_dir: Path | None) -> dict[str, Any]:
    """Arguments for the scenario's shared client.

    A Path-valued ``verify`` is a CA bundle and, like ``cert``, is
    scenario-relative; ``auth`` is invoked because httpx wants the resulting
    flow, not the call description.
    """
    verify = ssl.verify
    if isinstance(verify, Path):
        verify = str(resolve_scenario_path(scenario_dir, verify))

    kwargs: dict[str, Any] = {"verify": verify, "http2": True}

    if ssl.cert is not None:
        cert = ssl.cert
        if isinstance(cert, list | tuple):
            cert = tuple(resolve_scenario_path(scenario_dir, p) for p in cert)
        else:
            cert = resolve_scenario_path(scenario_dir, cert)
        kwargs["cert"] = normalize_cert(cert)

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


def build_request_kwargs(request_model: Request, scenario_dir: Path | None = None) -> dict[str, Any]:
    """Arguments for one ``client.request(...)`` call from a resolved `Request`."""
    request_kwargs: dict[str, Any] = {
        "method": request_model.method,
        "url": str(request_model.url),
        "headers": request_model.headers,
        "params": request_model.params or None,
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
