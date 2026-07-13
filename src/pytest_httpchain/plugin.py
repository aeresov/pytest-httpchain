"""pytest plugin entry point: discovery, collection, and reporting hooks.

Registered as the ``pytest11`` entry point, this module wires HTTP-chain JSON
scenarios into pytest:

- ``pytest_addoption`` / ``pytest_configure`` register and validate the ini
  options (``suffix``, ``ref_parent_traversal_depth``, ``max_comprehension_length``,
  ``max_parallel_iterations``) and the ``--output-dir`` flag.
- ``pytest_collect_file`` matches ``test_<name>.<suffix>.json`` files and hands
  them to `JsonModule`.
- `JsonModule.collect` loads the JSON (resolving ``$ref``), validates it
  against the `Scenario` model, runs the semantic validator
  (warnings become `ScenarioValidationWarning`, errors become
  ``CollectError``), and builds the dynamic test class via
  ``carrier.create_test_class``.
- ``pytest_runtest_makereport`` attaches the last HTTP request/response to the
  test report and optionally writes a HAR file.

`ScenarioValidationWarning` is defined here because the collection hook
and the ``pytest11`` entry point reference it directly; it is re-exported from
the package ``__init__`` as the user-facing name.
"""

import logging
import re
import types
import warnings
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
import simpleeval
from pydantic import ValidationError

import pytest_httpchain.jsonref
from pytest_httpchain.constants import ConfigOptions
from pytest_httpchain.models import Scenario

from .carrier import Carrier, create_test_class
from .har_writer import write_har_file
from .report_formatter import format_request, format_response
from .utils import make_marker
from .validation import check_scenario

logger = logging.getLogger(__name__)


class ScenarioValidationWarning(pytest.PytestWarning):
    """A collected scenario has a non-fatal validation issue (e.g. an undefined variable)."""


class JsonModule(pytest.Module):
    """JSON test module that collects and executes HTTP chain tests.

    This class extends pytest's Module to handle JSON test files containing
    HTTP chain test scenarios. It loads, validates, and converts JSON test
    definitions into executable pytest test classes.
    """

    def collect(self) -> Iterable[pytest.Item | pytest.Collector]:
        # read JSON and apply references
        ref_parent_traversal_depth = self.config.getini(ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH)
        root_path = Path(self.config.rootpath)
        try:
            test_data = pytest_httpchain.jsonref.load_json(
                self.path,
                max_parent_traversal_depth=ref_parent_traversal_depth,
                root_path=root_path,
            )
        except pytest_httpchain.jsonref.ReferenceResolverError as e:
            raise pytest.Collector.CollectError(f"Cannot load JSON file {self.path}: {e}") from None
        except Exception as e:
            raise pytest.Collector.CollectError(f"Failed to parse JSON file {self.path}: {e}") from None

        # validate general scenario structure
        try:
            scenario = Scenario.model_validate(test_data)
        except ValidationError as e:
            error_details = []
            for error in e.errors():
                loc = " -> ".join(str(x) for x in error["loc"])
                msg = error["msg"]
                error_details.append(f"  - {loc}: {msg}")

            full_error_msg = f"Cannot parse test scenario in {self.path}:\n" + "\n".join(error_details)
            raise pytest.Collector.CollectError(full_error_msg) from None

        # semantic validation: cross-cutting checks the schema cannot express
        # (duplicate stage names, fixture/variable conflicts, undefined/forward-referenced
        # variables, no-op verify, contradictory body checks, ...)
        diagnostics, _ = check_scenario(scenario, test_data)
        for diagnostic in diagnostics:
            if diagnostic.severity == "warning":
                warnings.warn(ScenarioValidationWarning(f"{self.path}: [{diagnostic.code}] {diagnostic.message}"), stacklevel=2)
        error_diagnostics = [d for d in diagnostics if d.severity == "error"]
        if error_diagnostics:
            detail = "\n".join(f"  - [{d.code}] {d.message}" for d in error_diagnostics)
            raise pytest.Collector.CollectError(f"Invalid test scenario in {self.path}:\n{detail}")

        # generate python test class
        max_parallel_iterations = self.config.getini(ConfigOptions.MAX_PARALLEL_ITERATIONS)
        try:
            CarrierClass = create_test_class(scenario, self.name, max_parallel_iterations=max_parallel_iterations)
        except Exception as e:
            # create_test_class resolves scenario-level substitutions/ssl and may call
            # the scenario auth user function and build the httpx client at collection
            # time; surface any failure as a clean collection error, like the sibling
            # load/validate paths above, instead of a raw internal traceback.
            raise pytest.Collector.CollectError(f"Cannot build test class for {self.path}: {e}") from None
        # Module._getobj() defaults to importtestmodule(self.path), which would try
        # to import this .json file as a Python module and fail. Bypass it by handing
        # pytest an in-memory module that already carries the generated test class.
        dummy_module = types.ModuleType("generated")
        setattr(dummy_module, self.name, CarrierClass)
        self._getobj = lambda: dummy_module  # ty: ignore[invalid-assignment]
        json_class = pytest.Class.from_parent(
            self,
            path=self.path,
            name=self.name,
            obj=CarrierClass,
        )

        # apply class-level markers
        for mark_str in scenario.marks:
            try:
                json_class.add_marker(make_marker(mark_str))
            except Exception as e:
                raise pytest.Collector.CollectError(f"Invalid marker '{mark_str}' in {self.path}: {e}") from None

        yield json_class


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini(
        name=ConfigOptions.SUFFIX,
        help="File suffix for HTTP test files.",
        type="string",
        default="http",
    )
    parser.addini(
        name=ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH,
        help="Maximum number of parent directory traversals allowed in $ref paths.",
        type="int",
        default=3,
    )
    parser.addini(
        name=ConfigOptions.MAX_COMPREHENSION_LENGTH,
        help="Maximum length for list/dict comprehensions in template expressions.",
        type="int",
        default=50000,
    )
    parser.addini(
        name=ConfigOptions.MAX_PARALLEL_ITERATIONS,
        help="Maximum number of parallel iterations allowed per stage.",
        type="int",
        default=10000,
    )
    parser.addoption(
        "--output-dir",
        dest="output_dir",
        default=None,
        help="Directory to write test output files (HAR format for HTTP communications).",
    )


