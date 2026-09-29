"""pytest plugin entry point (the ``pytest11`` hook module).

Registers the ini options and ``--httpchain-output-dir``, collects
``test_<name>.<suffix>.json`` files into `JsonModule`, keeps each scenario's
stages contiguous and ordered, and attaches the HTTP exchange (plus an optional
HAR file) to test reports, credentials redacted.
"""

import functools
import logging
import re
import sys
import types
import warnings
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest

from pytest_httpchain.body_schema import ReferenceBounds
from pytest_httpchain.carrier import Carrier
from pytest_httpchain.constants import ConfigOptions
from pytest_httpchain.factory import create_test_class
from pytest_httpchain.har_writer import write_har_file
from pytest_httpchain.models import Scenario
from pytest_httpchain.redaction import DEFAULT_REDACT_HEADERS, DEFAULT_REDACT_QUERY_PARAMS, NO_REDACTION, Redaction
from pytest_httpchain.report_formatter import format_curl, format_request, format_response
from pytest_httpchain.templates import get_max_comprehension_length, set_max_comprehension_length
from pytest_httpchain.utils import make_marker, xdist_group_names
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
        chain to break.
        """
        if len(scenario.stages) <= 1:
            return
        dist_mode = _dist_mode(self.config)
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
                # The bounds the scenario's $include was held to, for its body
                # schemas' references (body_schema).
                ref_bounds=ReferenceBounds(Path(self.config.rootpath), ref_parent_traversal_depth),
                # Retaining every iteration's exchange costs memory, so it is
                # done only when the HAR output that consumes them is on.
                record_all_exchanges=bool(self.config.getoption("httpchain_output_dir")),
                redaction=self.config.stash[_REDACTION],
            )
        except Exception as e:
            raise pytest.Collector.CollectError(f"Cannot build test class for {self.path}: {e}") from None
        dummy_module = types.ModuleType("generated")
        setattr(dummy_module, self.name, CarrierClass)
        # Consumed by _getobj(); pytest.Class resolves the class off it.
        self._generated_module = dummy_module
        json_class = JsonClass.from_parent(
            self,
            path=self.path,
            name=self.name,
        )

        markers = []
        for mark_str in scenario.marks:
            try:
                markers.append(make_marker(mark_str))
            except Exception as e:
                raise pytest.Collector.CollectError(f"Invalid marker '{mark_str}' in {self.path}: {e}") from None

        # Keeps the scenario's stages on one worker under --dist loadgroup. A
        # group the scenario declares does that too, as every stage inherits it,
        # and is left alone: xdist joins every group on a test into one name, so
        # adding this one would give each scenario sharing it a group of its own.
        # xdist reads a group back as the text after the node id's last '@', and
        # not at all when a ']' follows that '@', so neither is kept in the name
        # (the validator rejects a declared name xdist cannot read, HTTPCHAIN033).
        # Guarded: without xdist the marker fails --strict-markers.
        if self.config.pluginmanager.hasplugin("xdist") and not xdist_group_names(markers):
            json_class.add_marker(pytest.mark.xdist_group(name=self.nodeid.replace("@", "_").replace("]", "_")))

        for marker in markers:
            json_class.add_marker(marker)

        yield json_class


def _dist_mode(config: pytest.Config) -> str:
    """The pytest-xdist ``--dist`` mode of this run ("no" without xdist).

    Inside a worker the real mode is only visible via workerinput, seeded by
    `pytest_configure_node`.
    """
    workerinput = getattr(config, "workerinput", None)
    if workerinput is not None:
        return workerinput.get("httpchain_dist", "no")
    return config.getoption("dist", default="no")


# Collection order, recorded before any sorter runs: the regroup tiebreaker.
# Keyed by id(item), not the item: up to pytest 9.1 an item hashes its `_nodeid`,
# which an xdist loadgroup worker rewrites between recording and lookup.
_ORIGINAL_POSITIONS: pytest.StashKey[dict[int, int]] = pytest.StashKey()


def _carrier_class(item: pytest.Item) -> type[Carrier] | None:
    """The generated scenario class ``item`` belongs to, or None for any other test."""
    cls = getattr(item, "cls", None)
    return cls if isinstance(cls, type) and issubclass(cls, Carrier) else None


def _stage_index(item: pytest.Item) -> int:
    """The stage position `factory.create_test_class` stamped on the item's method."""
    return getattr(getattr(item, "function", None), "_httpchain_stage_index", 0)


