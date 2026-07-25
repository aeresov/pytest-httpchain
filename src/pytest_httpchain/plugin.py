"""pytest plugin entry point (the ``pytest11`` hook module).

Registers the ini options and ``--httpchain-output-dir``, collects
``test_<name>.<suffix>.json`` files into `JsonModule`, keeps each scenario's
stages contiguous and ordered, and attaches the HTTP exchange (plus an optional
HAR file) to test reports.
"""

import logging
import re
import sys
import types
import warnings
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import pytest_httpchain.jsonref
from pytest_httpchain.carrier import Carrier
from pytest_httpchain.constants import ConfigOptions
from pytest_httpchain.factory import create_test_class
from pytest_httpchain.har_writer import write_har_file
from pytest_httpchain.models import Scenario
from pytest_httpchain.report_formatter import format_request, format_response
from pytest_httpchain.templates import set_max_comprehension_length
from pytest_httpchain.utils import make_marker
from pytest_httpchain.validation import check_scenario, load_scenario
from pytest_httpchain.warnings import AmbiguousReferenceWarning, ScenarioValidationWarning

logger = logging.getLogger(__name__)


class JsonModule(pytest.Module):
    """Collector for one scenario file: loads, validates, and turns it into a
    test class. Execution belongs to `Carrier`, under pytest's runner."""

    def _reject_chain_splitting_dist_mode(self, scenario: Scenario) -> None:
        """Fail collection when pytest-xdist would scatter a stage chain.

        Modes that distribute tests individually would break a multi-stage
        chain silently, since pytest-order is a no-op across workers.
        Class-preserving modes are fine, and single-stage scenarios have no
        chain to break. Inside a worker the real mode is only visible via
        workerinput, seeded by `pytest_configure_node`.
        """
        if len(scenario.stages) <= 1:
            return
        workerinput = getattr(self.config, "workerinput", None)
        if workerinput is not None:
            dist_mode = workerinput.get("httpchain_dist", "no")
        else:
            dist_mode = self.config.getoption("dist", default="no")
        if dist_mode in {"load", "each", "worksteal"}:
            raise pytest.Collector.CollectError(
                f"pytest-httpchain scenarios cannot run under pytest-xdist --dist={dist_mode}: "
                f"a multi-stage scenario's stages must run in order on a single worker. "
                f"Use --dist loadscope, loadfile, or loadgroup (scenario classes are grouped automatically), "
                f"or deselect scenario files when running with -n."
            )

    def collect(self) -> Iterable[pytest.Item | pytest.Collector]:
        ref_parent_traversal_depth = _get_ini(self.config, ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH)
        try:
            # Recorded, not raised: under `filterwarnings = error` an escaping
            # resolver warning would land in the generic handler below as a
            # misleading "Failed to parse JSON file". They are re-emitted after
            # the load in the same [HTTPCHAINxxx] form as the diagnostics.
            with warnings.catch_warnings(record=True) as caught_warnings:
                warnings.simplefilter("always")
                scenario, test_data = load_scenario(
                    self.path,
                    root_path=Path(self.config.rootpath),
                    ref_parent_traversal_depth=ref_parent_traversal_depth,
                )
        except pytest_httpchain.jsonref.ReferenceResolverError as e:
            raise pytest.Collector.CollectError(f"Cannot load JSON file {self.path}: {e}") from None
        except ValidationError as e:
            error_details = []
            for error in e.errors():
                loc = " -> ".join(str(x) for x in error["loc"])
                msg = error["msg"]
                error_details.append(f"  - {loc}: {msg}")

            full_error_msg = f"Cannot parse test scenario in {self.path}:\n" + "\n".join(error_details)
            raise pytest.Collector.CollectError(full_error_msg) from None
        except Exception as e:
            raise pytest.Collector.CollectError(f"Failed to parse JSON file {self.path}: {e}") from None

        for caught in caught_warnings:
            if isinstance(caught.message, AmbiguousReferenceWarning):
                warnings.warn(ScenarioValidationWarning(f"{self.path}: [HTTPCHAIN026] {caught.message}"), stacklevel=2)
            else:
                warnings.warn_explicit(caught.message, caught.category, caught.filename, caught.lineno)

        self._reject_chain_splitting_dist_mode(scenario)

        diagnostics, _ = check_scenario(scenario, test_data)
        for diagnostic in diagnostics:
            if diagnostic.severity == "warning":
                warnings.warn(ScenarioValidationWarning(f"{self.path}: [{diagnostic.code}] {diagnostic.message}"), stacklevel=2)
        error_diagnostics = [d for d in diagnostics if d.severity == "error"]
        if error_diagnostics:
            detail = "\n".join(f"  - [{d.code}] {d.message}" for d in error_diagnostics)
            raise pytest.Collector.CollectError(f"Invalid test scenario in {self.path}:\n{detail}")

        max_parallel_iterations = _get_ini(self.config, ConfigOptions.MAX_PARALLEL_ITERATIONS)
        try:
            CarrierClass = create_test_class(
                scenario,
                self.name,
                max_parallel_iterations=max_parallel_iterations,
                scenario_dir=self.path.parent,
                # Retaining every iteration's exchange costs memory, so it is
                # done only when the HAR output that consumes them is on.
                record_all_exchanges=bool(self.config.getoption("output_dir")),
            )
        except Exception as e:
            raise pytest.Collector.CollectError(f"Cannot build test class for {self.path}: {e}") from None
        # _getobj() would try to import the .json file as a Python module, so
        # hand pytest an in-memory module carrying the generated class instead.
        dummy_module = types.ModuleType("generated")
        setattr(dummy_module, self.name, CarrierClass)
        self._getobj = lambda: dummy_module  # ty: ignore[invalid-assignment]
        json_class = pytest.Class.from_parent(
            self,
            path=self.path,
            name=self.name,
            obj=CarrierClass,
        )

        # Keeps the scenario's stages on one worker under --dist loadgroup.
        # Guarded: without xdist the marker fails --strict-markers.
        if self.config.pluginmanager.hasplugin("xdist"):
            json_class.add_marker(pytest.mark.xdist_group(name=self.nodeid))

        for mark_str in scenario.marks:
            try:
                json_class.add_marker(make_marker(mark_str))
            except Exception as e:
                raise pytest.Collector.CollectError(f"Invalid marker '{mark_str}' in {self.path}: {e}") from None

        yield json_class


