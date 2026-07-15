"""pytest plugin entry point: discovery, collection, and reporting hooks.

Registered as the ``pytest11`` entry point, this module wires HTTP-chain JSON
scenarios into pytest:

- ``pytest_addoption`` / ``pytest_configure`` register and validate the ini
  options (``httpchain_suffix``, ``httpchain_ref_parent_traversal_depth``,
  ``httpchain_max_comprehension_length``, ``httpchain_max_parallel_iterations``,
  plus their deprecated un-prefixed pre-0.10 aliases) and the
  ``--httpchain-output-dir`` flag (deprecated alias ``--output-dir``).
- ``pytest_collect_file`` matches ``test_<name>.<suffix>.json`` files and hands
  them to `JsonModule`.
- `JsonModule.collect` loads the JSON (resolving ``$ref``), validates it
  against the `Scenario` model, runs the semantic validator
  (warnings become `ScenarioValidationWarning`, errors become
  ``CollectError``), and builds the dynamic test class via
  ``carrier.create_test_class``.
- ``pytest_runtest_makereport`` attaches the last HTTP request/response to the
  test report and optionally writes a HAR file.
"""

import logging
import os
import re
import types
import warnings
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import pytest_httpchain.jsonref
from pytest_httpchain.constants import LEGACY_INI_NAMES, ConfigOptions
from pytest_httpchain.models import Scenario
from pytest_httpchain.templates import set_max_comprehension_length

from .carrier import Carrier, create_test_class
from .har_writer import write_har_file
from .report_formatter import format_request, format_response
from .utils import make_marker
from .validation import check_scenario
from .warnings import ScenarioValidationWarning

logger = logging.getLogger(__name__)


