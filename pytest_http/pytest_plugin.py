import inspect
import json
import logging
import re
import sys
from collections.abc import Iterable
from functools import reduce
from pathlib import Path
from typing import Any

import jmespath
import jsonref
import pytest
import requests
from _pytest.config import Config
from _pytest.config.argparsing import Parser
from _pytest.nodes import Collector, Item
from _pytest.python import Function
from pydantic import ValidationError

from pytest_http.models import FunctionCall, Scenario, Stage


def pytest_addoption(parser: Parser):
    parser.addini(
        "suffix",
        default="http",
        help="File suffix for HTTP test files (default: http). Must contain only alphanumeric characters, underscores, and hyphens, and be 32 characters or less.",
    )


def validate_suffix(suffix: str) -> str:
    if not re.match(r"^[a-zA-Z0-9_-]+$", suffix):
        raise ValueError("suffix must contain only alphanumeric characters, underscores, and hyphens")
    if len(suffix) > 32:
        raise ValueError("suffix must be 32 characters or less")
    return suffix


def pytest_configure(config: Config):
    suffix: str = config.getini("suffix")
    validated_suffix: str = validate_suffix(suffix)
    config.pytest_http_suffix = validated_suffix


def get_test_name_pattern(config: Config) -> re.Pattern[str]:
    suffix: str = getattr(config, "pytest_http_suffix", "http")
    return re.compile(rf"^test_(?P<name>.+)\.{re.escape(suffix)}\.json$")


class VariableSubstitutionError(Exception):
    pass


def substitute_variables(json_text: str, fixtures: dict[str, Any]) -> str:
    try:
        for name, value in fixtures.items():
            placeholder: str = f'"${name}"'
            json_value: str = json.dumps(value)
            json_text = json_text.replace(placeholder, json_value)

            unquoted_placeholder: str = f"${name}"
            string_value: str = str(value)
            json_text = json_text.replace(unquoted_placeholder, string_value)

        return json_text
    except Exception as e:
        raise VariableSubstitutionError(f"Failed to substitute variables: {e}") from e


def substitute_stage_variables(stage_data: dict[str, Any], variables: dict[str, Any]) -> dict[str, Any]:
    try:
        json_text: str = json.dumps(stage_data, default=str)

        for name, value in variables.items():
            quoted_placeholder: str = f'"${name}"'
            json_value: str = json.dumps(value)
            json_text = json_text.replace(quoted_placeholder, json_value)

            unquoted_placeholder: str = f"${name}"
            string_value: str = str(value)
            json_text = json_text.replace(unquoted_placeholder, string_value)

        return json.loads(json_text)
    except Exception as e:
        raise VariableSubstitutionError(f"Failed to substitute variables in stage: {e}") from e


def substitute_kwargs_variables(kwargs: dict[str, Any] | None, variables: dict[str, Any]) -> dict[str, Any] | None:
    """Apply variable substitution to kwargs values."""
    if kwargs is None:
        return None
    
    try:
        # Convert kwargs to JSON string for substitution
        kwargs_json = json.dumps(kwargs, default=str)
        
        for name, value in variables.items():
            quoted_placeholder: str = f'"${name}"'
            json_value: str = json.dumps(value)
            kwargs_json = kwargs_json.replace(quoted_placeholder, json_value)

            unquoted_placeholder: str = f"${name}"
            string_value: str = str(value)
            kwargs_json = kwargs_json.replace(unquoted_placeholder, string_value)
        
        return json.loads(kwargs_json)
    except Exception as e:
        raise VariableSubstitutionError(f"Failed to substitute variables in kwargs: {e}") from e


def call_function_with_kwargs(func_name: str, response: requests.Response, kwargs: dict[str, Any] | None = None) -> Any:
    """Call a function with the response and optional kwargs."""
    try:
        # Get function (validation already confirmed it exists and is callable)
        module_path, function_name = func_name.rsplit(":", 1)
        import importlib
        module = importlib.import_module(module_path)
        func = getattr(module, function_name)

        # Call the function with the response and kwargs
        if kwargs:
            return func(response, **kwargs)
        else:
            return func(response)
    except Exception as e:
        raise Exception(f"Error executing function '{func_name}': {e}")


