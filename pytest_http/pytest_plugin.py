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
from _pytest.nodes import Collector, Item
from _pytest.python import Function
from pydantic import ValidationError

from pytest_http.models import Structure, Test

HTTP_TEST_PATTERN = re.compile(r"^test_(?P<name>.*)\.http\.json$")


def json_test_function(test_text: str, path: Path, **fixtures):
    test_data = jsonref.loads(test_text, base_uri=path.as_uri())
    try:
        test_model = Test.model_validate(test_data)
        logging.info(f"Test model: {test_model}")
        logging.info(f"Available fixtures: {fixtures}")
    except ValidationError as e:
        pytest.fail(f"Validation error: {e}")


class JSONFile(pytest.File):
    def collect(self) -> Iterable[Item | Collector]:
        try:
            test_text = self.path.read_text()
            structure = Structure.model_validate(json.loads(test_text))
            fixtures = list(structure.fixtures or [])

            def test_func(**kwargs):
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
        self.error = error

    def runtest(self) -> None:
        raise AssertionError(self.error)

    def reportinfo(self) -> tuple[Path, int, str]:
        return self.path, 0, f"sandbox json test: {self.name}"


def pytest_collect_file(file_path: Path, parent: Collector) -> JSONFile | None:
    if match := HTTP_TEST_PATTERN.match(file_path.name):
        return JSONFile.from_parent(parent, path=file_path, name=match.group("name"))
    return None
