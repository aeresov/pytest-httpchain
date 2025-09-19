"""Response processing and verification for HTTP chain tests.

This module handles the processing of HTTP responses including data extraction
(save operations) and verification of response content.
"""

import json
import logging
import re
from collections import ChainMap
from pathlib import Path
from typing import Any

import jmespath
import jmespath.exceptions
import jsonschema
import requests
from pytest_httpchain_models.entities import Save, Verify
from pytest_httpchain_models.types import check_json_schema

from .exceptions import SaveError, VerificationError
from .utils import call_user_function

logger = logging.getLogger(__name__)


def process_save_step(
    save_model: Save,
    local_context: ChainMap[str, Any],
    response: requests.Response,
) -> dict[str, Any]:
    """Process a save step and return variables to be saved to global context.

    Extracts data from the response using:
    - JMESPath expressions for JSON responses
    - User-defined save functions for custom extraction
    - Substitutions for variable processing

    Args:
        save_model: Validated Save model
        local_context: Current execution context for template substitution
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
    if len(save_model.jmespath) > 0:
        try:
            response_json = response.json()
        except (requests.JSONDecodeError, UnicodeDecodeError) as e:
            raise SaveError(f"Cannot extract variables, response is not valid JSON: {str(e)}") from None

        for var_name, jmespath_expr in save_model.jmespath.items():
            logger.info(f"JMESPath processing: {jmespath_expr}")
            try:
                saved_value = jmespath.search(jmespath_expr, response_json)
                result[var_name] = saved_value
                logger.info(f"Saved {var_name} = {saved_value}")
            except jmespath.exceptions.JMESPathError as e:
                raise SaveError(f"Error saving variable {var_name}: {str(e)}") from None

    # Process substitutions (sequential substitution steps like in Scenario)
    # Build context for substitutions including already saved values
    import pytest_httpchain_templates.substitution

    # Create context with current results added
    substitution_context = local_context.new_child(result)

    for step in save_model.substitutions:
        if step.vars:
            for var_name, var_value in step.vars.items():
                # Evaluate the value with the current context
                resolved_value = pytest_httpchain_templates.substitution.walk(var_value, substitution_context)
                result[var_name] = resolved_value
                # Update context for next iterations
                substitution_context = substitution_context.new_child({var_name: resolved_value})
                logger.info(f"Saved {var_name} = {resolved_value}")

        if step.functions:
            for alias, func_def in step.functions.items():
                # These are function definitions that should be saved as callable wrappers
                # This follows the same pattern as Scenario.substitutions
                from pytest_httpchain_models.entities import UserFunctionKwargs, UserFunctionName
                from pytest_httpchain_userfunc.userfunc import wrap_function

                match func_def:
                    case UserFunctionName():
                        result[alias] = wrap_function(func_def.root)
                    case UserFunctionKwargs():
                        result[alias] = wrap_function(func_def.name.root, default_kwargs=func_def.kwargs)
                    case _:
                        raise SaveError(f"Invalid function definition for '{alias}': expected UserFunctionName or UserFunctionKwargs")
                logger.info(f"Saved {alias} = {result[alias]} (function)")

    for func_item in save_model.user_functions:
        try:
            func_result = call_user_function(func_item, response=response)

            if not isinstance(func_result, dict):
                raise SaveError(f"Save function must return dict, got {type(func_result).__name__}")

            result.update(func_result)
            for var_name, saved_value in func_result.items():
                logger.info(f"Saved {var_name} = {saved_value} (user_function)")
        except Exception as e:
            raise SaveError(f"Error calling user function '{func_item}': {str(e)}") from None

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
    - Expression evaluation
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

    for i, expression in enumerate(verify_model.expressions):
        # Expression may already be evaluated by substitution.walk in carrier.py
        # or it might still be a template string (shouldn't happen in normal flow)
        if isinstance(expression, str) and expression.startswith("{{") and expression.endswith("}}"):
            # This shouldn't happen in normal flow but handle it just in case
            import pytest_httpchain_templates.substitution

            try:
                result = pytest_httpchain_templates.substitution._sub_string(expression, local_context)
                logger.info(f"Checking expression {i}: '{expression}' evaluated to {result} (type: {type(result).__name__})")
            except Exception as e:
                raise VerificationError(f"Error evaluating expression {i} '{expression}': {str(e)}") from None
        else:
            # Already evaluated
            result = expression
            logger.info(f"Checking expression {i}: result = {result} (type: {type(result).__name__})")

        # Check if the expression is truthy
        if not result:
            raise VerificationError(f"Expression {i} failed: evaluated to {result}")

    for func_item in verify_model.user_functions:
        try:
            from .utils import call_user_function

            result = call_user_function(func_item, response=response)

            if not isinstance(result, bool):
                raise VerificationError(f"Verify function must return bool, got {type(result).__name__}")

            if not result:
                raise VerificationError(f"Function '{func_item}' verification failed")

        except Exception as e:
            raise VerificationError(f"Error calling user function '{func_item}': {str(e)}") from None

    if verify_model.body.schema:
        schema = verify_model.body.schema
        if isinstance(schema, str | Path):
            schema_path = Path(schema)
            try:
                schema = json.loads(schema_path.read_text())
                check_json_schema(schema)
            except (OSError, json.JSONDecodeError) as e:
                raise VerificationError(f"Error reading body schema file '{schema_path}': {str(e)}") from None
            except jsonschema.SchemaError as e:
                raise VerificationError(f"Invalid JSON Schema in file '{schema_path}': {e}") from None

        # Extract JSON for schema validation
        try:
            response_json = response.json()
        except (requests.JSONDecodeError, UnicodeDecodeError) as e:
            raise VerificationError(f"Cannot validate schema, response is not valid JSON: {str(e)}") from None

        try:
            jsonschema.validate(instance=response_json, schema=schema)
        except jsonschema.ValidationError as e:
            raise VerificationError(f"Body schema validation failed: {str(e)}") from None
        except jsonschema.SchemaError as e:
            raise VerificationError(f"Invalid body validation schema: {str(e)}") from None

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