# Which of its scenario's chains an item runs in: the param index of each fixture
# that splits the scenario into chains (see `_chain_args`), by name. Read at setup
# by `Carrier.begin_chain`.
type _ChainKey = tuple[tuple[str, int], ...]
_CHAIN_KEY: pytest.StashKey[_ChainKey] = pytest.StashKey()

# `_chain_args` over each scenario class's every stage, recorded by `JsonClass`
# before any selection narrows them: which fixtures every stage requests is a
# property of the scenario, so dropping the stages without one must not split
# the rest.
_SCENARIO_CHAIN_ARGS: pytest.StashKey[dict[type[Carrier], frozenset[str]]] = pytest.StashKey()


def _high_scoped_params(item: pytest.Item) -> dict[str, int]:
    """The param index of each parametrized fixture above function scope in the
    item's closure, read the way pytest's own param-major reordering reads them
    (``_arg2scope`` is private, but it is what pytest keys that reordering by).
    Stage ``parametrize`` is function-scoped, so it never appears here."""
    callspec = getattr(item, "callspec", None)
    if callspec is None:
        return {}
    return {name: index for name, index in callspec.indices.items() if callspec._arg2scope[name].value != "function"}


def _chain_args(items: Iterable[pytest.Item]) -> dict[type[Carrier], frozenset[str]]:
    """Per scenario class, the fixtures that split it into chains.

    A class-, module-, package- or session-scoped fixture with params that every
    stage requests (a scenario-level fixture, or one such a fixture depends on)
    runs the whole scenario once per param, so each param combination is a
    chain of its own. One only some stages request cannot: the stages without
    it belong to no single param, so it is left to vary in place, like stage
    ``parametrize``.
    """
    shared: dict[type[Carrier], frozenset[str]] = {}
    for item in items:
        if (cls := _carrier_class(item)) is not None:
            names = frozenset(_high_scoped_params(item))
            shared[cls] = shared[cls] & names if cls in shared else names
    return shared


def _warn_on_params_varying_across_stages(cls: type[Carrier], items: list[pytest.Item], chain_args: frozenset[str]) -> None:
    """Warn when a fixture parametrized above function scope that not every
    stage requests is requested by two or more, and so varies in place across
    them (`_chain_args`).

    Stage order then runs each of those stages for every param before the next
    one: with ``tenant`` over [a, b] on ``create`` and ``read`` only,
    create[a], create[b], read[a], read[b]. read[a] sees what create[b] saved,
    and the fixture is set up again each time the param changes, once per
    stage and param rather than once per param. Running those stages param by
    param instead would need the stages without the fixture to run once per
    param too, which they have no param for, so this warns rather than
    reorders: most likely every stage was meant to request the fixture.
    """
    stages: dict[str, set[int]] = {}
    params: dict[str, set[int]] = {}
    scopes: dict[str, str] = {}
    for item in items:
        if (callspec := getattr(item, "callspec", None)) is None:
            continue
        for name, index in _high_scoped_params(item).items():
            if name not in chain_args:
                stages.setdefault(name, set()).add(_stage_index(item))
                params.setdefault(name, set()).add(index)
                scopes[name] = callspec._arg2scope[name].value
    scenario = cls.scenario
    for name, indices in stages.items():
        if scenario is None or len(indices) < 2 or len(params[name]) < 2:
            continue
        names = [scenario.stages[j].name for j in sorted(indices) if j < len(scenario.stages)]
        warnings.warn(
            ScenarioValidationWarning(
                f"Scenario '{cls.__name__}': the {scopes[name]}-scoped fixture '{name}' has params and is requested by stages {names} "
                f"but not by every stage, so the scenario does not run once per param: each of those stages runs for every param "
                f"before the next one does, '{names[1]}' for the first param sees what '{names[0]}' saved for the last, and '{name}' "
                f"is set up again each time its param changes. Request '{name}' from every stage (e.g. in the scenario's fixtures) "
                f"to run the whole chain once per param"
            ),
            stacklevel=2,
        )


