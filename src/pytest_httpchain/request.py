import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from pytest_httpchain_models import (
    Base64Body,
    BinaryBody,
    FilesBody,
    FormBody,
    GraphQLBody,
    JsonBody,
    TextBody,
    XmlBody,
)
from pytest_httpchain_models import (
    Request as RequestModel,
)

from .exceptions import RequestError
from .utils import call_user_function


@dataclass
class PrepareRequestResult:
    request_kwargs: dict[str, Any] = field(default_factory=dict)
    last_request: httpx.Request | None = None


def prepare_request(
    client: httpx.Client,
    request_model: RequestModel,
) -> PrepareRequestResult:
    request_kwargs: dict[str, Any] = {
        "method": request_model.method,
        "url": str(request_model.url),
        "headers": request_model.headers,
        "params": request_model.params,
        "timeout": request_model.timeout,
        "follow_redirects": request_model.allow_redirects,
    }

    if request_model.auth:
        try:
            auth_result = call_user_function(request_model.auth)
            request_kwargs["auth"] = auth_result
        except Exception as e:
            raise RequestError(f"Failed to configure authentication: {str(e)}") from None

    match request_model.body:
        case None:
            pass

        case JsonBody(json=data):
            request_kwargs["json"] = data

        case GraphQLBody(graphql=gql):
            # GraphQL is sent as JSON with query and variables
            request_kwargs["json"] = {"query": gql.query, "variables": gql.variables}

        case FormBody(form=data):
            request_kwargs["data"] = data

        case XmlBody(xml=data) | TextBody(text=data):
            request_kwargs["content"] = data

        case Base64Body(base64=encoded_data):
            decoded_data = base64.b64decode(encoded_data)
            request_kwargs["content"] = decoded_data

        case BinaryBody(binary=file_path):
            try:
                with open(file_path, "rb") as f:
                    binary_data = f.read()
                request_kwargs["content"] = binary_data
            except FileNotFoundError:
                raise RequestError(f"Binary file not found: {file_path}") from None

        case FilesBody(files=file_paths):
            files_list = []
            for field_name, file_path in file_paths.items():
                try:
                    with open(file_path, "rb") as f:
                        file_content = f.read()
                    files_list.append((field_name, (Path(file_path).name, file_content)))
                except FileNotFoundError:
                    raise RequestError(f"File not found for upload: {file_path}") from None
            request_kwargs["files"] = files_list

    return PrepareRequestResult(request_kwargs=request_kwargs)


def execute_request(
    client: httpx.Client,
    prepared: PrepareRequestResult,
) -> httpx.Response:
    try:
        response = client.request(**prepared.request_kwargs)
        prepared.last_request = response.request
        return response
    except httpx.TimeoutException as e:
        raise RequestError(f"HTTP request timed out: {str(e)}") from None
    except httpx.ConnectError as e:
        raise RequestError(f"HTTP connection error: {str(e)}") from None
    except httpx.HTTPError as e:
        raise RequestError(f"HTTP request failed: {str(e)}") from None
    except Exception as e:
        raise RequestError(f"Unexpected error: {str(e)}") from None
