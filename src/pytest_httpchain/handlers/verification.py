import json
import logging
import re
from pathlib import Path
from typing import Any

import jsonschema
import requests
from pytest_httpchain_engine.functions import VerificationFunction
from pytest_httpchain_engine.models.entities import UserFunctionKwargs, UserFunctionName, Verify
from pytest_httpchain_engine.models.types import check_json_schema

from pytest_httpchain.handlers.response import ResponseHandler

logger = logging.getLogger(__name__)


class VerificationError(Exception):
    """An error during response verification."""


class VerificationHandler:
    """Handles response verification against expected values."""

    @staticmethod
    def verify(response: requests.Response, model: Verify, context: dict[str, Any]) -> None:
        """Verify response against the verification model."""
        if model.status:
            VerificationHandler._verify_status(response, model.status.value)

        VerificationHandler._verify_headers(response, model.headers)

        VerificationHandler._verify_variables(model.vars, context)
        VerificationHandler._verify_functions(response, model.functions)
        VerificationHandler._verify_body(response, model.body)

    @staticmethod
    def _verify_status(response: requests.Response, expected_status: int) -> None:
        """Verify HTTP status code."""
        actual_status = response.status_code
        if actual_status != expected_status:
            raise VerificationError(f"Status code doesn't match: expected {expected_status}, got {actual_status}")

    @staticmethod
    def _verify_headers(response: requests.Response, expected_headers: dict[str, str]) -> None:
        """Verify response headers."""
        for header_name, expected_value in expected_headers.items():
            actual_value = response.headers.get(header_name)
            if actual_value != expected_value:
                raise VerificationError(f"Header '{header_name}' doesn't match: expected {expected_value}, got {actual_value}")

    @staticmethod
    def _verify_variables(expected_vars: dict[str, Any], context: dict[str, Any]) -> None:
        """Verify variables in the context."""
        for var_name, expected_value in expected_vars.items():
            if var_name not in context:
                raise VerificationError(f"Var '{var_name}' not found in data context")

            actual_value = context[var_name]
            if actual_value != expected_value:
                raise VerificationError(f"Var '{var_name}' verification failed: expected {expected_value}, got {actual_value}")

    @staticmethod
    def _verify_functions(
        response: requests.Response,
        functions: list[UserFunctionKwargs | UserFunctionName],
    ) -> None:
        """Execute verification functions."""
        for func_item in functions:
            try:
                match func_item:
                    case UserFunctionKwargs():
                        result = VerificationFunction.call_with_kwargs(func_item.function.root, response, func_item.kwargs)
                    case UserFunctionName():
                        result = VerificationFunction.call(func_item.root, response)

                if not result:
                    raise VerificationError(f"Function '{func_item}' verification failed")

            except Exception as e:
                raise VerificationError(f"Error calling user function '{func_item}'") from e

    @staticmethod
    def _verify_body(response: requests.Response, body_model) -> None:
        """Verify response body content."""
        if body_model.schema:
            VerificationHandler._verify_json_schema(response, body_model.schema)

        VerificationHandler._verify_substring_patterns(response.text, body_model)
        VerificationHandler._verify_regex_patterns(response.text, body_model)

    @staticmethod
    def _verify_json_schema(response: requests.Response, schema) -> None:
        """Verify response JSON against a schema."""
        response_json = ResponseHandler.get_json(response)

        match schema:
            case str() | Path():
                schema_path = Path(schema)
                try:
                    with schema_path.open() as f:
                        schema_dict = json.load(f)
                except (FileNotFoundError, OSError, PermissionError, UnicodeDecodeError, json.JSONDecodeError) as e:
                    raise VerificationError(f"Error reading body schema file '{schema_path}'") from e

                try:
                    check_json_schema(schema_dict)
                except jsonschema.SchemaError as e:
                    raise VerificationError(f"Invalid JSON Schema in file '{schema_path}': {e.message}") from e

                schema = schema_dict

            case dict():
                # Use inline schema (already validated by Pydantic)
                pass

        try:
            jsonschema.validate(instance=response_json, schema=schema)
        except jsonschema.ValidationError as e:
            raise VerificationError("Body schema validation failed") from e
        except jsonschema.SchemaError as e:
            raise VerificationError("Invalid body validation schema") from e

    @staticmethod
    def _verify_substring_patterns(text: str, body_model) -> None:
        """Verify substring patterns in response body."""
        for substring in body_model.contains:
            if substring not in text:
                raise VerificationError(f"Body doesn't contain '{substring}'")

        for substring in body_model.not_contains:
            if substring in text:
                raise VerificationError(f"Body contains '{substring}' while it shouldn't")

    @staticmethod
    def _verify_regex_patterns(text: str, body_model) -> None:
        """Verify regex patterns in response body."""
        for pattern in body_model.matches:
            if not re.search(pattern, text):
                raise VerificationError(f"Body doesn't match '{pattern}'")

        for pattern in body_model.not_matches:
            if re.search(pattern, text):
                raise VerificationError(f"Body matches '{pattern}' while it shouldn't")
