import inspect
import json
import logging
import re
import sys
from collections.abc import Iterable
from functools import reduce
from pathlib import Path
from typing import Any

import jsonref
import pytest
import requests
from _pytest.config import Config
from _pytest.config.argparsing import Parser
from _pytest.nodes import Collector, Item
from _pytest.python import Function
from pydantic import ValidationError

from pytest_http.models import Scenario


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
        return json_text
    except Exception as e:
        raise VariableSubstitutionError(f"Failed to substitute variables: {e}") from e


def json_test_function(original_data: dict[str, Any], **fixtures: Any) -> None:
    saved_variables: dict[str, Any] = {}

    try:
        # Dump original data to JSON string
        json_text: str = json.dumps(original_data, default=str)

        # Substitute variables
        if fixtures:
            substituted_json: str = substitute_variables(json_text, fixtures)
            processed_data: dict[str, Any] = json.loads(substituted_json)
        else:
            processed_data = original_data

        # Pydantic validation with substituted variables
        test_model: Scenario = Scenario.model_validate(processed_data)
        logging.info(f"Test model: {test_model}")
        logging.info(f"Available fixtures: {fixtures}")

        # Execute each stage
        for stage in test_model.stages:
            logging.info(f"Executing stage: {stage.name}")

            # Make HTTP request if URL is provided
            if stage.url:
                logging.info(f"Making HTTP request to: {stage.url}")

                # Prepare request parameters
                request_params = {}
                if stage.params:
                    request_params["params"] = stage.params
                if stage.headers:
                    request_params["headers"] = stage.headers

                # Make GET request (for now, could be extended to support other methods)
                response = requests.get(stage.url, **request_params)

                # Log response details
                logging.info(f"Response status: {response.status_code}")
                logging.info(f"Response headers: {dict(response.headers)}")

                # Store response data for potential saving
                response_data = {
                    "status_code": response.status_code,
                    "headers": dict(response.headers),
                    "text": response.text,
                    "json": response.json() if response.headers.get("content-type", "").startswith("application/json") else None,
                }

                # Save variables if specified
                if stage.save:
                    import jmespath

                    for var_name, jmespath_expr in stage.save.items():
                        try:
                            saved_value = jmespath.search(jmespath_expr, response_data)
                            saved_variables[var_name] = saved_value
                            logging.info(f"Saved variable '{var_name}' = {saved_value}")
                        except Exception as e:
                            pytest.fail(f"Error saving variable '{var_name}': {e}")
            else:
                logging.info(f"No URL provided for stage '{stage.name}', skipping HTTP request")

    except VariableSubstitutionError as e:
        pytest.fail(f"Variable substitution error: {e}")
    except json.JSONDecodeError as e:
        pytest.fail(f"JSON decode error after substitution: {e}")
    except ValidationError as e:
        pytest.fail(f"Validation error: {e}")
    except requests.RequestException as e:
        pytest.fail(f"HTTP request error: {e}")
    except Exception as e:
        pytest.fail(f"Unexpected error: {e}")


class JSONFile(pytest.File):
    def collect(self) -> Iterable[Item | Collector]:
        try:
            # Load JSON (catch general JSON formatting errors)
            test_text: str = self.path.read_text()
            test_data: dict[str, Any] = json.loads(test_text)
        except json.JSONDecodeError as e:
            yield FailedValidationItem.from_parent(self, name=self.name, error=f"Invalid JSON: {e}")
            return
        except Exception as e:
            yield FailedValidationItem.from_parent(self, name=self.name, error=f"Error reading file: {e}")
            return

        try:
            # JSONRef (catch more JSON errors)
            processed_data: dict[str, Any] = jsonref.replace_refs(test_data, base_uri=self.path.as_uri())
        except Exception as e:
            yield FailedValidationItem.from_parent(self, name=self.name, error=f"JSONRef error: {e}")
            return

        try:
            # Pydantic validation without variable substitution (catch validation errors)
            test_spec: Scenario = Scenario.model_validate(processed_data)
        except ValidationError as e:
            yield FailedValidationItem.from_parent(self, name=self.name, error=f"Validation error: {e}")
            return
        except Exception as e:
            yield FailedValidationItem.from_parent(self, name=self.name, error=f"Unexpected validation error: {e}")
            return

        try:
            # Extract fixtures and marks (catch specific errors)
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
