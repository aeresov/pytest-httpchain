import json
import logging
import re
from collections import ChainMap
from pathlib import Path
from typing import Any

import httpx
import jmespath
import jmespath.exceptions
import jsonschema
from pytest_httpchain_models.entities import Save, Verify
from pytest_httpchain_models.types import check_json_schema

from .exceptions import SaveError, VerificationError
from .utils import call_user_function, process_substitutions

logger = logging.getLogger(__name__)


def process_save_step(
    save_model: Save,
    local_context: ChainMap[str, Any],
    response: httpx.Response,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    step_context = local_context.new_child(result)

    # first jmespath
    if len(save_model.jmespath) > 0:
        try:
            response_json = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise SaveError(f"Cannot extract variables, response is not valid JSON: {str(e)}") from None

        for var_name, jmespath_expr in save_model.jmespath.items():
            logger.info(f"JMESPath processing: {jmespath_expr}")
            try:
                saved_value = jmespath.search(jmespath_expr, response_json)
                result[var_name] = saved_value
                logger.info(f"Saved {var_name} = {saved_value}")
            except jmespath.exceptions.JMESPathError as e:
                raise SaveError(f"Error saving variable {var_name}: {str(e)}") from None

    # then substitutions
    try:
        substitution_result = process_substitutions(save_model.substitutions, step_context)
        result.update(substitution_result)
    except Exception as e:
        raise SaveError(f"Error processing substitutions: {str(e)}") from None

    # last user_functions
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

    # return only step-generated data
    return result


def process_verify_step(
    verify_model: Verify,
    local_context: ChainMap[str, Any],
    response: httpx.Response,
) -> None:
    if verify_model.status and response.status_code != verify_model.status.value:
        raise VerificationError(f"Status code doesn't match: expected {verify_model.status.value}, got {response.status_code}")

    for header_name, expected_value in verify_model.headers.items():
        if response.headers.get(header_name) != expected_value:
            raise VerificationError(f"Header '{header_name}' doesn't match: expected {expected_value}, got {response.headers.get(header_name)}")

    for i, expression in enumerate(verify_model.expressions):
        if not expression:
            raise VerificationError(f"Expression {i} failed: evaluated to {expression}")

    for func_item in verify_model.user_functions:
        try:
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

        try:
            response_json = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
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
