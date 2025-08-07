"""Stage execution logic for HTTP chain tests.

This module handles the execution of individual test stages, maintaining
a clear separation between:

1. Global Context: Shared state that persists across all stages in a scenario.
   - Initialized empty at scenario start
   - Updated only by SaveStep operations
   - Passed between stages via Carrier._data_context

2. Local Context: Stage-specific execution context that includes:
   - Copy of global context (read-only base)
   - Fixture values from pytest
   - Scenario-level variables
   - Stage-level variables
   - Temporary variables during stage execution

The flow for each stage:
1. Build local context = global + fixtures + scenario.vars + stage.vars
2. Execute requests and process responses using local context
3. Return only SaveStep results to update global context

Exceptions:
- StageExecutionError: Base exception for all stage failures
  - RequestError: HTTP request issues
  - ResponseError: Response processing issues
  - VerificationError: Verification failures
"""

import json
import logging
import re
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from typing import Any

import jmespath
import jsonschema
import pytest_httpchain_templates.substitution
import requests
from pytest_httpchain_models.entities import (
    FilesBody,
    FormBody,
    JsonBody,
    RawBody,
    Request,
    Response,
    Save,
    SaveStep,
    Scenario,
    Stage,
    UserFunctionKwargs,
    Verify,
    VerifyStep,
    XmlBody,
)
from pytest_httpchain_models.types import check_json_schema
from pytest_httpchain_userfunc.auth import call_auth_function
from pytest_httpchain_userfunc.save import call_save_function
from pytest_httpchain_userfunc.verify import call_verify_function

logger = logging.getLogger(__name__)


class StageExecutionError(Exception):
    """Base exception for all stage execution errors."""


class RequestError(StageExecutionError):
    """Error during HTTP request preparation or execution."""


class ResponseError(StageExecutionError):
    """Error during response processing (save operations)."""


class VerificationError(StageExecutionError):
    """Error during response verification."""


