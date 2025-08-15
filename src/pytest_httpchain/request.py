"""HTTP request preparation and execution for chain tests.

This module handles the preparation of HTTP requests from test configurations
and their execution using the requests library.
"""

from contextlib import ExitStack
from pathlib import Path
from typing import Any

import requests
from pytest_httpchain_models.entities import (
    FilesBody,
    FormBody,
    JsonBody,
    RawBody,
    XmlBody,
)
from pytest_httpchain_models.entities import (
    Request as RequestModel,
)
from pytest_httpchain_userfunc.auth import call_auth_function

from .exceptions import RequestError
from .helpers import call_user_function


def prepare_and_execute(
    session: requests.Session,
    request_model: RequestModel,
) -> requests.Response:
    """Prepare and execute an HTTP request.

    This function combines preparation and execution to avoid unnecessary
    complexity. It handles authentication, different body types, and file
    uploads with proper resource management.

    Args:
        session: HTTP session to use for the request
        request_model: Validated request model

    Returns:
        HTTP response object

    Raises:
        RequestError: If request preparation or execution fails
    """

    # Base request kwargs
    kwargs: dict[str, Any] = {
        "method": request_model.method.value,
        "url": str(request_model.url),
        "headers": request_model.headers,
        "params": request_model.params,
        "timeout": request_model.timeout,
        "allow_redirects": request_model.allow_redirects,
        "verify": request_model.ssl.verify,
    }

    # Add SSL cert if present
    if request_model.ssl.cert:
        kwargs["cert"] = request_model.ssl.cert

    # Configure auth if present
    if request_model.auth:
        try:
            kwargs["auth"] = call_user_function(request_model.auth, call_auth_function)
        except Exception as e:
            raise RequestError("Failed to configure authentication") from e

    # Handle different body types
    match request_model.body:
        case None:
            pass
        case JsonBody(json=data):
            kwargs["json"] = data
        case FormBody(form=data) | XmlBody(xml=data) | RawBody(raw=data):
            kwargs["data"] = data
        case FilesBody(files=file_paths):
            # Handle file uploads with context manager
            with ExitStack() as stack:
                try:
                    files_dict = {}
                    for field_name, file_path in file_paths.items():
                        file_handle = stack.enter_context(open(file_path, "rb"))
                        files_dict[field_name] = (Path(file_path).name, file_handle)
                    kwargs["files"] = files_dict

                    return session.request(**kwargs)
                except FileNotFoundError as e:
                    raise RequestError("File not found for upload") from e

    try:
        return session.request(**kwargs)
    except requests.Timeout as e:
        raise RequestError("HTTP request timed out") from e
    except requests.ConnectionError as e:
        raise RequestError("HTTP connection error") from e
    except requests.RequestException as e:
        raise RequestError("HTTP request failed") from e
    except Exception as e:
        raise RequestError("Unexpected error") from e