class JsonModule(pytest.Module):
    """JSON test module that collects and executes HTTP chain tests.

    This class extends pytest's Module to handle JSON test files containing
    HTTP chain test scenarios. It loads, validates, and converts JSON test
    definitions into executable pytest test classes.
    """

    def _reject_chain_splitting_dist_mode(self, scenario: Scenario) -> None:
        """Fail collection when pytest-xdist would scatter a stage chain.

        A multi-stage scenario forms one ordered chain over shared class state
        (Carrier ClassVars), and pytest-order is a no-op across xdist workers —
        so dist modes that distribute tests individually (load/each/worksteal)
        would break the chain silently. Class-preserving modes work: loadscope
        groups by class, loadfile by file, loadgroup by the xdist_group marker
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
        # read JSON and apply references
        ref_parent_traversal_depth = _get_ini(self.config, ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH)
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
        max_parallel_iterations = _get_ini(self.config, ConfigOptions.MAX_PARALLEL_ITERATIONS)
        try:
            CarrierClass = create_test_class(scenario, self.name, max_parallel_iterations=max_parallel_iterations, scenario_dir=self.path.parent)
        except Exception as e:
            # create_test_class parses stage markers and — only when stage
            # parametrize values contain templates — resolves scenario
            # substitutions (which can execute user functions). Client/auth/ssl
            # initialization is deferred to first stage execution
            # (Carrier._ensure_initialized), so collection stays free of user
            # code otherwise. Surface any failure as a clean collection error,
            # like the sibling load/validate paths above.
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


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node) -> None:
    """xdist controller-side hook: pass the real dist mode to workers.

    Workers cannot see it themselves — xdist resets ``config.option.dist`` to
    "no" inside workers — so `JsonModule.collect` reads this key instead. The
    hook only exists when pytest-xdist is installed (hence ``optionalhook``).
    """
    node.workerinput["httpchain_dist"] = node.config.getoption("dist", default="no")


def _explicitly_set(config: pytest.Config, name: str) -> bool:
    """True if an ini option was explicitly set — in the ini file OR via ``-o``.

    ``config.inicfg`` alone is not enough: on pytest 8.x it does not include
    ``-o``/``--override-ini`` values (pytest 9 merges them in), so relying on it
    silently dropped CLI overrides. ``getini()`` itself applies overrides on
    both versions; only this explicit-set detection needs the extra scan.
    """
    if name in config.inicfg:
        return True
    for override in config.getoption("override_ini", None) or []:
        key, sep, _ = override.partition("=")
        if sep and key.strip() == name:
            return True
    return False


def _get_ini(config: pytest.Config, option: ConfigOptions) -> Any:
    """Read an httpchain ini option, honoring its deprecated pre-0.10 alias.

    Precedence: the ``httpchain_``-prefixed name when explicitly set, else the
    legacy un-prefixed name when explicitly set, else the registered default.
    The deprecation warning for a set legacy name is issued once, in
    ``pytest_configure`` — not here, since collection reads run per file.
    """
    if _explicitly_set(config, str(option)):
        return config.getini(option)
    legacy = LEGACY_INI_NAMES[option]
    if _explicitly_set(config, legacy):
        return config.getini(legacy)
    return config.getini(option)


def pytest_addoption(parser: pytest.Parser) -> None:
    ini_options: list[tuple[ConfigOptions, str, str, Any]] = [
        (ConfigOptions.SUFFIX, "File suffix for HTTP test files.", "string", "http"),
        (ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH, "Maximum number of parent directory traversals allowed in $ref paths.", "int", 3),
        (ConfigOptions.MAX_COMPREHENSION_LENGTH, "Maximum length for list/dict comprehensions in template expressions.", "int", 50000),
        (ConfigOptions.MAX_PARALLEL_ITERATIONS, "Maximum number of parallel iterations allowed per stage.", "int", 10000),
    ]
    for option, help_text, ini_type, default in ini_options:
        parser.addini(name=option, help=help_text, type=ini_type, default=default)  # ty: ignore[invalid-argument-type]
        # Deprecated pre-0.10 alias; read via _get_ini, removal in 0.11.
        parser.addini(name=LEGACY_INI_NAMES[option], help=f"Deprecated alias of {option}.", type=ini_type, default=default)  # ty: ignore[invalid-argument-type]
    parser.addoption(
        "--httpchain-output-dir",
        "--output-dir",  # deprecated pre-0.10 alias, removal in 0.11
        dest="output_dir",
        default=None,
        help="Directory to write test output files (HAR format for HTTP communications).",
    )


def pytest_configure(config: pytest.Config) -> None:
    # One-time deprecation notices for the pre-0.10 option spellings.
    for option, legacy in LEGACY_INI_NAMES.items():
        if _explicitly_set(config, legacy):
            if _explicitly_set(config, str(option)):
                message = f"ini option '{legacy}' is deprecated and ignored because '{option}' is also set (removal in 0.11)"
            else:
                message = f"ini option '{legacy}' is deprecated, use '{option}' (removal in 0.11)"
            config.issue_config_time_warning(pytest.PytestDeprecationWarning(message), stacklevel=2)
    # The deprecated flag can arrive via argv, ini addopts, or PYTEST_ADDOPTS —
    # scan all three (invocation_params.args carries only argv).
    flag_sources = " ".join(
        [
            *config.invocation_params.args,
            str(config.inicfg.get("addopts", "")),
            os.environ.get("PYTEST_ADDOPTS", ""),
        ]
    )
    if "--output-dir" in flag_sources:
        config.issue_config_time_warning(
            pytest.PytestDeprecationWarning("flag '--output-dir' is deprecated, use '--httpchain-output-dir' (removal in 0.11)"),
            stacklevel=2,
        )

    # Numeric options are registered with type="int", but pytest performs the
    # int() conversion with a bare int(value) that raises ValueError for a
    # non-integer ini value — which pytest renders as an INTERNALERROR traceback.
    # Wrap the read so a garbage value becomes a clean usage error; the range
    # checks below likewise raise pytest.UsageError.
    def _getint(option: ConfigOptions) -> int:
        try:
            return _get_ini(config, option)
        except ValueError as e:
            raise pytest.UsageError(f"{option} must be an integer: {e}") from None

    suffix = str(_get_ini(config, ConfigOptions.SUFFIX))
    if not re.match(r"^[a-zA-Z0-9_-]{1,32}$", suffix):
        raise pytest.UsageError(f"{ConfigOptions.SUFFIX} must contain only alphanumeric characters, underscores, hyphens, and be ≤32 chars")

    ref_parent_traversal_depth = _getint(ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH)
    if ref_parent_traversal_depth < 0:
        raise pytest.UsageError(f"{ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH} must be non-negative")

    max_comprehension_length = _getint(ConfigOptions.MAX_COMPREHENSION_LENGTH)
    if max_comprehension_length < 1:
        raise pytest.UsageError(f"{ConfigOptions.MAX_COMPREHENSION_LENGTH} must be a positive integer")
    if max_comprehension_length > 1_000_000:
        raise pytest.UsageError(f"{ConfigOptions.MAX_COMPREHENSION_LENGTH} must not exceed 1,000,000")
    set_max_comprehension_length(max_comprehension_length)

    max_parallel_iterations = _getint(ConfigOptions.MAX_PARALLEL_ITERATIONS)
    if max_parallel_iterations < 1:
        raise pytest.UsageError(f"{ConfigOptions.MAX_PARALLEL_ITERATIONS} must be a positive integer")
    if max_parallel_iterations > 1_000_000:
        raise pytest.UsageError(f"{ConfigOptions.MAX_PARALLEL_ITERATIONS} must not exceed 1,000,000")


def pytest_collect_file(file_path: Path, parent: pytest.Collector) -> pytest.Collector | None:
    suffix: str = _get_ini(parent.config, ConfigOptions.SUFFIX)
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