def prepare_data_context(
    scenario: Scenario,
    stage_template: Stage,
    global_context: dict[str, Any],
    fixture_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Prepare the complete data context for stage execution.

    Merges contexts in order of precedence:
    1. Global context (shared across all stages)
    2. Fixture values (from pytest fixtures)
    3. Scenario variables (from scenario.vars)
    4. Stage variables (from stage.vars)

    Returns:
        dict: Complete context for stage execution (local context)
    """
    # Start with a copy of the global context (shared state)
    local_context = deepcopy(global_context)

    # Add fixture values
    local_context.update(fixture_kwargs)

    # Apply scenario-level variables (can reference fixtures and global context)
    scenario_vars = pytest_httpchain_templates.substitution.walk(scenario.vars, local_context)
    local_context.update(scenario_vars)

    # Apply stage-level variables (can reference all above)
    stage_vars = pytest_httpchain_templates.substitution.walk(stage_template.vars, local_context)
    local_context.update(stage_vars)

    return local_context


def prepare_request(request_dict: dict[str, Any], local_context: dict[str, Any]) -> tuple[Request, dict[str, Any]]:
    """Prepare HTTP request parameters from request model."""
    request_model = Request.model_validate(request_dict)

    # Prepare request parameters directly
    request_params: dict[str, Any] = {
        "timeout": request_model.timeout,
        "allow_redirects": request_model.allow_redirects,
        "params": request_model.params,
        "headers": request_model.headers,
        # SSL configuration
        "verify": request_model.ssl.verify,
        **({"cert": request_model.ssl.cert} if request_model.ssl.cert else {}),
    }

    # Configure auth if present
    if request_model.auth:
        try:
            if isinstance(request_model.auth, UserFunctionKwargs):
                request_params["auth"] = call_auth_function(request_model.auth.function.root, **request_model.auth.kwargs)
            else:  # UserFunctionName
                request_params["auth"] = call_auth_function(request_model.auth.root)
        except Exception as e:
            raise RequestError("Failed to configure stage authentication") from e

    # Add body to params
    match request_model.body:
        case None:
            pass
        case JsonBody(json=data):
            request_params["json"] = data
        case FormBody(form=data):
            request_params["data"] = data
        case XmlBody(xml=data):
            request_params["data"] = data
        case RawBody(raw=data):
            request_params["data"] = data
        case FilesBody(files=data):
            request_params["files"] = data

    return request_model, request_params


def execute_request(session: requests.Session, request_model: Request, request_params: dict[str, Any]) -> requests.Response:
    """Execute the HTTP request."""
    with ExitStack() as stack:
        try:
            if "files" in request_params:
                request_params["files"] = {field_name: stack.enter_context(open(file_path, "rb")) for field_name, file_path in request_params["files"]}

            response = session.request(request_model.method.value, request_model.url, **request_params)

        except FileNotFoundError as e:
            raise RequestError("File not found for upload") from e
        except requests.Timeout as e:
            raise RequestError("HTTP request timed out") from e
        except requests.ConnectionError as e:
            raise RequestError("HTTP connection error") from e
        except requests.RequestException as e:
            raise RequestError("HTTP request failed") from e
        except Exception as e:
            raise RequestError("Unexpected error") from e

    return response


def process_save_step(
    save_dict: dict[str, Any],
    local_context: dict[str, Any],
    response: requests.Response,
    response_json: dict[str, Any],
) -> dict[str, Any]:
    """Process a save step and return variables to be saved to global context."""
    save_model = Save.model_validate(save_dict)
    result: dict[str, Any] = {}

    if len(save_model.vars) > 0:
        for var_name, jmespath_expr in save_model.vars.items():
            try:
                saved_value = jmespath.search(jmespath_expr, response_json)
                result[var_name] = saved_value
            except jmespath.exceptions.JMESPathError as e:
                raise ResponseError(f"Error saving variable {var_name}") from e

    for func_item in save_model.functions:
        try:
            if isinstance(func_item, UserFunctionKwargs):
                func_result = call_save_function(func_item.function.root, response, **func_item.kwargs)
            else:  # UserFunctionName
                func_result = call_save_function(func_item.root, response)
            result.update(func_result)
        except Exception as e:
            raise ResponseError(f"Error calling user function {func_item}") from e

    return result


def process_verify_step(
    verify_dict: dict[str, Any],
    local_context: dict[str, Any],
    response: requests.Response,
    response_json: dict[str, Any],
) -> None:
    """Process a verify step and raise errors if verification fails."""
    verify_model = Verify.model_validate(verify_dict)

    if verify_model.status and response.status_code != verify_model.status.value:
        raise VerificationError(f"Status code doesn't match: expected {verify_model.status.value}, got {response.status_code}")

    for header_name, expected_value in verify_model.headers.items():
        if response.headers.get(header_name) != expected_value:
            raise VerificationError(f"Header '{header_name}' doesn't match: expected {expected_value}, got {response.headers.get(header_name)}")

    for var_name, expected_value in verify_model.vars.items():
        if var_name not in local_context:
            raise VerificationError(f"Var '{var_name}' not found in data context")
        if local_context[var_name] != expected_value:
            raise VerificationError(f"Var '{var_name}' verification failed: expected {expected_value}, got {local_context[var_name]}")

    for func_item in verify_model.functions:
        try:
            if isinstance(func_item, UserFunctionKwargs):
                result = call_verify_function(func_item.function.root, response, **func_item.kwargs)
            else:  # UserFunctionName
                result = call_verify_function(func_item.root, response)

            if not result:
                raise VerificationError(f"Function '{func_item}' verification failed")

        except Exception as e:
            raise VerificationError(f"Error calling user function '{func_item}'") from e

    if verify_model.body.schema:
        schema = verify_model.body.schema
        if isinstance(schema, str | Path):
            schema_path = Path(schema)
            try:
                schema = json.loads(schema_path.read_text())
                check_json_schema(schema)
            except (OSError, json.JSONDecodeError) as e:
                raise VerificationError(f"Error reading body schema file '{schema_path}'") from e
            except jsonschema.SchemaError as e:
                raise VerificationError(f"Invalid JSON Schema in file '{schema_path}': {e.message}") from e

        try:
            jsonschema.validate(instance=response_json, schema=schema)
        except jsonschema.ValidationError as e:
            raise VerificationError("Body schema validation failed") from e
        except jsonschema.SchemaError as e:
            raise VerificationError("Invalid body validation schema") from e

    for substring in verify_model.body.contains:
        if substring not in response.text:
            raise VerificationError(f"Body doesn't contain '{substring}'")

    for substring in verify_model.body.not_contains:
        if substring in response.text:
            raise VerificationError(f"Body contains '{substring}' while it shouldn't")

    for pattern in verify_model.body.matches:
        if not re.search(pattern, response.text):
            raise VerificationError(f"Body doesn't match '{pattern}'")

    for pattern in verify_model.body.not_matches:
        if re.search(pattern, response.text):
            raise VerificationError(f"Body matches '{pattern}' while it shouldn't")


def execute_stage(
    stage_template: Stage,
    scenario: Scenario,
    session: requests.Session,
    global_context: dict[str, Any],
    fixture_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """
    Execute a single stage and return context updates.

    Args:
        stage_template: The stage definition (with templates)
        scenario: The scenario configuration
        session: HTTP session for requests
        global_context: Shared context from previous stages (read-only)
        fixture_kwargs: Values from pytest fixtures

    This function handles:
    - Data context preparation (merging global + local)
    - Template substitution
    - Request execution
    - Response processing (save & verify)

    Returns:
        dict: Context updates to be merged into global context
    Raises:
        StageExecutionError: Base exception for any stage execution failure
            - RequestError: HTTP request preparation/execution failed
            - ResponseError: Response processing (save) failed
            - VerificationError: Response verification failed
    """
    # Build local context for this stage (global + fixtures + vars)
    local_context = prepare_data_context(scenario=scenario, stage_template=stage_template, global_context=global_context, fixture_kwargs=fixture_kwargs)

    # Resolve stage template with complete local context
    stage = pytest_httpchain_templates.substitution.walk(stage_template, local_context)

    # Prepare request with resolved templates
    request_dict = pytest_httpchain_templates.substitution.walk(stage.request, local_context)
    request_model, request_params = prepare_request(request_dict, local_context)

    # Execute request
    response = execute_request(session, request_model, request_params)

    # Process response
    response_dict = pytest_httpchain_templates.substitution.walk(stage.response, local_context)
    response_model = Response.model_validate(response_dict)

    # Track what needs to be saved to global context
    global_context_updates: dict[str, Any] = {}

    # Extract JSON once for reuse
    try:
        response_json = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    except requests.JSONDecodeError:
        response_json = {}

    for step in response_model:
        match step:
            case SaveStep():
                save_dict = pytest_httpchain_templates.substitution.walk(step.save, local_context)
                saved_vars = process_save_step(save_dict, local_context, response, response_json)
                local_context.update(saved_vars)
                global_context_updates.update(saved_vars)

            case VerifyStep():
                verify_dict = pytest_httpchain_templates.substitution.walk(step.verify, local_context)
                process_verify_step(verify_dict, local_context, response, response_json)

    # Return only the updates that should persist globally
    return global_context_updates