def pytest_configure(config: pytest.Config) -> None:
    # Numeric options are registered with type="int", but pytest performs the
    # int() conversion with a bare int(value) that raises ValueError for a
    # non-integer ini value — which pytest renders as an INTERNALERROR traceback.
    # Wrap the read so a garbage value becomes a clean usage error; the range
    # checks below likewise raise pytest.UsageError.
    def _getint(name: str) -> int:
        try:
            return config.getini(name)
        except ValueError as e:
            raise pytest.UsageError(f"{name} must be an integer: {e}") from None

    suffix = str(config.getini(ConfigOptions.SUFFIX))
    if not re.match(r"^[a-zA-Z0-9_-]{1,32}$", suffix):
        raise pytest.UsageError("suffix must contain only alphanumeric characters, underscores, hyphens, and be ≤32 chars")

    ref_parent_traversal_depth = _getint(ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH)
    if ref_parent_traversal_depth < 0:
        raise pytest.UsageError("ref_parent_traversal_depth must be non-negative")

    max_comprehension_length = _getint(ConfigOptions.MAX_COMPREHENSION_LENGTH)
    if max_comprehension_length < 1:
        raise pytest.UsageError("max_comprehension_length must be a positive integer")
    if max_comprehension_length > 1_000_000:
        raise pytest.UsageError("max_comprehension_length must not exceed 1,000,000")
    simpleeval.MAX_COMPREHENSION_LENGTH = max_comprehension_length  # ty: ignore[invalid-assignment]

    max_parallel_iterations = _getint(ConfigOptions.MAX_PARALLEL_ITERATIONS)
    if max_parallel_iterations < 1:
        raise pytest.UsageError("max_parallel_iterations must be a positive integer")
    if max_parallel_iterations > 1_000_000:
        raise pytest.UsageError("max_parallel_iterations must not exceed 1,000,000")


def pytest_collect_file(file_path: Path, parent: pytest.Collector) -> pytest.Collector | None:
    suffix: str = parent.config.getini(ConfigOptions.SUFFIX)
    pattern = re.compile(rf"^test_(?P<name>.+)\.{re.escape(suffix)}\.json$")
    file_match = pattern.match(file_path.name)
    if file_match:
        return JsonModule.from_parent(parent, path=file_path, name=file_match.group("name"))
    return None


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Any:
    # pytest 8+ wrapper protocol: `yield` returns the inner hook's result directly
    # (no Outcome wrapper). We augment the report's sections in place and must
    # return the (same) result so it propagates to outer wrappers.
    report: pytest.TestReport = yield

    if call.when == "call":
        if hasattr(item, "instance") and isinstance(item.instance, Carrier):
            carrier = item.instance

            if carrier.last_request is not None:
                try:
                    report.sections.append(("HTTP Request", format_request(carrier.last_request)))
                except Exception as e:
                    report.sections.append(("HTTP Request", f"<Error formatting request: {e}>"))

            if carrier.last_response is not None:
                try:
                    report.sections.append(("HTTP Response", format_response(carrier.last_response)))
                except Exception as e:
                    report.sections.append(("HTTP Response", f"<Error formatting response: {e}>"))

            output_dir = item.config.getoption("output_dir")
            if output_dir and carrier.last_request is not None and carrier.last_response is not None:
                try:
                    har_path = write_har_file(
                        output_dir=Path(output_dir),
                        test_name=item.nodeid,
                        request=carrier.last_request,
                        response=carrier.last_response,
                    )
                    report.sections.append(("HAR File", str(har_path)))
                except Exception as e:
                    logger.warning(f"Failed to write HAR file for {item.nodeid}: {e}")

    return report