def json_test_function(original_data: dict[str, Any], **fixtures: Any) -> None:
    variable_context: dict[str, Any] = dict(fixtures)

    try:
        test_model: Scenario = Scenario.model_validate(original_data)
        logging.info(f"Test model: {test_model}")
        logging.info(f"Available fixtures: {fixtures}")

        for stage_index, original_stage in enumerate(test_model.stages):
            logging.info(f"Executing stage {stage_index}: {original_stage.name}")
            logging.info(f"Current variable context: {variable_context}")

            stage_dict = original_stage.model_dump(by_alias=True)
            substituted_stage_dict = substitute_stage_variables(stage_dict, variable_context)

            try:
                stage = Stage.model_validate(substituted_stage_dict)
            except ValidationError as e:
                pytest.fail(f"Stage '{original_stage.name}' validation failed after variable substitution: {e}")

            if stage.url:
                logging.info(f"Making HTTP request to: {stage.url}")

                request_params = {}
                if stage.params:
                    request_params["params"] = stage.params
                if stage.headers:
                    request_params["headers"] = stage.headers

                try:
                    response = requests.get(stage.url, **request_params)
                except requests.Timeout:
                    pytest.fail(f"HTTP request timed out for stage '{stage.name}' to URL: {stage.url}")
                except requests.ConnectionError as e:
                    pytest.fail(f"HTTP connection error for stage '{stage.name}' to URL: {stage.url} - {e}")
                except requests.RequestException as e:
                    pytest.fail(f"HTTP request failed for stage '{stage.name}' to URL: {stage.url} - {e}")

                logging.info(f"Response status: {response.status_code}")
                logging.info(f"Response headers: {dict(response.headers)}")

                if stage.verify and stage.verify.status is not None:
                    expected_status = stage.verify.status.value
                    actual_status = response.status_code
                    if actual_status != expected_status:
                        pytest.fail(f"Status code verification failed for stage '{stage.name}': expected {expected_status}, got {actual_status}")
                    logging.info(f"Status code verification passed: {actual_status}")

                response_data = {
                    "status_code": response.status_code,
                    "headers": dict(response.headers),
                    "text": response.text,
                    "json": response.json() if response.headers.get("content-type", "").startswith("application/json") else None,
                }

                if stage.verify and stage.verify.json_data is not None:
                    for jmespath_expr, expected_value in stage.verify.json_data.items():
                        try:
                            actual_value = jmespath.search(jmespath_expr, response_data)
                            if actual_value != expected_value:
                                pytest.fail(f"JSON verification failed for stage '{stage.name}' with JMESPath '{jmespath_expr}': expected {expected_value}, got {actual_value}")
                            logging.info(f"JSON verification passed for JMESPath '{jmespath_expr}': {actual_value}")
                        except Exception as e:
                            pytest.fail(f"Error during JSON verification for stage '{stage.name}' with JMESPath '{jmespath_expr}': {e}")

                # Handle verify functions
                if stage.verify and stage.verify.functions:
                    for func_item in stage.verify.functions:
                        try:
                            if isinstance(func_item, str):
                                # Backward compatibility: string function name
                                func_name = func_item
                                kwargs = None
                            else:
                                # New format: FunctionCall object
                                func_name = func_item.function
                                # Apply variable substitution to kwargs
                                kwargs = substitute_kwargs_variables(func_item.kwargs, variable_context)

                            # Call the function with the response and get the verification result
                            verification_result = call_function_with_kwargs(func_name, response, kwargs)

                            # Validate that the function returns a boolean
                            if not isinstance(verification_result, bool):
                                pytest.fail(f"Verify function '{func_name}' must return a boolean, got {type(verification_result)} for stage '{stage.name}'")

                            # If verification fails, fail the test
                            if not verification_result:
                                pytest.fail(f"Verify function '{func_name}' failed for stage '{stage.name}'")

                            logging.info(f"Verify function '{func_name}' passed for stage '{stage.name}'")

                        except Exception as e:
                            pytest.fail(f"Error executing verify function '{func_name}' for stage '{stage.name}': {e}")

                if stage.save:
                    # Handle vars
                    if stage.save.vars:
                        for var_name, jmespath_expr in stage.save.vars.items():
                            try:
                                saved_value = jmespath.search(jmespath_expr, response_data)
                                variable_context[var_name] = saved_value
                                logging.info(f"Saved variable '{var_name}' = {saved_value}")
                            except Exception as e:
                                pytest.fail(f"Error saving variable '{var_name}': {e}")

                    # Handle functions
                    if stage.save.functions:
                        for func_item in stage.save.functions:
                            try:
                                if isinstance(func_item, str):
                                    # Backward compatibility: string function name
                                    func_name = func_item
                                    kwargs = None
                                else:
                                    # New format: FunctionCall object
                                    func_name = func_item.function
                                    # Apply variable substitution to kwargs
                                    kwargs = substitute_kwargs_variables(func_item.kwargs, variable_context)

                                # Call the function with the response and get the returned variables
                                returned_vars = call_function_with_kwargs(func_name, response, kwargs)

                                # Validate that the function returns a dictionary
                                if not isinstance(returned_vars, dict):
                                    pytest.fail(f"Function '{func_name}' must return a dictionary of variables, got {type(returned_vars)} for stage '{stage.name}'")

                                # Add the returned variables to the context
                                for var_name, var_value in returned_vars.items():
                                    # Validate that variable names are valid Python identifiers
                                    if not var_name.isidentifier():
                                        pytest.fail(f"Function '{func_name}' returned invalid variable name '{var_name}' for stage '{stage.name}'")

                                    variable_context[var_name] = var_value
                                    logging.info(f"Function '{func_name}' saved variable '{var_name}' = {var_value}")

                            except Exception as e:
                                pytest.fail(f"Error executing function '{func_name}' for stage '{stage.name}': {e}")
            else:
                logging.info(f"No URL provided for stage '{stage.name}', skipping HTTP request")

    except VariableSubstitutionError as e:
        pytest.fail(f"Variable substitution error: {e}")
    except json.JSONDecodeError as e:
        pytest.fail(f"JSON decode error after substitution: {e}")
    except ValidationError as e:
        pytest.fail(f"Validation error: {e}")
    except Exception as e:
        pytest.fail(f"Unexpected error: {e}")


