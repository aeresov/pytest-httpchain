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
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Literal

import pytest

from pytest_httpchain.carrier import Carrier
from pytest_httpchain.constants import ConfigOptions
from pytest_httpchain.factory import create_test_class
from pytest_httpchain.har_writer import write_har_file
from pytest_httpchain.models import Scenario
from pytest_httpchain.report_formatter import format_request, format_response
from pytest_httpchain.templates import get_max_comprehension_length, set_max_comprehension_length
from pytest_httpchain.utils import make_marker
from pytest_httpchain.validation import check_scenario, load_with_diagnostics
from pytest_httpchain.warnings import ScenarioValidationWarning

logger = logging.getLogger(__name__)


class JsonModule(pytest.Module):
    """Collector for one scenario file: loads, validates, and turns it into a
    test class. Execution belongs to `Carrier`, under pytest's runner."""

    # Seeded in collect(); consumed by _getobj().
    _generated_module: types.ModuleType

    def _getobj(self) -> types.ModuleType:
        """Return the in-memory module built in ``collect``.

        ``Module._getobj`` defaults to importing ``self.path`` as a Python
        module — a .json file — so it is overridden (the extension point
        ``_pytest.python.PyobjMixin`` documents) to hand pytest a generated
        module that already carries the test class.
        """
        return self._generated_module

    def _reject_chain_splitting_dist_mode(self, scenario: Scenario) -> None:
        """Fail collection when pytest-xdist would scatter a stage chain.

        Modes that distribute tests individually would break a multi-stage
        chain silently: no in-worker ordering can reunite a scattered chain.
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
        ref_parent_traversal_depth = self.config.getini(ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH)

        # Load failures arrive as coded diagnostics, the same ones `validate`
        # reports, and go through the one partition below — so a broken file is
        # described identically by the CLI and by collection.
        loaded, diagnostics = load_with_diagnostics(
            self.path,
            root_path=Path(self.config.rootpath),
            ref_parent_traversal_depth=ref_parent_traversal_depth,
        )
        if loaded is not None:
            scenario, test_data = loaded
            self._reject_chain_splitting_dist_mode(scenario)
            diagnostics += check_scenario(scenario, test_data)

        for diagnostic in diagnostics:
            if diagnostic.severity == "warning":
                warnings.warn(ScenarioValidationWarning(f"{self.path}: [{diagnostic.code}] {diagnostic.message}"), stacklevel=2)
        error_diagnostics = [d for d in diagnostics if d.severity == "error"]
        if error_diagnostics:
            detail = "\n".join(f"  - [{d.code}] {d.message}" for d in error_diagnostics)
            raise pytest.Collector.CollectError(f"Invalid test scenario in {self.path}:\n{detail}")
        assert loaded is not None, "a failed load always yields at least one error diagnostic"

        max_parallel_iterations = self.config.getini(ConfigOptions.MAX_PARALLEL_ITERATIONS)
        try:
            CarrierClass = create_test_class(
                scenario,
                self.name,
                max_parallel_iterations=max_parallel_iterations,
                scenario_dir=self.path.parent,
                # Retaining every iteration's exchange costs memory, so it is
                # done only when the HAR output that consumes them is on.
                record_all_exchanges=bool(self.config.getoption("httpchain_output_dir")),
            )
        except Exception as e:
            raise pytest.Collector.CollectError(f"Cannot build test class for {self.path}: {e}") from None
        dummy_module = types.ModuleType("generated")
        setattr(dummy_module, self.name, CarrierClass)
        # Consumed by _getobj(); pytest.Class resolves the class off it.
        self._generated_module = dummy_module
        json_class = pytest.Class.from_parent(
            self,
            path=self.path,
            name=self.name,
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


def _carrier_class(item: pytest.Item) -> type[Carrier] | None:
    """The generated scenario class ``item`` belongs to, or None for any other test."""
    cls = getattr(item, "cls", None)
    return cls if isinstance(cls, type) and issubclass(cls, Carrier) else None


def _stage_index(item: pytest.Item) -> int:
    """The stage position `factory.create_test_class` stamped on the item's method."""
    return getattr(getattr(item, "function", None), "_httpchain_stage_index", 0)


def _regroup_carrier_items(items: list[pytest.Item], original_position: dict[pytest.Item, int]) -> None:
    """Re-sort collected items so each scenario class's stages run contiguously,
    in stage order.

    Leaving a class finalizes its scope, and ``Carrier.teardown_class`` resets
    the chain — so any sorter that interleaves two scenarios breaks both
    (a user-installed pytest-order acting on user-authored ``order(...)`` marks,
    pytest-randomly's shuffle, core's ``--ff``). Each class is pulled together
    at its first item (preserving inter-class order), with ``original_position``
    breaking ties so parametrized instances of a stage keep collection order.
    """
    buckets: dict[type[Carrier], list[pytest.Item]] = {}
    for item in items:
        if (cls := _carrier_class(item)) is not None:
            buckets.setdefault(cls, []).append(item)
    if not buckets:
        return

    def stage_key(item: pytest.Item) -> tuple[int, int]:
        return (_stage_index(item), original_position.get(item, sys.maxsize))

    for bucket in buckets.values():
        bucket.sort(key=stage_key)

    regrouped: list[pytest.Item] = []
    emitted: set[type[Carrier]] = set()
    for item in items:
        cls = _carrier_class(item)
        if cls is None:
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


