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
from collections import ChainMap
from contextlib import ExitStack
from pathlib import Path
from typing import Any, BinaryIO

import jmespath
import jsonschema
import pytest_httpchain_templates.substitution
import requests
from pydantic import JsonValue
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
    """Base exception for all stage execution errors.

    This is the base class for all exceptions that can occur during
    stage execution. Catching this will catch all stage-related errors.
    """


class RequestError(StageExecutionError):
    """Error during HTTP request preparation or execution.

    Raised when:
    - Request preparation fails (auth, file opening, etc.)
    - HTTP request times out
    - Connection errors occur
    - Other request-related issues
    """


class ResponseError(StageExecutionError):
    """Error during response processing (save operations).

    Raised when:
    - JMESPath expression fails
    - User save function fails
    - Variable extraction fails
    """


class VerificationError(StageExecutionError):
    """Error during response verification.

    Raised when:
    - Status code doesn't match expected
    - Headers don't match expected
    - Response body validation fails
    - User verify function returns False
    - JSON schema validation fails
    """


def prepare_data_context(
    scenario: Scenario,
    stage_template: Stage,
    global_context: dict[str, Any],
    fixture_kwargs: dict[str, Any],
) -> ChainMap[str, Any]:
    """Prepare the complete data context for stage execution.

    Uses ChainMap for efficient layered context management with lazy evaluation.
    No copying occurs - all layers share references to original data.

    Merges contexts in order of precedence (later overrides earlier):
    1. Global context (shared across all stages) - base layer
    2. Fixture values (from pytest fixtures)
    3. Scenario variables (from scenario.vars)
    4. Stage variables (from stage.vars) - top layer

    Each level can reference variables from previous levels in templates.

    Args:
        scenario: The scenario configuration
        stage_template: The stage being executed
        global_context: Shared context from previous stages
        fixture_kwargs: Pytest fixture values for this stage

    Returns:
        ChainMap with layered context for efficient lookups

    Note:
        Returns a ChainMap for full performance benefits:
        - No data copying
        - Lazy evaluation (only accesses what's needed)
        - Memory efficient (shares references)
        - O(1) for most lookups
    """
    # Build layers incrementally - each layer can reference previous ones
    # Template substitution now works directly with ChainMap

    # Layer 1: Base context (global + fixtures)
    base_context = ChainMap(fixture_kwargs, global_context)

    # Layer 2: Scenario variables (can reference base)
    scenario_vars = {}
    if scenario.vars:
        scenario_vars = pytest_httpchain_templates.substitution.walk(
            scenario.vars,
            base_context,  # Pass ChainMap directly
        )

    # Layer 3: Stage variables (can reference base + scenario)
    # Process stage vars incrementally so they can reference each other
    stage_vars = {}
    if stage_template.vars:
        context_with_scenario = ChainMap({}, scenario_vars, fixture_kwargs, global_context)
        for key, value in stage_template.vars.items():
            resolved_value = pytest_httpchain_templates.substitution.walk(value, context_with_scenario)
            stage_vars[key] = resolved_value
            # Add resolved var to context so next vars can reference it
            context_with_scenario.maps[0][key] = resolved_value

    # Create final context with proper precedence order
    # Stage vars override scenario vars, which override fixtures, which override global
    # Returns ChainMap for full performance benefits
    return ChainMap(stage_vars, scenario_vars, fixture_kwargs, global_context)


def prepare_request(request_dict: dict[str, Any], local_context: ChainMap[str, Any]) -> tuple[Request, dict[str, Any]]:
    """Prepare HTTP request parameters from request model.

    Converts the request configuration into parameters suitable for
    the requests library. Handles different body types, authentication,
    SSL configuration, and other request options.

    Args:
        request_dict: Raw request configuration (after template substitution)
        local_context: Local execution context (for auth functions)

    Returns:
        Tuple of (validated Request model, request parameters dict)

    Raises:
        RequestError: If authentication configuration fails
    """
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
    """Execute the HTTP request.

    Performs the actual HTTP request using the session and prepared parameters.
    Handles file uploads with proper resource management.

    Args:
        session: HTTP session to use for the request
        request_model: Validated request model with URL and method
        request_params: Parameters for requests.request()

    Returns:
        HTTP response object

    Raises:
        RequestError: For various HTTP and file-related errors
    """
    with ExitStack() as stack:
        try:
            if "files" in request_params:
                files_dict: dict[str, BinaryIO] = {}
                for field_name, file_path in request_params["files"].items():
                    files_dict[field_name] = stack.enter_context(open(file_path, "rb"))
                request_params["files"] = files_dict

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
    local_context: ChainMap[str, Any],
    response: requests.Response,
    response_json: JsonValue | None,
) -> dict[str, Any]:
    """Process a save step and return variables to be saved to global context.

    Extracts data from the response using:
    - JMESPath expressions for JSON responses
    - User-defined save functions for custom extraction

    Args:
        save_dict: Save step configuration
        local_context: Current execution context
        response: HTTP response object
        response_json: Parsed JSON response (empty dict if not JSON)

    Returns:
        Dictionary of variables to add to global context

    Raises:
        ResponseError: If variable extraction fails
    """
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
    local_context: ChainMap[str, Any],
    response: requests.Response,
    response_json: JsonValue | None,
) -> None:
    """Process a verify step and raise errors if verification fails.

    Performs various verifications on the response:
    - Status code matching
    - Header value matching
    - Variable value matching
    - JSON schema validation
    - Body content checks (contains/not_contains/matches/not_matches)
    - User-defined verify functions

    Args:
        verify_dict: Verify step configuration
        local_context: Current execution context
        response: HTTP response object
        response_json: Parsed JSON response (empty dict if not JSON)

    Raises:
        VerificationError: If any verification fails
    """
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
    """Execute a single stage and return context updates.

    This is the main entry point for stage execution. It orchestrates:
    1. Context preparation (merge global + fixtures + variables)
    2. Template substitution for all stage elements
    3. HTTP request preparation and execution
    4. Response processing (save and verify steps)
    5. Return updates for global context

    Args:
        stage_template: The stage definition (with templates)
        scenario: The scenario configuration
        session: HTTP session for requests
        global_context: Shared context from previous stages (read-only)
        fixture_kwargs: Values from pytest fixtures

    Returns:
        Context updates to be merged into global context.
        Only includes variables from SaveStep operations.

    Raises:
        StageExecutionError: Base exception for any stage execution failure
            - RequestError: HTTP request preparation/execution failed
            - ResponseError: Response processing (save) failed
            - VerificationError: Response verification failed

    Note:
        The function maintains a clear separation between global and local
        context. Only SaveStep results are returned for global updates.
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
    response_json: JsonValue | None
    try:
        if response.headers.get("content-type", "").startswith("application/json"):
            response_json = response.json()
        else:
            response_json = {}
    except requests.JSONDecodeError:
        response_json = {}

    for step in response_model:
        match step:
            case SaveStep():
                save_dict = pytest_httpchain_templates.substitution.walk(step.save, local_context)
                saved_vars = process_save_step(save_dict, local_context, response, response_json)
                # Add saved vars as a new layer in ChainMap for subsequent steps
                local_context = local_context.new_child(saved_vars)
                global_context_updates.update(saved_vars)

            case VerifyStep():
                verify_dict = pytest_httpchain_templates.substitution.walk(step.verify, local_context)
                process_verify_step(verify_dict, local_context, response, response_json)

    # Return only the updates that should persist globally
    return global_context_updates