class JSONFile(pytest.File):
    def collect(self) -> Iterable[Item | Collector]:
        try:
            test_text: str = self.path.read_text()
            test_data: dict[str, Any] = json.loads(test_text)
        except json.JSONDecodeError as e:
            yield FailedValidationItem.from_parent(self, name=self.name, error=f"Invalid JSON: {e}")
            return
        except Exception as e:
            yield FailedValidationItem.from_parent(self, name=self.name, error=f"Error reading file: {e}")
            return

        try:
            processed_data: dict[str, Any] = jsonref.replace_refs(test_data, base_uri=self.path.as_uri())
        except Exception as e:
            yield FailedValidationItem.from_parent(self, name=self.name, error=f"JSONRef error: {e}")
            return

        try:
            test_spec: Scenario = Scenario.model_validate(processed_data)
        except ValidationError as e:
            yield FailedValidationItem.from_parent(self, name=self.name, error=f"Validation error: {e}")
            return
        except Exception as e:
            yield FailedValidationItem.from_parent(self, name=self.name, error=f"Unexpected validation error: {e}")
            return

        try:
            fixtures: list[str] = list(test_spec.fixtures)
            marks: list[str] = list(test_spec.marks)
        except Exception as e:
            yield FailedValidationItem.from_parent(self, name=self.name, error=f"Error extracting fixtures/marks: {e}")
            return

        try:

            def test_func(**kwargs: Any) -> None:
                return json_test_function(processed_data, **{name: kwargs[name] for name in fixtures if name in kwargs})

            test_func.__signature__ = inspect.Signature([inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD) for name in fixtures])
            test_func.__name__ = f"test_{self.name}"

            test_func = reduce(lambda func, mark: eval(f"pytest.mark.{mark}", {"pytest": pytest, "sys": sys})(func), marks or [], test_func)

            yield Function.from_parent(self, name=self.name, callobj=test_func)
        except (SyntaxError, NameError, AttributeError) as e:
            yield FailedValidationItem.from_parent(self, name=self.name, error=f"Failed to apply marker: {e}")
        except Exception as e:
            yield FailedValidationItem.from_parent(self, name=self.name, error=f"Error creating test function: {e}")


class FailedValidationItem(pytest.Item):
    def __init__(self, error: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.error: str = error

    def runtest(self) -> None:
        raise AssertionError(self.error)

    def reportinfo(self) -> tuple[Path, int, str]:
        return self.path, 0, f"sandbox json test: {self.name}"


def pytest_collect_file(file_path: Path, parent: Collector) -> JSONFile | None:
    match: re.Match[str] | None = get_test_name_pattern(parent.config).match(file_path.name)
    if match:
        return JSONFile.from_parent(parent, path=file_path, name=match.group("name"))
    return None