def _warn_on_split_chains(items: list[pytest.Item]) -> None:
    """Warn when selection (``--lf``, ``-k``, ``--deselect``, ``--sw``) dropped
    earlier stages of a chain while later ones remain.

    Reordering and dist-mode scattering are prevented outright, but pytest's
    selection mechanisms silently orphan a chain's tail: the surviving stages
    run without the deselected stages' saved context and fail with misleading
    undefined-variable errors (or worse, run against un-set-up server state).
    """
    selected_indices: dict[type[Carrier], set[int]] = {}
    for item in items:
        if (cls := _carrier_class(item)) is not None:
            selected_indices.setdefault(cls, set()).add(_stage_index(item))

    for cls, indices in selected_indices.items():
        scenario = cls.scenario
        if scenario is None or len(scenario.stages) <= 1:
            continue
        missing = set(range(max(indices))) - indices
        if missing:
            names = [scenario.stages[j].name for j in sorted(missing) if j < len(scenario.stages)]
            try:
                warnings.warn(
                    ScenarioValidationWarning(
                        f"Scenario '{cls.__name__}': earlier stage(s) {names} were deselected (e.g. by --lf, -k, or --deselect) "
                        f"while later stages of the chain remain selected; the surviving stages will run without their saved context"
                    ),
                    stacklevel=2,
                )
            except ScenarioValidationWarning as promoted:
                # Promoted by filterwarnings=error. Unlike every other warning
                # site in the plugin, this one runs inside pytest_collection_finish,
                # which has no warning-to-error recovery — escaping raw would end
                # the session in an INTERNALERROR traceback. A UsageError keeps
                # the user's "warnings are errors" policy while failing cleanly.
                raise pytest.UsageError(str(promoted)) from None


@pytest.hookimpl(tryfirst=True)
def pytest_collection_finish(session: pytest.Session) -> None:
    """Re-enforce chain contiguity after every sorter, including tryfirst
    wrappers (``--ff``, ``--order-after-ff``). ``tryfirst`` so xdist workers
    report the final, regrouped order to the controller.
    """
    positions = session.config.stash.get(_ORIGINAL_POSITIONS, {})
    _regroup_carrier_items(session.items, positions)
    _warn_on_split_chains(session.items)


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node) -> None:
    """Pass the real dist mode to xdist workers, which cannot see it themselves
    (xdist resets their own ``dist`` option to "no")."""
    node.workerinput["httpchain_dist"] = node.config.getoption("dist", default="no")


def pytest_addoption(parser: pytest.Parser) -> None:
    ini_options: list[tuple[ConfigOptions, str, Literal["string", "int"], Any]] = [
        (ConfigOptions.SUFFIX, "File suffix for HTTP test files.", "string", "http"),
        (ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH, "Maximum number of parent directory traversals allowed in $ref paths.", "int", 3),
        (ConfigOptions.MAX_COMPREHENSION_LENGTH, "Maximum length for list/dict comprehensions in template expressions.", "int", 50000),
        (ConfigOptions.MAX_PARALLEL_ITERATIONS, "Maximum number of parallel iterations allowed per stage.", "int", 10000),
    ]
    for option, help_text, ini_type, default in ini_options:
        # pytest does not render ini defaults in --help, so repeat them there.
        parser.addini(name=option, help=f"{help_text} Default: {default}.", type=ini_type, default=default)
    group = parser.getgroup("httpchain", "HTTP chain scenario testing")
    group.addoption(
        # No dest= override: argparse derives httpchain_output_dir, keeping the
        # plugin prefix in pytest's shared option namespace.
        "--httpchain-output-dir",
        default=None,
        help="Directory to write test output files (HAR format for HTTP communications).",
    )


# The cap found at configure time, restored at unconfigure: the setting writes a
# process-wide simpleeval global, which an in-process pytester run (or any nested
# pytest session) must not leak to the enclosing process.
_PREVIOUS_MAX_COMPREHENSION_LENGTH: pytest.StashKey[int] = pytest.StashKey()


def pytest_configure(config: pytest.Config) -> None:
    # Through pytest 9, type="int" options are converted with a bare int() whose
    # ValueError is rendered as an INTERNALERROR; wrap it into a clean usage
    # error. pytest 10 raises a UsageError itself, so the except branch stops
    # firing there — keep it until the floor moves past 9.
    def _getint(option: ConfigOptions, minimum: int, minimum_message: str, maximum: int | None = None) -> int:
        try:
            value = config.getini(option)
        except ValueError as e:
            raise pytest.UsageError(f"{option} must be an integer: {e}") from None
        if value < minimum:
            raise pytest.UsageError(f"{option} {minimum_message}")
        if maximum is not None and value > maximum:
            raise pytest.UsageError(f"{option} must not exceed {maximum:,}")
        return value

    suffix = str(config.getini(ConfigOptions.SUFFIX))
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", suffix):
        raise pytest.UsageError(f"{ConfigOptions.SUFFIX} must contain only alphanumeric characters, underscores, hyphens, and be ≤32 chars")

    _getint(ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH, minimum=0, minimum_message="must be non-negative")
    max_comprehension_length = _getint(ConfigOptions.MAX_COMPREHENSION_LENGTH, minimum=1, minimum_message="must be a positive integer", maximum=1_000_000)
    _getint(ConfigOptions.MAX_PARALLEL_ITERATIONS, minimum=1, minimum_message="must be a positive integer", maximum=1_000_000)

    config.stash[_PREVIOUS_MAX_COMPREHENSION_LENGTH] = get_max_comprehension_length()
    set_max_comprehension_length(max_comprehension_length)