# Collection order, recorded before any sorter runs: the regroup tiebreaker.
_ORIGINAL_POSITIONS: pytest.StashKey[dict[pytest.Item, int]] = pytest.StashKey()


def _regroup_carrier_items(items: list[pytest.Item], original_position: dict[pytest.Item, int]) -> None:
    """Re-sort collected items so each scenario class's stages run contiguously,
    in stage order.

    Leaving a class finalizes its scope, and ``Carrier.teardown_class`` resets
    the chain — so any sorter that interleaves two scenarios breaks both. The
    main offender is pytest-order: every scenario carries the same
    ``order(0..n-1)`` marks, which its session-wide scope interleaves into A0,
    B0, A1, B1. Each class is pulled together at its first item (preserving
    inter-class order), with ``original_position`` breaking ties so parametrized
    instances of a stage keep collection order.
    """
    buckets: dict[type, list[pytest.Item]] = {}
    for item in items:
        cls = getattr(item, "cls", None)
        if cls is not None and issubclass(cls, Carrier):
            buckets.setdefault(cls, []).append(item)
    if not buckets:
        return

    def stage_key(item: pytest.Item) -> tuple[int, int]:
        index = getattr(getattr(item, "function", None), "_httpchain_stage_index", 0)
        return (index, original_position.get(item, sys.maxsize))

    for bucket in buckets.values():
        bucket.sort(key=stage_key)

    regrouped: list[pytest.Item] = []
    emitted: set[type] = set()
    for item in items:
        cls = getattr(item, "cls", None)
        if cls is None or cls not in buckets:
            regrouped.append(item)
        elif cls not in emitted:
            emitted.add(cls)
            regrouped.extend(buckets[cls])
    items[:] = regrouped


@pytest.hookimpl(wrapper=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> Any:
    """Enforce chain contiguity after the non-wrapper sorters have run.

    Pre-yield the items are still in collection order, which is recorded as the
    tiebreaker; post-yield runs after pytest-order, pytest-randomly and friends.
    Sorters written as tryfirst wrappers finish even later — `pytest_collection_finish`
    catches those.
    """
    config.stash[_ORIGINAL_POSITIONS] = {item: i for i, item in enumerate(items)}
    result = yield
    _regroup_carrier_items(items, config.stash[_ORIGINAL_POSITIONS])
    return result


@pytest.hookimpl(tryfirst=True)
def pytest_collection_finish(session: pytest.Session) -> None:
    """Re-enforce chain contiguity after every sorter, including tryfirst
    wrappers (``--ff``, ``--order-after-ff``). ``tryfirst`` so xdist workers
    report the final, regrouped order to the controller.
    """
    positions = session.config.stash.get(_ORIGINAL_POSITIONS, {})
    _regroup_carrier_items(session.items, positions)


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node) -> None:
    """Pass the real dist mode to xdist workers, which cannot see it themselves
    (xdist resets their own ``dist`` option to "no")."""
    node.workerinput["httpchain_dist"] = node.config.getoption("dist", default="no")


# Applied by _get_ini: the options register with default=None, so None doubles
# as the "not explicitly set" sentinel for both ini-file and -o values.
_INI_DEFAULTS: dict[ConfigOptions, Any] = {
    ConfigOptions.SUFFIX: "http",
    ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH: 3,
    ConfigOptions.MAX_COMPREHENSION_LENGTH: 50000,
    ConfigOptions.MAX_PARALLEL_ITERATIONS: 10000,
}


