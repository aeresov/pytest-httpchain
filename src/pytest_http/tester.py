import json
import re
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import jmespath
import jsonschema
import pytest_http_engine.models.entities
import requests
from pytest_http_engine.user_function import UserFunction


class TesterError(Exception):
    """An error making HTTP call."""


def call(session: requests.Session, model: pytest_http_engine.models.entities.Request) -> requests.Response:
    request_params: dict[str, Any] = {}
    if model.params:
        request_params["params"] = model.params
    if model.headers:
        request_params["headers"] = model.headers
    request_params["timeout"] = model.timeout
    request_params["allow_redirects"] = model.allow_redirects

    # Add SSL configuration for this specific request (overrides session SSL)
    if model.ssl:
        if model.ssl.verify is not None:
            request_params["verify"] = model.ssl.verify
        if model.ssl.cert is not None:
            request_params["cert"] = model.ssl.cert

    # Add authentication for this specific request (overrides session auth)
    if model.auth:
        try:
            auth_instance = UserFunction.call_auth_function_from_spec(model.auth)
            request_params["auth"] = auth_instance
        except Exception as e:
            raise TesterError("Failed to configure stage authentication") from e

    # Handle different body types
    match model.body:
        case None:
            pass
        case pytest_http_engine.models.full.JsonBody(json=data):
            request_params["json"] = data
        case pytest_http_engine.models.full.FormBody(form=data):
            request_params["data"] = data
        case pytest_http_engine.models.full.XmlBody(xml=data):
            request_params["data"] = data
        case pytest_http_engine.models.full.RawBody(raw=data):
            request_params["data"] = data
        case pytest_http_engine.models.full.FilesBody(files=data):
            request_params["files"] = data

    with ExitStack() as stack:
        try:
            if "files" in request_params:
                request_params["files"] = {field_name: stack.enter_context(open(file_path, "rb")) for field_name, file_path in request_params["files"]}

            return session.request(model.method.value, model.url, **request_params)
        except FileNotFoundError as e:
            raise TesterError("File not found for upload") from e
        except requests.Timeout as e:
            raise TesterError("HTTP request timed out") from e
        except requests.ConnectionError as e:
            raise TesterError("HTTP connection error") from e
        except requests.RequestException as e:
            raise TesterError("HTTP request failed") from e
        except Exception as e:
            raise TesterError("Unexpected error") from e


def _get_response_json(response: requests.Response) -> dict[str, Any]:
    try:
        return response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    except requests.JSONDecodeError as e:
        raise TesterError("Error getting jSON from response") from e


def save(response: requests.Response, model: pytest_http_engine.models.entities.Save) -> dict[str, Any]:
    result: dict[str, Any] = {}

    # vars first
    if len(model.vars) > 0:
        response_json: dict[str, Any] = _get_response_json(response)
        for var_name, jmespath_expr in model.vars.items():
            try:
                saved_value = jmespath.search(jmespath_expr, response_json)
                result[var_name] = saved_value
            except jmespath.exceptions.JMESPathError as e:
                raise TesterError(f"Error saving variable {var_name}") from e

    # functions next
    for func_item in model.functions:
        try:
            match func_item:
                case str():
                    result.update(UserFunction.call_with_kwargs(func_item, response, None))
                case pytest_http_engine.models.full.FunctionCall():
                    result.update(UserFunction.call_with_kwargs(func_item.function, response, func_item.kwargs))
        except Exception as e:
            raise TesterError(f"Error calling user function {func_item}") from e

    return result


def verify(response: requests.Response, model: pytest_http_engine.models.entities.Verify, context: dict[str, Any]):
    # HTTP status code
    if model.status:
        expected_value = model.status.value
        actual_value = response.status_code
        if actual_value != expected_value:
            raise TesterError(f"Status code doesn't match: expected {expected_value}, got {actual_value}")

    # headers
    for header_name, expected_value in model.headers.items():
        actual_value = response.headers.get(header_name)
        if actual_value != expected_value:
            raise TesterError(f"Header '{header_name}' doesn't match: expected {expected_value}, got {actual_value}")

    # vars
    for var_name, expected_value in model.vars.items():
        if var_name not in context:
            raise TesterError(f"Var '{var_name}' not found in data context")
        actual_value = context[var_name]
        if actual_value != expected_value:
            raise TesterError(f"Var '{var_name}' verification failed: expected {expected_value}, got {actual_value}")

    # user functions
    for func_item in model.functions:
        try:
            match func_item:
                case str():
                    actual_value = UserFunction.call_with_kwargs(func_item, response, None)
                case pytest_http_engine.models.full.FunctionCall():
                    actual_value = UserFunction.call_with_kwargs(func_item.function, response, func_item.kwargs)
        except Exception as e:
            raise TesterError(f"Error calling user function '{func_item}'") from e

        if not actual_value:
            raise TesterError(f"Function '{func_item}' verification failed")

    # body
    ## JSON schema if available
    if model.body.schema:
        response_json: dict[str, Any] = _get_response_json(response)
        match model.body.schema:
            case str() | Path():
                # load schema from path
                schema_path = Path(model.body.schema)
                try:
                    with schema_path.open() as f:
                        schema = json.load(f)
                except (FileNotFoundError, OSError, PermissionError, UnicodeDecodeError, json.JSONDecodeError) as e:
                    raise TesterError(f"Error reading body schema file '{schema_path}'") from e
            case dict():
                # use inline schema
                schema = model.body.schema
        try:
            jsonschema.validate(instance=response_json, schema=schema)
        except jsonschema.ValidationError as e:
            raise TesterError("Body schema validation failed") from e
        except jsonschema.SchemaError as e:
            raise TesterError("Invalid body validation schema") from e

    ## substrings
    for substring in model.body.contains:
        if substring not in response.text:
            raise TesterError(f"Body doesn't contain '{substring}'")

    for substring in model.body.not_contains:
        if substring in response.text:
            raise TesterError(f"Body contains '{substring}' while it shouldn't")

    ## regex
    for pattern in model.body.matches:
        if not re.search(pattern, response.text):
            raise TesterError(f"Body doesn't match '{pattern}'")

    for pattern in model.body.not_matches:
        if re.search(pattern, response.text):
            raise TesterError(f"Body matches '{pattern}' while it shouldn't")