def pytest_unconfigure(config: pytest.Config) -> None:
    previous = config.stash.get(_PREVIOUS_MAX_COMPREHENSION_LENGTH, None)
    if previous is not None:
        set_max_comprehension_length(previous)


def pytest_collect_file(file_path: Path, parent: pytest.Collector) -> pytest.Collector | None:
    suffix: str = parent.config.getini(ConfigOptions.SUFFIX)
    if file_match := re.fullmatch(rf"test_(?P<name>.+)\.{re.escape(suffix)}\.json", file_path.name):
        return JsonModule.from_parent(parent, path=file_path, name=file_match["name"])
    return None


def _sections_will_be_shown(config: pytest.Config, report: pytest.TestReport) -> bool:
    """Whether pytest will actually print this report's sections.

    Formatting an exchange re-parses and re-serializes its whole body, which on a
    suite of passing stages nobody ever reads: the terminal renders sections from
    the FAILURES block, and from the PASSES block only under -rP/-rA (XFAILURES
    needs --xfail-tb). An xdist worker unregisters the terminal reporter and ships
    its sections to the controller, which does the rendering — so there, yes.
    """
    if report.failed:
        return True
    terminal_reporter = config.pluginmanager.get_plugin("terminalreporter")
    if terminal_reporter is None:
        return True
    return terminal_reporter.hasopt("P") or bool(config.option.xfail_tb)


def _format_section[T](what: str, formatter: Callable[[T], str], exchange: T) -> str:
    """A report section body. Reporting must never break the report, so a
    formatter failure becomes the section's text."""
    try:
        return formatter(exchange)
    except Exception as e:
        return f"<Error formatting {what}: {e}>"


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Any:
    # tryfirst makes this the outermost wrapper: its post-yield half sees the
    # final outcome after pytest has applied skip/xfail/strict semantics.
    report: pytest.TestReport = yield

    carrier_class = _carrier_class(item)

    if call.when == "call" and carrier_class is not None:
        # An initialization failure breaks the whole scenario and must stay
        # red, so undo the xfail conversion pytest's skipping plugin already
        # applied (this wrapper is outermost, so every consumer sees the
        # flip). `wasxfail` holds a reason string: presence is the signal.
        if carrier_class._init_failed is not None and report.skipped and hasattr(report, "wasxfail"):
            report.outcome = "failed"
            del report.wasxfail

        # A parallel stage runs many exchanges but shows one; say so rather
        # than presenting it as the stage's only exchange. A failure that
        # never recorded a request (template error, rate-limit timeout)
        # shows the last COMPLETED iteration, which must not be labeled as
        # the failing one.
        suffix = ""
        if carrier_class.last_iterations_attempted > 1:
            if carrier_class.last_shown_exchange_is_failed:
                shown = "failing"
            elif report.failed:
                shown = "last completed"
            else:
                shown = "last"
            suffix = f" ({shown} of {carrier_class.last_iterations_attempted} parallel iterations)"

        # The shown request is the final hop's, which may differ from what
        # the stage authored; the full chain is in the HAR output.
        if carrier_class.last_response is not None and carrier_class.last_response.history:
            hops = len(carrier_class.last_response.history)
            suffix += f" (after {hops} redirect{'s' if hops != 1 else ''})"

        if _sections_will_be_shown(item.config, report):
            if (request := carrier_class.last_request) is not None:
                report.sections.append((f"HTTP Request{suffix}", _format_section("request", format_request, request)))
            if (response := carrier_class.last_response) is not None:
                report.sections.append((f"HTTP Response{suffix}", _format_section("response", format_response, response)))

        output_dir = item.config.getoption("httpchain_output_dir")
        if output_dir and carrier_class.last_exchanges:
            try:
                har_path = write_har_file(
                    output_dir=Path(output_dir),
                    test_name=item.nodeid,
                    exchanges=carrier_class.last_exchanges,
                )
                report.sections.append(("HAR File", str(har_path)))
            except Exception as e:
                logger.warning("Failed to write HAR file for %s: %s", item.nodeid, e)

    # The report, not the stage body, is the source of truth. Fixture setup and
    # teardown can fail without execute_stage running, and strict XPASS plus
    # string xfail conditions are classified only by pytest. Expected xfails and
    # ordinary skips are `skipped`, so they deliberately leave the chain healthy.
    if carrier_class is not None and report.failed:
        carrier_class.aborted = True

    return report