def _reject_ids_splitting_loadscope(items: list[pytest.Item]) -> None:
    """Fail collection when a ``::`` in a stage's test id would make
    ``--dist loadscope`` run that stage apart from its scenario.

    loadscope schedules a test by its node id up to the last ``::``, which for
    a stage is meant to be the scenario class. A stage name cannot add one
    (HTTPCHAIN032), but the id in brackets can: a stage's parametrize id, from
    ``ids`` or from a value such as ``"::1"``, or a fixture param's, from the
    fixture's ``ids`` or ``params``. The stage would then run on any worker,
    without the saves of the stages before it. pytest itself handles such an
    id, so only this mode rejects it, and, as for the modes `JsonModule`
    rejects, only in a scenario with a chain to split.
    """
    split = [item for item in items if "::" in item.name]
    cls = _carrier_class(split[0]) if split else None
    if cls is None or cls.scenario is None or len(cls.scenario.stages) <= 1:
        return
    raise pytest.Collector.CollectError(
        f"pytest-xdist --dist loadscope would run {[item.name for item in split]} apart from the rest of scenario '{cls.__name__}': "
        f"loadscope groups tests by node id up to the last '::', and these test ids contain '::' "
        f"(from a stage's parametrize step, or from a fixture's params). "
        f"Give that parametrize step or fixture explicit ids without '::', or use --dist loadfile or loadgroup."
    )


class JsonClass(pytest.Class):
    """Collector for a scenario's generated test class.

    It is the one place that sees every stage of the scenario: a selection
    narrows them only afterwards, ``-k``, ``-m``, ``--deselect`` and ``--lf``
    at ``pytest_collection_modifyitems``, and a node id on the command line
    (how an IDE runs one test) when the session matches the class's items to
    it. So the fixtures that split the scenario into chains are recorded here,
    for `_regroup_carrier_items`, and every stage's test id is checked here.
    """

    def collect(self) -> Iterable[pytest.Item | pytest.Collector]:
        collected = list(super().collect())
        items = [node for node in collected if isinstance(node, pytest.Item)]
        if _dist_mode(self.config) == "loadscope":
            _reject_ids_splitting_loadscope(items)
        chain_args = _chain_args(items)
        self.config.stash.setdefault(_SCENARIO_CHAIN_ARGS, {}).update(chain_args)
        for cls, names in chain_args.items():
            _warn_on_params_varying_across_stages(cls, items, names)
        return collected


