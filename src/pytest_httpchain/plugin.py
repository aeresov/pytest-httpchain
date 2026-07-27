"""pytest plugin entry point: discovery, collection, and reporting hooks.

Registered as the ``pytest11`` entry point, this module wires HTTP-chain JSON
scenarios into pytest:

- ``pytest_addoption`` / ``pytest_configure`` register and validate the ini
  options (``httpchain_suffix``, ``httpchain_ref_parent_traversal_depth``,
  ``httpchain_max_comprehension_length``, ``httpchain_max_parallel_iterations``)
  and the ``--httpchain-output-dir`` flag.
- ``pytest_collect_file`` matches ``test_<name>.<suffix>.json`` files and hands
  them to `JsonModule`.
- `JsonModule.collect` loads the JSON (resolving ``$ref``), validates it
  against the `Scenario` model, runs the semantic validator
  (warnings become `ScenarioValidationWarning`, errors become
  ``CollectError``), and builds the dynamic test class via
  ``factory.create_test_class``.
- ``pytest_runtest_makereport`` attaches the last HTTP request/response to the
  test report and optionally writes a HAR file.
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
    """JSON test module: collects HTTP chain test scenarios.

    This class extends pytest's Module to handle JSON test files containing
    HTTP chain test scenarios. It loads, validates, and converts JSON test
    definitions into executable pytest test classes — execution itself belongs
    to `Carrier` under pytest's runner.
    """

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

        A multi-stage scenario forms one ordered chain over shared class state
        (Carrier ClassVars), and no in-worker ordering can reunite a chain
        scattered across workers — so dist modes that distribute tests
        individually (load/each/worksteal) would break the chain silently.
        Class-preserving modes work: loadscope groups by class, loadfile by
        file, loadgroup by the xdist_group marker
        added in `collect`. Single-stage scenarios have no chain and are safe
        under any mode (a parametrized single stage never consumes its own
        saves), so they are exempt.

        Inside a worker the real mode is only available via workerinput
        (seeded by `pytest_configure_node` below): xdist resets the worker's
        own ``dist`` option to "no" so workers don't recursively spawn.
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
        # Load, $ref-resolve and schema-validate through the same pipeline the
        # CLI uses (validation.load_scenario); only the root path differs, and
        # only in authority: collection has pytest's real rootpath, while the
        # CLI default (validation.resolve_root_path) approximates it.
        ref_parent_traversal_depth = self.config.getini(ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH)
        try:
            # Record resolver warnings instead of letting them escape raw: under
            # `filterwarnings = error` a bare AmbiguousReferenceWarning would be
            # promoted mid-load and land in the generic handler below as a
            # misleading "Failed to parse JSON file". Recorded warnings are
            # re-emitted after loading, in the same [HTTPCHAINxxx] form as the
            # semantic diagnostics.
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
            CarrierClass = create_test_class(
                scenario,
                self.name,
                max_parallel_iterations=max_parallel_iterations,
                scenario_dir=self.path.parent,
                # Retaining every parallel iteration's exchange costs memory, so
                # it is only done when the HAR output that consumes them is on.
                record_all_exchanges=bool(self.config.getoption("httpchain_output_dir")),
            )
        except Exception as e:
            # create_test_class parses stage markers and — only when stage
            # parametrize values contain templates — resolves scenario
            # substitutions (which can execute user functions). Client/auth/ssl
            # initialization is deferred to first stage execution
            # (Carrier._ensure_initialized), so collection stays free of user
            # code otherwise. Surface any failure as a clean collection error,
            # like the sibling load/validate paths above.
            raise pytest.Collector.CollectError(f"Cannot build test class for {self.path}: {e}") from None
        dummy_module = types.ModuleType("generated")
        setattr(dummy_module, self.name, CarrierClass)
        # Consumed by the _getobj() override below; pytest.Class resolves the
        # test class via getattr(parent.obj, name).
        self._generated_module = dummy_module
        json_class = pytest.Class.from_parent(
            self,
            path=self.path,
            name=self.name,
        )

        # Keep all stages of this scenario on one xdist worker under
        # --dist loadgroup. Guarded by plugin presence: without xdist the
        # marker is unregistered and would fail --strict-markers.
        if self.config.pluginmanager.hasplugin("xdist"):
            json_class.add_marker(pytest.mark.xdist_group(name=self.nodeid))

        # apply class-level markers
        for mark_str in scenario.marks:
            try:
                json_class.add_marker(make_marker(mark_str))
            except Exception as e:
                raise pytest.Collector.CollectError(f"Invalid marker '{mark_str}' in {self.path}: {e}") from None

        yield json_class


# Collection (definition) order of all items, recorded before any sorter runs.
# Used as the regroup sort tiebreaker so parametrized instances of one stage
# are restored to their original order even after a shuffling plugin.
_ORIGINAL_POSITIONS: pytest.StashKey[dict[pytest.Item, int]] = pytest.StashKey()


def _regroup_carrier_items(items: list[pytest.Item], original_position: dict[pytest.Item, int]) -> None:
    """Re-sort collected items so each scenario class's stages run contiguously,
    in stage order.

    A multi-stage scenario is one ordered chain over shared class state, and
    pytest finalizes class scope every time execution leaves the class —
    ``Carrier.teardown_class`` resets the chain's saved context. Any sorter
    that splits a scenario class's items therefore breaks the chain. A
    canonical offender is a user-installed pytest-order acting on user-authored
    ``order(...)`` stage marks: with matching indices across scenarios, its
    default session-wide group scope stable-sorts them into A0, B0, A1, B1, ...
    (pytest-randomly's shuffle and core's ``--ff`` are others).

    Each scenario class's items are pulled together at the position of the
    class's first item — preserving inter-class order — and sorted back into
    stage order within the class; ``original_position`` breaks ties so
    parametrized instances of one stage run in collection order (the last
    instance's save is what later stages consume).
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

    The pre-yield half runs before any non-wrapper implementation: items are
    still in collection (definition) order, which is recorded as the regroup
    tiebreaker. The post-yield half runs after all non-wrapper
    ``pytest_collection_modifyitems`` implementations (pytest-order,
    pytest-randomly, ...) and regroups scenario chains. Sorters implemented as
    *tryfirst wrappers* (core cacheprovider's ``--ff``, pytest-order's
    ``--order-after-ff``) post-yield even later than this hook — those are
    caught by `pytest_collection_finish` below.
    """
    config.stash[_ORIGINAL_POSITIONS] = {item: i for i, item in enumerate(items)}
    result = yield
    _regroup_carrier_items(items, config.stash[_ORIGINAL_POSITIONS])
    return result


@pytest.hookimpl(tryfirst=True)
def pytest_collection_finish(session: pytest.Session) -> None:
    """Re-enforce chain contiguity after ALL ``pytest_collection_modifyitems``
    activity — including tryfirst wrappers whose post-yield runs after this
    plugin's own wrapper (core cacheprovider's ``--ff`` reorder, pytest-order's
    ``--order-after-ff``). ``tryfirst`` so this runs before xdist's
    WorkerInteractor reports collected IDs to the controller: workers must
    report the final, regrouped order.
    """
    positions = session.config.stash.get(_ORIGINAL_POSITIONS, {})
    _regroup_carrier_items(session.items, positions)


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node) -> None:
    """xdist controller-side hook: pass the real dist mode to workers.

    Workers cannot see it themselves — xdist resets ``config.option.dist`` to
    "no" inside workers — so `JsonModule.collect` reads this key instead. The
    hook only exists when pytest-xdist is installed (hence ``optionalhook``).
    """
    node.workerinput["httpchain_dist"] = node.config.getoption("dist", default="no")


def pytest_addoption(parser: pytest.Parser) -> None:
    ini_options: list[tuple[ConfigOptions, str, str, Any]] = [
        (ConfigOptions.SUFFIX, "File suffix for HTTP test files.", "string", "http"),
        (ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH, "Maximum number of parent directory traversals allowed in $ref paths.", "int", 3),
        (ConfigOptions.MAX_COMPREHENSION_LENGTH, "Maximum length for list/dict comprehensions in template expressions.", "int", 50000),
        (ConfigOptions.MAX_PARALLEL_ITERATIONS, "Maximum number of parallel iterations allowed per stage.", "int", 10000),
    ]
    for option, help_text, ini_type, default in ini_options:
        # pytest does not render ini defaults in --help, so keep them in the
        # help text too.
        parser.addini(name=option, help=f"{help_text} Default: {default}.", type=ini_type, default=default)  # ty: ignore[invalid-argument-type]
    group = parser.getgroup("httpchain", "HTTP chain scenario testing")
    group.addoption(
        # No dest= override: argparse derives httpchain_output_dir, keeping
        # the plugin prefix in pytest's shared option namespace.
        "--httpchain-output-dir",
        default=None,
        help="Directory to write test output files (HAR format for HTTP communications).",
    )


def pytest_configure(config: pytest.Config) -> None:
    # Numeric options are registered with type="int", but pytest performs the
    # int() conversion with a bare int(value) that raises ValueError for a
    # non-integer ini value — which pytest renders as an INTERNALERROR traceback.
    # Wrap the read so a garbage value becomes a clean usage error; the range
    # checks likewise raise pytest.UsageError.
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
    if not re.match(r"^[a-zA-Z0-9_-]{1,32}$", suffix):
        raise pytest.UsageError(f"{ConfigOptions.SUFFIX} must contain only alphanumeric characters, underscores, hyphens, and be ≤32 chars")

    _getint(ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH, minimum=0, minimum_message="must be non-negative")
    max_comprehension_length = _getint(ConfigOptions.MAX_COMPREHENSION_LENGTH, minimum=1, minimum_message="must be a positive integer", maximum=1_000_000)
    _getint(ConfigOptions.MAX_PARALLEL_ITERATIONS, minimum=1, minimum_message="must be a positive integer", maximum=1_000_000)

    set_max_comprehension_length(max_comprehension_length)


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

            # A scenario-initialization failure (broken auth function, bad
            # cert, unresolvable scenario substitutions) is scenario-level
            # breakage, not the stage-level "expected failure" an xfail mark
            # declares — pre-0.10 it was a hard collection error regardless of
            # marks and must stay red. This wrapper registers after pytest's
            # own skipping plugin, so it is OUTERMOST and its post-yield runs
            # last:
            # the xfail conversion has already happened by the time the report
            # arrives here, and flipping it back is seen consistently by every
            # downstream consumer (Session's failure counter, the terminal,
            # xdist's worker->controller forwarding). NB: ``wasxfail`` holds
            # the mark's REASON string (often empty) — presence, not
            # truthiness, is the signal.
            if type(carrier)._init_failed is not None and report.skipped and hasattr(report, "wasxfail"):
                report.outcome = "failed"
                del report.wasxfail

            # A parallel stage runs many exchanges but the report shows ONE
            # (the failing iteration, else the last) — say so in the section
            # title instead of presenting it as the stage's only exchange.
            suffix = ""
            if carrier.last_iterations_attempted > 1:
                shown = "failing" if report.failed else "last"
                suffix = f" ({shown} of {carrier.last_iterations_attempted} parallel iterations)"

            # The shown request is the FINAL hop's after redirects — say so,
            # since it may differ from the request the stage authored. The
            # full chain is in the HAR output (every hop is an exchange).
            if carrier.last_response is not None and carrier.last_response.history:
                hops = len(carrier.last_response.history)
                suffix += f" (after {hops} redirect{'s' if hops != 1 else ''})"

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

            output_dir = item.config.getoption("httpchain_output_dir")
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
