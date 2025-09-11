"""Pytest plugin for HTTP chain testing.

This module provides the pytest plugin hooks and collection logic for
discovering and executing HTTP chain tests from JSON files.
"""

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
        """Collect test items from a JSON module.

        This method:
        1. Loads the JSON file with reference resolution
        2. Validates the test scenario against the schema
        3. Creates a dynamic test class using the factory
        4. Yields the test class for pytest to execute

        Yields:
            python.Class: A pytest Class node containing test methods

        Raises:
            Collector.CollectError: If JSON loading or validation fails
        """
        # Load and validate the test scenario from JSON
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
            # Create a detailed error message with validation errors
            error_details = []
            for error in e.errors():
                loc = " -> ".join(str(x) for x in error["loc"])
                msg = error["msg"]
                error_details.append(f"  - {loc}: {msg}")

            full_error_msg = f"Cannot parse test scenario in {self.path}:\n" + "\n".join(error_details)
            raise nodes.Collector.CollectError(full_error_msg) from None

        # Create test class using factory
        CarrierClass = Carrier.create_test_class(scenario, self.name)

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
    """Add command-line options for the plugin.

    Registers configuration options that can be set in pytest.ini:
    - httpchain_suffix: File suffix for test files (default: "http")
    - httpchain_ref_parent_traversal_depth: Max parent directory traversals in $ref paths

    Args:
        parser: Pytest's argument parser to add options to
    """
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
    """Validate configuration settings.

    Ensures that configuration values are valid:
    - Suffix must be alphanumeric with underscores/hyphens, max 32 chars
    - Reference traversal depth must be non-negative

    Args:
        config: Pytest configuration object

    Raises:
        ValueError: If configuration values are invalid
    """
    suffix = str(config.getini(ConfigOptions.SUFFIX))
    if not re.match(r"^[a-zA-Z0-9_-]{1,32}$", suffix):
        raise ValueError("suffix must contain only alphanumeric characters, underscores, hyphens, and be ≤32 chars")

    ref_parent_traversal_depth = int(config.getini(ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH))
    if ref_parent_traversal_depth < 0:
        raise ValueError("Maximum number of parent directory traversals must be non-negative")


def pytest_collect_file(file_path: Path, parent: nodes.Collector) -> nodes.Collector | None:
    """Collect JSON test files matching the configured pattern.

    This hook is called by pytest for each file in the test directory.
    It checks if the file matches the pattern: test_<name>.<suffix>.json
    where suffix is configurable (default: "http").

    Args:
        file_path: Path to the file being considered for collection
        parent: The parent collector node

    Returns:
        JsonModule collector if file matches pattern, None otherwise

    Example:
        For suffix="http", these files would be collected:
        - test_api.http.json
        - test_user_flow.http.json

    Configuration:
        The suffix can be configured in pytest.ini:
        [tool.pytest.ini_options]
        httpchain_suffix = "api"  # Changes pattern to test_*.api.json
    """
    suffix: str = parent.config.getini(ConfigOptions.SUFFIX)
    pattern = re.compile(rf"^test_(?P<name>.+)\.{re.escape(suffix)}\.json$")
    file_match = pattern.match(file_path.name)
    if file_match:
        return JsonModule.from_parent(parent, path=file_path, name=file_match.group("name"))
    return None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: nodes.Item, call: runner.CallInfo[Any]) -> Any:
    """Add custom sections to test reports.

    This hook adds additional information to test reports that can be
    displayed in pytest output or used by other plugins.

    Args:
        item: The test item being reported on
        call: Information about the test call

    Yields:
        The report with additional sections added
    """
    outcome = yield
    report: reports.TestReport = outcome.get_result()

    if call.when == "call":
        if hasattr(item, "instance") and isinstance(item.instance, Carrier):
            carrier = item.instance

            if carrier._last_request:
                report.sections.append(("HTTP Request", format_request(carrier._last_request)))
            if carrier._last_response:
                report.sections.append(("HTTP Response", format_response(carrier._last_response)))