def _regroup_carrier_items(
    items: list[pytest.Item],
    original_position: dict[int, int],
    scenario_chain_args: dict[type[Carrier], frozenset[str]] | None = None,
) -> None:
    """Re-sort collected items so each scenario class's stages run contiguously,
    in stage order — per chain, when a parametrized fixture splits the class
    into several (`_chain_args`).

    Leaving a class finalizes its scope, and ``Carrier.teardown_class`` resets
    the chain — so any sorter that interleaves two scenarios breaks both
    (a user-installed pytest-order acting on user-authored ``order(...)`` marks,
    pytest-randomly's shuffle, core's ``--ff``). Each class is pulled together
    at its first item (preserving inter-class order), and within it each chain
    at its first item: pytest has already ordered a high-scoped fixture's params
    so each is set up once, and sorting by stage alone would run every param's
    first stage before any second one. ``original_position`` (keyed by
    ``id(item)``) breaks ties so parametrized instances of a stage keep
    collection order. ``scenario_chain_args`` is `_chain_args` over each
    class's every stage, as `JsonClass` recorded it, so the chains do not
    depend on the selection. Each item's chain is stashed for
    ``Carrier.begin_chain``.
    """
    buckets: dict[type[Carrier], list[pytest.Item]] = {}
    for item in items:
        if (cls := _carrier_class(item)) is not None:
            buckets.setdefault(cls, []).append(item)
    if not buckets:
        return

    # A class's own record covers all its stages; the selected items stand in
    # only for a class collected some other way.
    chain_args = _chain_args(items) | (scenario_chain_args or {})
    for cls, bucket in buckets.items():
        names = chain_args[cls]
        chain_rank: dict[_ChainKey, int] = {}
        sort_keys: dict[int, tuple[int, int, int]] = {}
        for item in bucket:
            params = _high_scoped_params(item)
            chain_key = tuple((name, params[name]) for name in sorted(names))
            item.stash[_CHAIN_KEY] = chain_key
            rank = chain_rank.setdefault(chain_key, len(chain_rank))
            sort_keys[id(item)] = (rank, _stage_index(item), original_position.get(id(item), sys.maxsize))
        bucket.sort(key=lambda item: sort_keys[id(item)])

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


def _apply_xdist_group_nodeids(items: list[pytest.Item]) -> None:
    """Make the ``@<group>`` suffix xdist writes into a scenario item's nodeid
    actually reach the nodeid, so ``--dist loadgroup`` keeps a chain on one worker.

    An xdist worker encodes each item's ``xdist_group`` by assigning the private
    ``item._nodeid``, and the controller's loadgroup scheduler groups by the
    text after ``@``. Up to pytest 9.1 ``_nodeid`` backs ``nodeid``. From pytest
    9.2 (pytest-dev/pytest#14758) ``nodeid`` is derived from a structured
    ``item._id`` instead, so the assignment only adds an unused attribute: the
    suffix is lost, every stage becomes its own work unit, and a scenario's stages
    scatter across workers. This carries the id xdist wrote over to ``_id``.
    Where ``_nodeid`` still backs ``nodeid``, or xdist wrote nothing (another dist
    mode, no group), there is no mismatch and nothing changes.
    """
    for item in items:
        if _carrier_class(item) is None:
            continue
        # From 9.2 `_nodeid` is not a slot, so xdist's write lands in __dict__.
        written = item.__dict__.get("_nodeid")
        node_id = getattr(item, "_id", None)
        if written is None or node_id is None or written == item.nodeid:
            continue
        # parse() caches the exact string, so nodeid reads back verbatim. setattr
        # because `_id` exists only from 9.2: a plain assignment fails the type
        # check on older pytest, and a `ty: ignore` goes unused on newer.
        setattr(item, "_id", type(node_id).parse(written))  # noqa: B010


