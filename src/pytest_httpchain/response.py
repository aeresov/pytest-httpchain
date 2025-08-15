"""Response processing and verification for HTTP chain tests.

This module handles the processing of HTTP responses including data extraction
(save operations) and verification of response content.
"""

import json
import re
from collections import ChainMap
from pathlib import Path
from typing import Any

import jmespath
import jsonschema
import requests
from pytest_httpchain_models.entities import Save, Verify
from pytest_httpchain_models.types import check_json_schema
from pytest_httpchain_userfunc.save import call_save_function
from pytest_httpchain_userfunc.verify import call_verify_function

from .exceptions import SaveError, VerificationError
from .helpers import call_user_function


def process_save_step(
    save_model: Save,
    response: requests.Response,
) -> dict[str, Any]:
    """Process a save step and return variables to be saved to global context.

    Extracts data from the response using:
    - JMESPath expressions for JSON responses
    - User-defined save functions for custom extraction

    Args:
        save_model: Validated Save model
        response: HTTP response object

    Returns:
        Dictionary of variables to add to global context

    Raises:
        ResponseError: If variable extraction fails

    Note:
        Save functions must conform to the SaveFunction protocol,
        accepting a response and returning a dict[str, Any].
    """
    result: dict[str, Any] = {}

    # Extract JSON only if we need it for JMESPath expressions
    if len(save_model.vars) > 0:
        try:
            response_json = response.json()
        except (requests.JSONDecodeError, UnicodeDecodeError) as e:
            raise SaveError("Cannot extract variables: response is not valid JSON") from e

        for var_name, jmespath_expr in save_model.vars.items():
            try:
                saved_value = jmespath.search(jmespath_expr, response_json)
                result[var_name] = saved_value
            except jmespath.exceptions.JMESPathError as e:
                raise SaveError(f"Error saving variable {var_name}") from e

    for func_item in save_model.functions:
        try:
            func_result = call_user_function(func_item, call_save_function, response)
            result.update(func_result)
        except Exception as e:
            raise SaveError(f"Error calling user function {func_item}") from e

    return result


def process_verify_step(
    verify_model: Verify,
    local_context: ChainMap[str, Any],
    response: requests.Response,
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
        verify_model: Validated Verify model
        local_context: Current execution context
        response: HTTP response object

    Raises:
        VerificationError: If any verification fails

    Note:
        Verify functions must conform to the VerifyFunction protocol,
        accepting a response and returning a bool.
    """

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
            result = call_user_function(func_item, call_verify_function, response)

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

        # Extract JSON for schema validation
        try:
            response_json = response.json()
        except (requests.JSONDecodeError, UnicodeDecodeError) as e:
            raise VerificationError("Cannot validate schema: response is not valid JSON") from e

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
