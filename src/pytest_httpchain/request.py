"""HTTP request preparation and execution for chain tests.

This module handles the preparation of HTTP requests from test configurations
and their execution using the requests library.
"""

from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from pytest_httpchain_models.entities import (
    FilesBody,
    FormBody,
    GraphQLBody,
    JsonBody,
    RawBody,
    XmlBody,
)
from pytest_httpchain_models.entities import (
    Request as RequestModel,
)

from .exceptions import RequestError


@dataclass
class PrepareRequestResult:
    request: requests.PreparedRequest
    send_kwargs: dict[str, Any]


def prepare_request(
    session: requests.Session,
    request_model: RequestModel,
) -> PrepareRequestResult:
    """Prepare an HTTP request for execution.

    Args:
        session: HTTP session to use for preparing the request
        request_model: Validated request model

    Returns:
        PreparedRequest dataclass containing the prepared request and send kwargs

    Raises:
        RequestError: If request preparation fails or files not found
    """

    request_kwargs: dict[str, Any] = {
        "method": request_model.method.value,
        "url": str(request_model.url),
        "headers": request_model.headers,
        "params": request_model.params,
    }

    if request_model.auth:
        try:
            # Import and call the auth function directly based on the model type
            from pytest_httpchain_userfunc import call_function

            if isinstance(request_model.auth, str):
                auth_result = call_function(request_model.auth)
            elif isinstance(request_model.auth, dict):
                func_name = request_model.auth.get("function")
                if not func_name:
                    raise RequestError("Auth function definition must have 'function' key")
                kwargs = request_model.auth.get("kwargs", {})
                auth_result = call_function(func_name, **kwargs)
            elif hasattr(request_model.auth, "kwargs"):
                # Model with .function.root and .kwargs
                auth_result = call_function(request_model.auth.function.root, **request_model.auth.kwargs)
            elif hasattr(request_model.auth, "root"):
                # Model with .root
                auth_result = call_function(request_model.auth.root)
            else:
                raise RequestError(f"Invalid auth function definition: {request_model.auth}")

            request_kwargs["auth"] = auth_result
        except Exception as e:
            raise RequestError(f"Failed to configure authentication: {str(e)}") from None

    with ExitStack() as stack:
        match request_model.body:
            case None:
                pass

            case JsonBody(json=data):
                request_kwargs["json"] = data

            case GraphQLBody(graphql=gql):
                # GraphQL is sent as JSON with query and variables
                request_kwargs["json"] = {"query": gql.query, "variables": gql.variables}

            case FormBody(form=data) | XmlBody(xml=data) | RawBody(raw=data):
                request_kwargs["data"] = data

            case FilesBody(files=file_paths):
                # File uploads - open files and keep them open for preparation
                files_dict = {}
                for field_name, file_path in file_paths.items():
                    try:
                        file_handle = stack.enter_context(open(file_path, "rb"))
                        files_dict[field_name] = (Path(file_path).name, file_handle)
                    except FileNotFoundError:
                        raise RequestError(f"File not found for upload: {file_path}") from None
                request_kwargs["files"] = files_dict

        try:
            req = requests.Request(**request_kwargs)
            prepared = session.prepare_request(req)
        except Exception as e:
            raise RequestError(f"Failed to prepare request: {str(e)}") from None

    send_kwargs = {
        "timeout": request_model.timeout,
        "allow_redirects": request_model.allow_redirects,
        "verify": request_model.ssl.verify,
    }
    if request_model.ssl.cert:
        send_kwargs["cert"] = request_model.ssl.cert

    return PrepareRequestResult(request=prepared, send_kwargs=send_kwargs)


def execute_request(
    session: requests.Session,
    prepared: PrepareRequestResult,
) -> requests.Response:
    """Execute a prepared HTTP request.

    Args:
        session: HTTP session to use for sending the request
        prepared: PreparedRequest dataclass with request and send kwargs

    Returns:
        HTTP response

    Raises:
        RequestError: If request execution fails
    """
    try:
        response = session.send(prepared.request, **prepared.send_kwargs)
        return response
    except requests.Timeout as e:
        raise RequestError(f"HTTP request timed out: {str(e)}") from None
    except requests.ConnectionError as e:
        raise RequestError(f"HTTP connection error: {str(e)}") from None
    except requests.RequestException as e:
        raise RequestError(f"HTTP request failed: {str(e)}") from None
    except Exception as e:
        raise RequestError(f"Unexpected error: {str(e)}") from None