@pytest.hookimpl(wrapper=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> Any:
    """Enforce chain contiguity after the non-wrapper sorters have run.

    Pre-yield the items are still in collection order, which is recorded as the
    tiebreaker; post-yield runs after pytest-order, pytest-randomly and friends.
    Sorters written as tryfirst wrappers finish even later — `pytest_collection_finish`
    catches those. Post-yield is also after an xdist worker has written its
    loadgroup nodeid suffix, which `_apply_xdist_group_nodeids` needs.
    """
    config.stash[_ORIGINAL_POSITIONS] = {id(item): i for i, item in enumerate(items)}
    result = yield
    _apply_xdist_group_nodeids(items)
    _regroup_carrier_items(items, config.stash[_ORIGINAL_POSITIONS], config.stash.get(_SCENARIO_CHAIN_ARGS, None))
    return result


def _describe_chain(item: pytest.Item, chain_key: _ChainKey) -> str:
    """`` for tenant='alpha'``: the params of a scenario's chain, told apart
    only when a fixture splits it into several; ``item`` is one of the chain's."""
    callspec = getattr(item, "callspec", None)
    if not chain_key or callspec is None:
        return ""
    return " for " + ", ".join(f"{name}={callspec.params[name]!r}" for name, _ in chain_key)


def _warn_on_split_chains(items: list[pytest.Item]) -> None:
    """Warn when selection (``--lf``, ``-k``, ``--deselect``, ``--sw``) dropped
    earlier stages of a chain while later ones remain.

    Reordering and dist-mode scattering are prevented outright, but pytest's
    selection mechanisms silently orphan a chain's tail: the surviving stages
    run without the deselected stages' saved context and fail with misleading
    undefined-variable errors (or worse, run against un-set-up server state).
    Checked per chain, not per scenario: with one chain per param, ``--lf`` can
    keep the head of one param's chain and the tail of another's.
    """
    selected_indices: dict[tuple[type[Carrier], _ChainKey], set[int]] = {}
    first_items: dict[tuple[type[Carrier], _ChainKey], pytest.Item] = {}
    for item in items:
        if (cls := _carrier_class(item)) is not None:
            chain = (cls, item.stash.get(_CHAIN_KEY, ()))
            selected_indices.setdefault(chain, set()).add(_stage_index(item))
            first_items.setdefault(chain, item)

    for (cls, chain_key), indices in selected_indices.items():
        scenario = cls.scenario
        if scenario is None or len(scenario.stages) <= 1:
            continue
        missing = set(range(max(indices))) - indices
        if missing:
            names = [scenario.stages[j].name for j in sorted(missing) if j < len(scenario.stages)]
            chain = _describe_chain(first_items[(cls, chain_key)], chain_key)
            try:
                warnings.warn(
                    ScenarioValidationWarning(
                        f"Scenario '{cls.__name__}': earlier stage(s) {names} were deselected (e.g. by --lf, -k, or --deselect) "
                        f"while later stages of the chain{chain} remain selected; the surviving stages will run without their saved context"
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
    _regroup_carrier_items(session.items, positions, session.config.stash.get(_SCENARIO_CHAIN_ARGS, None))
    _warn_on_split_chains(session.items)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Start a scenario's next chain from fresh state, after the previous
    chain's last report (which may have aborted it). ``tryfirst`` puts this
    ahead of fixture setup: a setup error in a chain's first stage must abort
    that chain, not be reset away when its second stage enters it."""
    if (cls := _carrier_class(item)) is not None:
        cls.begin_chain(item.stash.get(_CHAIN_KEY, ()))


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node) -> None:
    """Pass the real dist mode to xdist workers, which cannot see it themselves
    (xdist resets their own ``dist`` option to "no")."""
    node.workerinput["httpchain_dist"] = node.config.getoption("dist", default="no")


def pytest_addoption(parser: pytest.Parser) -> None:
    ini_options: list[tuple[ConfigOptions, str, Literal["string", "int", "args", "bool"], Any]] = [
        (ConfigOptions.SUFFIX, "File suffix for HTTP test files.", "string", "http"),
        (ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH, "Maximum number of parent directory traversals allowed in $ref paths.", "int", 3),
        (ConfigOptions.MAX_COMPREHENSION_LENGTH, "Maximum length for list/dict comprehensions in template expressions.", "int", 50000),
        (ConfigOptions.MAX_PARALLEL_ITERATIONS, "Maximum number of parallel iterations allowed per stage.", "int", 10000),
        (
            ConfigOptions.REDACT_HEADERS,
            "Headers (case-insensitive, separated by whitespace or commas) whose values report sections and failure messages show as [REDACTED]; empty disables.",
            "args",
            list(DEFAULT_REDACT_HEADERS),
        ),
        (
            ConfigOptions.REDACT_QUERY_PARAMS,
            "Query parameters (case-insensitive, separated by whitespace or commas) whose values URLs in report sections show as [REDACTED]; empty disables.",
            "args",
            list(DEFAULT_REDACT_QUERY_PARAMS),
        ),
        (ConfigOptions.HAR_REDACT, "Apply the httpchain_redact_* rules to HAR files too.", "bool", False),
    ]
    for option, help_text, ini_type, default in ini_options:
        # pytest does not render ini defaults in --help, so repeat them there.
        shown = " ".join(default) if isinstance(default, list) else default
        parser.addini(name=option, help=f"{help_text} Default: {shown}.", type=ini_type, default=default)
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

# The httpchain_redact_* rules, for the report sections and the failure messages
# (through the carrier), and what the HAR export applies: the same rules under
# httpchain_har_redact, none otherwise.
_REDACTION: pytest.StashKey[Redaction] = pytest.StashKey()
_HAR_REDACTION: pytest.StashKey[Redaction] = pytest.StashKey()

# An HTTP field name is an RFC 9110 token.
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def _redaction_names(config: pytest.Config, option: ConfigOptions, *, header_names: bool) -> list[str]:
    """A redaction list's names.

    Split at commas too: shlex alone reads ``Authorization, Cookie`` as
    ``Authorization,`` and ``Cookie``, and a name that can never match would
    leave that credential in the report without a word. For the same reason a
    header entry that is not a header name is a usage error.

    A native ``[tool.pytest]`` table has pytest check a TOML list's items;
    ``[tool.pytest.ini_options]`` hands the list over as written, so a
    non-string item is refused here, in pytest's own words.
    """
    try:
        entries = config.getini(option)
    except (TypeError, ValueError) as e:
        raise pytest.UsageError(f"{option}: {e}") from None
    for i, entry in enumerate(entries):
        if not isinstance(entry, str):
            raise pytest.UsageError(f"{option}: expects a list of strings, but item at index {i} is {type(entry).__name__}: {entry!r}")
    names = [name.strip() for entry in entries for name in entry.split(",") if name.strip()]
    if header_names:
        for name in names:
            if not _HEADER_NAME.fullmatch(name):
                raise pytest.UsageError(f"{option}: {name!r} is not a header name")
    return names


class _HttpxUrlRedaction(logging.Filter):
    """Redacts the URL httpx logs for every request at INFO (``HTTP Request:
    GET <url> ...``), which a run capturing INFO prints with a failure's report.

    Keyed on the argument's type, not on httpx's message text, so a reworded
    message is still covered. Added to the ``httpx`` logger for the session
    only: an in-process pytester run must not leave its rules behind.
    """

    def __init__(self, redaction: Redaction) -> None:
        super().__init__()
        self.redaction = redaction

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and any(isinstance(arg, httpx.URL) for arg in record.args):
            record.args = tuple(self.redaction.url(arg) if isinstance(arg, httpx.URL) else arg for arg in record.args)
        return True


_HTTPX_LOG_FILTER: pytest.StashKey[_HttpxUrlRedaction] = pytest.StashKey()


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

    # The same bare conversion for type="bool" (`maybe`), and in a native TOML
    # table pytest's TypeError for a value that is not a boolean; as for
    # _getint, newer pytest raises the UsageError itself.
    def _getbool(option: ConfigOptions) -> bool:
        try:
            return config.getini(option)
        except (TypeError, ValueError) as e:
            raise pytest.UsageError(f"{option} must be a boolean: {e}") from None

    suffix = str(config.getini(ConfigOptions.SUFFIX))
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", suffix):
        raise pytest.UsageError(f"{ConfigOptions.SUFFIX} must contain only alphanumeric characters, underscores, hyphens, and be ≤32 chars")

    _getint(ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH, minimum=0, minimum_message="must be non-negative")
    max_comprehension_length = _getint(ConfigOptions.MAX_COMPREHENSION_LENGTH, minimum=1, minimum_message="must be a positive integer", maximum=1_000_000)
    _getint(ConfigOptions.MAX_PARALLEL_ITERATIONS, minimum=1, minimum_message="must be a positive integer", maximum=1_000_000)

    redaction = Redaction(
        _redaction_names(config, ConfigOptions.REDACT_HEADERS, header_names=True),
        _redaction_names(config, ConfigOptions.REDACT_QUERY_PARAMS, header_names=False),
    )
    config.stash[_REDACTION] = redaction
    config.stash[_HAR_REDACTION] = redaction if _getbool(ConfigOptions.HAR_REDACT) else NO_REDACTION
    config.stash[_HTTPX_LOG_FILTER] = httpx_log_filter = _HttpxUrlRedaction(redaction)
    logging.getLogger("httpx").addFilter(httpx_log_filter)

    config.stash[_PREVIOUS_MAX_COMPREHENSION_LENGTH] = get_max_comprehension_length()
    set_max_comprehension_length(max_comprehension_length)


def pytest_unconfigure(config: pytest.Config) -> None:
    previous = config.stash.get(_PREVIOUS_MAX_COMPREHENSION_LENGTH, None)
    if previous is not None:
        set_max_comprehension_length(previous)
    httpx_log_filter = config.stash.get(_HTTPX_LOG_FILTER, None)
    if httpx_log_filter is not None:
        logging.getLogger("httpx").removeFilter(httpx_log_filter)


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


def _earlier_hops(history: list[httpx.Response]) -> str:
    """`` (after ...)`` for the report of a response others came before, which
    httpx keeps in one history: the redirects followed, and the responses an
    auth flow answered by sending the request again (a digest challenge's
    401), which are not redirects."""
    redirects = sum(1 for hop in history if hop.is_redirect)
    counts = ((redirects, "redirect"), (len(history) - redirects, "auth exchange"))
    hops = [f"{count} {noun}{'s' if count != 1 else ''}" for count, noun in counts if count]
    return f" (after {' and '.join(hops)})" if hops else ""


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
        # the failing one. Likewise a retried stage shows its last attempt,
        # the others being in the HAR output.
        details: list[str] = []
        if carrier_class.last_iterations_attempted > 1:
            if carrier_class.last_shown_exchange_is_failed:
                shown = "failing"
            elif report.failed:
                shown = "last completed"
            else:
                shown = "last"
            details.append(f"{shown} of {carrier_class.last_iterations_attempted} parallel iterations")
        if (attempt := carrier_class.last_shown_attempt) is not None:
            details.append(f"attempt {attempt[0]} of {attempt[1]}")
        suffix = f" ({', '.join(details)})" if details else ""

        # The shown request is the final hop's, which may differ from what
        # the stage authored; the full chain is in the HAR output.
        if carrier_class.last_response is not None:
            suffix += _earlier_hops(carrier_class.last_response.history)

        if _sections_will_be_shown(item.config, report):
            redaction = item.config.stash[_REDACTION]
            if (request := carrier_class.last_request) is not None:
                report.sections.append((f"HTTP Request{suffix}", _format_section("request", functools.partial(format_request, redaction=redaction), request)))
                report.sections.append((f"HTTP Request (curl){suffix}", _format_section("curl command", functools.partial(format_curl, redaction=redaction), request)))
            if (response := carrier_class.last_response) is not None:
                report.sections.append((f"HTTP Response{suffix}", _format_section("response", functools.partial(format_response, redaction=redaction), response)))

        output_dir = item.config.getoption("httpchain_output_dir")
        if output_dir and carrier_class.last_exchanges:
            try:
                har_path = write_har_file(
                    output_dir=Path(output_dir),
                    test_name=item.nodeid,
                    exchanges=carrier_class.last_exchanges,
                    redaction=item.config.stash[_HAR_REDACTION],
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
