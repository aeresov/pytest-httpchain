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
from _pytest.config import Config
from _pytest.config.argparsing import Parser
from _pytest.nodes import Collector, Item
from _pytest.python import Function
from pydantic import ValidationError

from pytest_http.models import Scenario, Structure


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
    return re.compile(rf"^test_(?P<name>.*)\.{re.escape(suffix)}\.json$")


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


def json_test_function(test_text: str, path: Path, **fixtures: Any) -> None:
    try:
        substituted_json: str = substitute_variables(test_text, fixtures)
        test_data: dict[str, Any] = jsonref.loads(substituted_json, base_uri=path.as_uri())
        test_model: Scenario = Scenario.model_validate(test_data)
        logging.info(f"Test model: {test_model}")
        logging.info(f"Available fixtures: {fixtures}")
    except VariableSubstitutionError as e:
        pytest.fail(f"Variable substitution error: {e}")
    except json.JSONDecodeError as e:
        pytest.fail(f"JSON decode error: {e}")
    except ValidationError as e:
        pytest.fail(f"Validation error: {e}")
    except Exception as e:
        pytest.fail(f"Unexpected error: {e}")


class JSONFile(pytest.File):
    def collect(self) -> Iterable[Item | Collector]:
        try:
            test_text: str = self.path.read_text()
            structure: Structure = Structure.model_validate(json.loads(test_text))
            fixtures: list[str] = list(structure.fixtures or [])

            def test_func(**kwargs: Any) -> None:
                return json_test_function(test_text, self.path, **{name: kwargs[name] for name in fixtures if name in kwargs})

            test_func.__signature__ = inspect.Signature([inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD) for name in fixtures])
            test_func.__name__ = f"test_{self.name}"

            test_func = reduce(lambda func, mark: eval(f"pytest.mark.{mark}", {"pytest": pytest, "sys": sys})(func), structure.marks or [], test_func)

            yield Function.from_parent(self, name=self.name, callobj=test_func)

        except ValidationError as e:
            yield FailedValidationItem.from_parent(self, name=self.name, error=f"Invalid JSON: {e}")
        except (SyntaxError, NameError, AttributeError) as e:
            yield FailedValidationItem.from_parent(self, name=self.name, error=f"Failed to apply marker: {e}")
        except Exception as e:
            yield FailedValidationItem.from_parent(self, name=self.name, error=f"Error reading file: {e}")


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
