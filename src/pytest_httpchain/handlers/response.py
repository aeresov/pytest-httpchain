import logging
from typing import Any

import jmespath
import requests
from pytest_httpchain_engine.functions import VerificationFunction
from pytest_httpchain_engine.models.entities import Save, UserFunctionKwargs, UserFunctionName

logger = logging.getLogger(__name__)


class ResponseError(Exception):
    """An error processing HTTP response."""


class ResponseHandler:
    """Handles HTTP response processing and data extraction."""

    @staticmethod
    def get_json(response: requests.Response) -> dict[str, Any]:
        """Extract JSON from response with error handling."""
        try:
            return response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        except requests.JSONDecodeError as e:
            raise ResponseError("Error getting JSON from response") from e

    @staticmethod
    def save_data(response: requests.Response, model: Save) -> dict[str, Any]:
        """Extract and save data from response according to save model."""
        result: dict[str, Any] = {}

        if len(model.vars) > 0:
            result.update(ResponseHandler._save_variables(response, model.vars))

        for func_item in model.functions:
            result.update(ResponseHandler._execute_save_function(response, func_item))

        return result

    @staticmethod
    def _save_variables(response: requests.Response, vars_map: dict[str, str]) -> dict[str, Any]:
        """Save variables from response using JMESPath expressions."""
        result: dict[str, Any] = {}
        response_json = ResponseHandler.get_json(response)

        for var_name, jmespath_expr in vars_map.items():
            try:
                saved_value = jmespath.search(jmespath_expr, response_json)
                result[var_name] = saved_value
            except jmespath.exceptions.JMESPathError as e:
                raise ResponseError(f"Error saving variable {var_name}") from e

        return result

    @staticmethod
    def _execute_save_function(response: requests.Response, func_item: UserFunctionKwargs | UserFunctionName) -> dict[str, Any]:
        """Execute a save function and return its results."""
        try:
            match func_item:
                case UserFunctionKwargs():
                    return VerificationFunction.call_with_kwargs(func_item.function.root, response, func_item.kwargs)
                case UserFunctionName():
                    return VerificationFunction.call(func_item.root, response)
        except Exception as e:
            raise ResponseError(f"Error calling user function {func_item}") from e
