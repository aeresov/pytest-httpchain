import logging
import re
import types
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
import pytest_httpchain_jsonref.loader
from _pytest import config, nodes, python, reports, runner
from _pytest.config import argparsing
from pydantic import ValidationError
from pytest_httpchain_jsonref.exceptions import ReferenceResolverError
from pytest_httpchain_models.entities import Scenario
from simpleeval import EvalWithCompoundTypes

from pytest_httpchain.constants import ConfigOptions

from .carrier import Carrier
from .report_formatter import format_request, format_response

logger = logging.getLogger(__name__)


class JsonModule(python.Module):
    """JSON test module that collects and executes HTTP chain tests.

    This class extends pytest's Module to handle JSON test files containing
    HTTP chain test scenarios. It loads, validates, and converts JSON test
    definitions into executable pytest test classes.
    """

    def collect(self) -> Iterable[nodes.Item | nodes.Collector]:
        ref_parent_traversal_depth = int(self.config.getini(ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH))
        root_path = Path(self.config.rootpath)

        try:
            test_data = pytest_httpchain_jsonref.loader.load_json(
                self.path,
                max_parent_traversal_depth=ref_parent_traversal_depth,
                root_path=root_path,
            )
        except ReferenceResolverError as e:
            raise nodes.Collector.CollectError(f"Cannot load JSON file {self.path}: {str(e)}") from None
        except Exception as e:
            raise nodes.Collector.CollectError(f"Failed to parse JSON file {self.path}: {str(e)}") from None

        try:
            scenario = Scenario.model_validate(test_data)
        except ValidationError as e:
            error_details = []
            for error in e.errors():
                loc = " -> ".join(str(x) for x in error["loc"])
                msg = error["msg"]
                error_details.append(f"  - {loc}: {msg}")

            full_error_msg = f"Cannot parse test scenario in {self.path}:\n" + "\n".join(error_details)
            raise nodes.Collector.CollectError(full_error_msg) from None

        CarrierClass = Carrier.create_test_class(scenario, self.name)
        dummy_module = types.ModuleType("generated")
        setattr(dummy_module, self.name, CarrierClass)
        self._getobj = lambda: dummy_module

        json_class = python.Class.from_parent(
            self,
            path=self.path,
            name=self.name,
            obj=CarrierClass,
        )

        evaluator = EvalWithCompoundTypes(names={"pytest": pytest})

        # Apply class-level marks from scenario
        for mark_str in scenario.marks:
            try:
                marker = evaluator.eval(f"pytest.mark.{mark_str}")
                if marker:
                    json_class.add_marker(marker)
            except Exception as e:
                logger.warning(f"Failed to create marker '{mark_str}': {e}")

        yield json_class


def pytest_addoption(parser: argparsing.Parser) -> None:
    parser.addini(
        name=ConfigOptions.SUFFIX,
        help="File suffix for HTTP test files.",
        type="string",
        default="http",
    )
    parser.addini(
        name=ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH,
        help="Maximum number of parent directory traversals allowed in $ref paths.",
        type="string",
        default="3",
    )


def pytest_configure(config: config.Config) -> None:
    suffix = str(config.getini(ConfigOptions.SUFFIX))
    if not re.match(r"^[a-zA-Z0-9_-]{1,32}$", suffix):
        raise ValueError("suffix must contain only alphanumeric characters, underscores, hyphens, and be ≤32 chars")

    ref_parent_traversal_depth = int(config.getini(ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH))
    if ref_parent_traversal_depth < 0:
        raise ValueError("Maximum number of parent directory traversals must be non-negative")


def pytest_collect_file(file_path: Path, parent: nodes.Collector) -> nodes.Collector | None:
    suffix: str = parent.config.getini(ConfigOptions.SUFFIX)
    pattern = re.compile(rf"^test_(?P<name>.+)\.{re.escape(suffix)}\.json$")
    file_match = pattern.match(file_path.name)
    if file_match:
        return JsonModule.from_parent(parent, path=file_path, name=file_match.group("name"))
    return None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: nodes.Item, call: runner.CallInfo[Any]) -> Any:
    outcome = yield
    report: reports.TestReport = outcome.get_result()

    if call.when == "call":
        if hasattr(item, "instance") and isinstance(item.instance, Carrier):
            carrier = item.instance

            if carrier._last_request:
                report.sections.append(("HTTP Request", format_request(carrier._last_request)))
            if carrier._last_response:
                report.sections.append(("HTTP Response", format_response(carrier._last_response)))
