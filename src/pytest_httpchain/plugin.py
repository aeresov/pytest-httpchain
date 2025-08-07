import logging
import re
import types
from collections.abc import Iterable
from pathlib import Path

import pytest
import pytest_httpchain_jsonref.loader
from _pytest import config, nodes, python, reports, runner
from _pytest.config import argparsing
from pydantic import ValidationError
from pytest_httpchain_jsonref.exceptions import ReferenceResolverError
from pytest_httpchain_models.entities import Scenario
from simpleeval import EvalWithCompoundTypes

from pytest_httpchain.constants import ConfigOptions

from .carrier_factory import create_test_class

logger = logging.getLogger(__name__)


class JsonModule(python.Module):
    """JSON test module that collects and executes HTTP chain tests."""

    def collect(self) -> Iterable[nodes.Item | nodes.Collector]:
        """Collect test items from a JSON module."""
        # Load and validate the test scenario from JSON
        ref_parent_traversal_depth = int(self.config.getini(ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH))

        try:
            test_data = pytest_httpchain_jsonref.loader.load_json(
                self.path,
                max_parent_traversal_depth=ref_parent_traversal_depth,
            )
        except ReferenceResolverError as e:
            raise nodes.Collector.CollectError("Cannot load JSON file") from e

        try:
            scenario = Scenario.model_validate(test_data)
        except ValidationError as e:
            raise nodes.Collector.CollectError("Cannot parse test scenario") from e

        # Create test class using factory
        CarrierClass = create_test_class(scenario, self.path, self.name)

        # Create pytest Class node
        dummy_module = types.ModuleType("generated")
        setattr(dummy_module, self.name, CarrierClass)
        self._getobj = lambda: dummy_module

        json_class = python.Class.from_parent(
            self,
            path=self.path,
            name=self.name,
            obj=CarrierClass,
        )

        # Apply scenario-level markers
        evaluator = EvalWithCompoundTypes(names={"pytest": pytest})
        for mark_str in scenario.marks:
            try:
                marker = evaluator.eval(f"pytest.mark.{mark_str}")
                if marker:
                    json_class.add_marker(marker)
            except Exception as e:
                logger.warning(f"Failed to create marker '{mark_str}': {e}")

        yield json_class


def pytest_addoption(parser: argparsing.Parser) -> None:
    """Add command-line options for the plugin."""
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
    """Validate configuration settings."""
    suffix = str(config.getini(ConfigOptions.SUFFIX))
    if not re.match(r"^[a-zA-Z0-9_-]{1,32}$", suffix):
        raise ValueError("suffix must contain only alphanumeric characters, underscores, hyphens, and be ≤32 chars")

    ref_parent_traversal_depth = int(config.getini(ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH))
    if ref_parent_traversal_depth < 0:
        raise ValueError("Maximum number of parent directory traversals must be non-negative")


def pytest_collect_file(file_path: Path, parent: nodes.Collector) -> nodes.Collector | None:
    """Collect JSON test files matching the configured pattern."""
    suffix: str = parent.config.getini(ConfigOptions.SUFFIX)
    pattern = re.compile(rf"^test_(?P<name>.+)\.{re.escape(suffix)}\.json$")
    file_match = pattern.match(file_path.name)
    if file_match:
        return JsonModule.from_parent(parent, path=file_path, name=file_match.group("name"))
    return None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: nodes.Item, call: runner.CallInfo):
    """Add custom sections to test reports."""
    outcome = yield
    report: reports.TestReport = outcome.get_result()
    if call.when == "call":
        report.sections.append(("call_title", "call_value"))