def _get_ini(config: pytest.Config, option: ConfigOptions) -> Any:
    """An ini option's explicitly-set value, else its ``_INI_DEFAULTS`` entry."""
    value = config.getini(option)
    if value is not None:
        return value
    return _INI_DEFAULTS[option]


def pytest_addoption(parser: pytest.Parser) -> None:
    ini_options: list[tuple[ConfigOptions, str, str]] = [
        (ConfigOptions.SUFFIX, "File suffix for HTTP test files.", "string"),
        (ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH, "Maximum number of parent directory traversals allowed in $ref paths.", "int"),
        (ConfigOptions.MAX_COMPREHENSION_LENGTH, "Maximum length for list/dict comprehensions in template expressions.", "int"),
        (ConfigOptions.MAX_PARALLEL_ITERATIONS, "Maximum number of parallel iterations allowed per stage.", "int"),
    ]
    for option, help_text, ini_type in ini_options:
        parser.addini(name=option, help=f"{help_text} Default: {_INI_DEFAULTS[option]}.", type=ini_type, default=None)  # ty: ignore[invalid-argument-type]
    parser.addoption(
        "--httpchain-output-dir",
        dest="output_dir",
        default=None,
        help="Directory to write test output files (HAR format for HTTP communications).",
    )


def pytest_configure(config: pytest.Config) -> None:
    # pytest converts type="int" options with a bare int(), whose ValueError it
    # renders as an INTERNALERROR; wrap it into a clean usage error.
    def _getint(option: ConfigOptions, minimum: int, minimum_message: str, maximum: int | None = None) -> int:
        try:
            value = _get_ini(config, option)
        except ValueError as e:
            raise pytest.UsageError(f"{option} must be an integer: {e}") from None
        if value < minimum:
            raise pytest.UsageError(f"{option} {minimum_message}")
        if maximum is not None and value > maximum:
            raise pytest.UsageError(f"{option} must not exceed {maximum:,}")
        return value

    suffix = str(_get_ini(config, ConfigOptions.SUFFIX))
    if not re.match(r"^[a-zA-Z0-9_-]{1,32}$", suffix):
        raise pytest.UsageError(f"{ConfigOptions.SUFFIX} must contain only alphanumeric characters, underscores, hyphens, and be ≤32 chars")

    _getint(ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH, minimum=0, minimum_message="must be non-negative")
    max_comprehension_length = _getint(ConfigOptions.MAX_COMPREHENSION_LENGTH, minimum=1, minimum_message="must be a positive integer", maximum=1_000_000)
    _getint(ConfigOptions.MAX_PARALLEL_ITERATIONS, minimum=1, minimum_message="must be a positive integer", maximum=1_000_000)

    set_max_comprehension_length(max_comprehension_length)


def pytest_collect_file(file_path: Path, parent: pytest.Collector) -> pytest.Collector | None:
    suffix: str = _get_ini(parent.config, ConfigOptions.SUFFIX)
    pattern = re.compile(rf"^test_(?P<name>.+)\.{re.escape(suffix)}\.json$")
    file_match = pattern.match(file_path.name)
    if file_match:
        return JsonModule.from_parent(parent, path=file_path, name=file_match.group("name"))
    return None


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Any:
    # The yielded report is augmented in place and returned so it propagates to
    # outer wrappers.
    report: pytest.TestReport = yield

    if call.when == "call":
        if hasattr(item, "instance") and isinstance(item.instance, Carrier):
            carrier = item.instance

            # An initialization failure breaks the whole scenario and must stay
            # red, so undo the xfail conversion pytest's skipping plugin already
            # applied (this wrapper is outermost, so every consumer sees the
            # flip). `wasxfail` holds a reason string: presence is the signal.
            if type(carrier)._init_failed is not None and report.skipped and hasattr(report, "wasxfail"):
                report.outcome = "failed"
                del report.wasxfail

            # A parallel stage runs many exchanges but shows one; say so rather
            # than presenting it as the stage's only exchange.
            suffix = ""
            if carrier.last_iterations_attempted > 1:
                shown = "failing" if report.failed else "last"
                suffix = f" ({shown} of {carrier.last_iterations_attempted} parallel iterations)"

            for title, what, exchange, formatter in (
                ("HTTP Request", "request", carrier.last_request, format_request),
                ("HTTP Response", "response", carrier.last_response, format_response),
            ):
                if exchange is None:
                    continue
                try:
                    body = formatter(exchange)  # ty: ignore[invalid-argument-type]
                except Exception as e:
                    body = f"<Error formatting {what}: {e}>"
                report.sections.append((f"{title}{suffix}", body))

            output_dir = item.config.getoption("output_dir")
            if output_dir and carrier.last_exchanges:
                try:
                    har_path = write_har_file(
                        output_dir=Path(output_dir),
                        test_name=item.nodeid,
                        exchanges=carrier.last_exchanges,
                    )
                    report.sections.append(("HAR File", str(har_path)))
                except Exception as e:
                    logger.warning(f"Failed to write HAR file for {item.nodeid}: {e}")

    return report
