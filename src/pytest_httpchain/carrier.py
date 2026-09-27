"""Runtime execution engine: the base class of every generated scenario test class.

``factory.create_test_class`` builds one ``test NN - <stage name>`` method per
stage on a fresh `Carrier` subclass, which the plugin's collection hooks keep
contiguous and in stage order; the per-scenario state lives at the *class*
level, so stage methods share one running context across the chain while
scenarios stay isolated from each other.

This module owns sequencing only. What the individual pieces mean lives next
door: ``request_builder`` (models -> httpx arguments), ``response_steps`` (one
verify/save step), ``scoping`` (context layering).
"""

import inspect
import json
import logging
import math
import threading
import time
import warnings
from collections import ChainMap
from collections.abc import Callable, Hashable, Iterable, Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import AbstractContextManager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import httpx
import pytest
from pydantic import BaseModel, RootModel, ValidationError
from pyrate_limiter import Duration, Limiter, Rate

from pytest_httpchain.errors import RequestError, SaveError, StageExecutionError, VerificationError
from pytest_httpchain.har_writer import Exchange
from pytest_httpchain.models import (
    ClientConfig,
    CombinationsParameter,
    IndividualParameter,
    JsonBody,
    ParallelConfig,
    ParallelForeachConfig,
    ParallelRepeatConfig,
    SaveStep,
    Scenario,
    Stage,
    SubstitutionsSave,
    VerifyStep,
    validate_rendered_scenario_auth,
)
from pytest_httpchain.redaction import DEFAULT_REDACTION, Redaction
from pytest_httpchain.request_builder import build_client_kwargs, build_request_kwargs
from pytest_httpchain.response_steps import process_save, process_verify
from pytest_httpchain.scoping import (
    RESPONSE_META_NAME,
    base_global_context,
    iteration_context,
    response_step_context,
    stage_start_context,
    with_saves,
    with_stage_substitutions,
)
from pytest_httpchain.templates import TemplatesError, contains_template, walk
from pytest_httpchain.utils import process_substitutions
from pytest_httpchain.warnings import ScenarioValidationWarning

logger = logging.getLogger(__name__)

# An expected stage failure (bad scenario, failed verification, unreachable
# server) rather than a plugin bug: both execution paths turn these into a clean
# pytest failure instead of a traceback.
_STAGE_FAILURE_EXCEPTIONS = (StageExecutionError, TemplatesError, ValidationError)

# Every way a stage (or one of its iterations) ends that the stage reports
# itself: an expected failure, or what pytest.skip(), pytest.xfail() and
# pytest.fail() raise from a user function (XFailed is a Failed). Anything else
# is an interrupt or a plugin bug, and propagates as it is.
_STAGE_OUTCOMES = (*_STAGE_FAILURE_EXCEPTIONS, pytest.skip.Exception, pytest.fail.Exception)

# Makes _ensure_initialized's check-then-act atomic under thread-based runners.
_INIT_LOCK = threading.Lock()

# A context manager a factory fixture returned, entered by a template's call
# (`Carrier._wrap_factory_fixture`), with the fixture's name for an exit error.
_EnteredContextManager = tuple[str, AbstractContextManager]


class _IterationContextManagers:
    """The context managers an iteration entered, until it exits them.

    The iteration closes it to exit them (`Carrier._run_iteration`), and it
    takes no more then: a thread the iteration started in a copy of its
    context (``asyncio.to_thread``, or any thread on a free-threaded build,
    which inherits the context by default) can still reach it afterwards,
    and what it enters then goes on the stage's list instead, as it does from
    a thread started with an empty context, rather than on a list no one will
    exit again.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entered: list[_EnteredContextManager] | None = []

    def add(self, entry: _EnteredContextManager) -> bool:
        """Register ``entry``, unless the iteration has already exited its own."""
        with self._lock:
            if self._entered is None:
                return False
            self._entered.append(entry)
            return True

    def close(self) -> list[_EnteredContextManager]:
        """What the iteration entered, to exit; it takes no more from now on."""
        with self._lock:
            entered, self._entered = self._entered or [], None
        return entered


# The running iteration's entered context managers, set in the thread running
# it (`Carrier._run_iteration`). Outside an iteration it is unset, and the
# stage's `Carrier.active_context_managers` collects them.
_ITERATION_CONTEXT_MANAGERS: ContextVar[_IterationContextManagers | None] = ContextVar("iteration_context_managers", default=None)

# Raised by an exit, these stop the run: re-raised once every context manager
# is exited. pytest.exit()'s is an Exception, so an `except Exception` must
# let these through first.
_INTERRUPTS = (KeyboardInterrupt, SystemExit, pytest.exit.Exception)


def _exit_context_managers(entered: list[_EnteredContextManager], errors: Iterable[str] = ()) -> list[str]:
    """Exit ``entered``, last entered first, and describe each exit that raised,
    after ``errors``: those of exits done before, reported with these (a
    parallel stage's iterations', before the stage's own).

    Whatever one raises, the rest are still exited, as pytest does for fixture
    finalizers: an exception, ``pytest.fail()`` included, becomes a message,
    and an interrupt is re-raised once all are exited (the messages logged,
    ``errors`` included, since nothing else will report them). Like a yield
    fixture's teardown, an exit is not told whether the stage failed.
    """
    errors = list(errors)
    interrupt: BaseException | None = None
    while entered:
        name, context_manager = entered.pop()
        try:
            context_manager.__exit__(None, None, None)
        except _INTERRUPTS as e:
            if interrupt is None:
                interrupt = e
        except BaseException as e:
            errors.append(f"Exiting the context manager from fixture '{name}' failed: {_describe_exception(e)}")
    if interrupt is not None:
        _log_exit_errors(errors)
        raise interrupt
    return errors


def _describe_exception(e: BaseException) -> str:
    """``Type: message`` for an exception user code raised, whose ``__str__``
    may raise in turn: formatting it must not abandon the exits still to come."""
    try:
        return f"{type(e).__name__}: {e}"
    except Exception:
        return f"{type(e).__name__}: <exception str() failed>"


def _log_exit_errors(errors: list[str]) -> None:
    """Log exit errors that no stage failure will carry: the stage is ending in
    an interrupt or a plugin bug, which is what gets reported."""
    for error in errors:
        logger.error("%s", error)


def _failure_lines(outcome: BaseException | None, exit_errors: list[str]) -> list[str]:
    """What a stage or iteration that ended in ``outcome`` (None: it succeeded)
    and whose context managers then raised on exit reports, one entry per line.

    Its own failure stays first, the likelier cause. A skip or an xfail is not
    a failure, so the exit errors are all there is to report.
    """
    if outcome is None or isinstance(outcome, (pytest.skip.Exception, pytest.xfail.Exception)):
        return list(exit_errors)
    return [str(outcome), *exit_errors]


def _exit_failure(outcome: BaseException | None, exit_errors: list[str]) -> str:
    """The failure message `_failure_lines` describes."""
    return "\n".join(_failure_lines(outcome, exit_errors))


def _response_meta(response: httpx.Response) -> SimpleNamespace:
    """The ``response`` namespace response-step templates see: metadata only,
    since ``save`` is what extracts body data."""
    try:
        elapsed_ms = response.elapsed.total_seconds() * 1000
    except RuntimeError:
        elapsed_ms = None
    return SimpleNamespace(
        status=response.status_code,
        reason=response.reason_phrase,
        headers=response.headers,
        elapsed_ms=elapsed_ms,
    )


def _error_request(e: Exception) -> httpx.Request | None:
    """The request an httpx error was raised for, if it recorded one.

    ``HTTPError.request`` raises RuntimeError when unset, so this cannot be a
    getattr with a default.
    """
    try:
        request = e.request  # ty: ignore[unresolved-attribute]
    except (AttributeError, RuntimeError):
        return None
    return request if isinstance(request, httpx.Request) else None


def _request_error(what: str, e: Exception, redaction: Redaction) -> RequestError:
    """``e`` as the stage failure it is, its text shown through ``redaction``:
    the message prints above the request's report section, whose header values
    it may quote (h11 refusing ``b'Bearer <token>\\n'``)."""
    request = _error_request(e)
    text = str(e) if request is None else redaction.error_text(str(e), request.headers)
    return RequestError(f"{what}: {text}", request=request)


def _parallel_number(field: str, value: Any) -> float:
    """A walk()-resolved ``parallel`` setting as a float, or a stage failure.

    The numeric fields are `PositiveInt | NumberOrTemplate` and walk() re-validates
    the config it resolves, but NumberOrTemplate accepts any complete template:
    a template resolving to another template string satisfies it and arrives here
    as text. A bare ``float()``/``int()`` would raise ValueError, which is not a
    stage failure and so escapes as a plugin traceback.
    """
    if isinstance(value, bool):
        # float(True) is 1.0, so without this a resolved bool would silently
        # configure one worker / one call per second.
        raise StageExecutionError(f"parallel.{field} must be a positive number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError, ArithmeticError):
        raise StageExecutionError(f"parallel.{field} must be a positive number, got {value!r}") from None
    if not math.isfinite(number) or number <= 0:
        raise StageExecutionError(f"parallel.{field} must be a positive number, got {value!r}")
    return number


def _parallel_int(field: str, value: Any) -> int:
    """`_parallel_number` for a whole-number setting. Truncating instead would
    turn a resolved 0.5 into 0 — no workers, or a silently disabled limiter.

    An int is taken as it stands: `PositiveInt` has no upper bound, and routing
    one through ``float()`` would raise OverflowError on a value the callers
    handle perfectly well (``max_concurrency`` clamps against the iteration
    count, and pyrate_limiter accepts any int rate).
    """
    if isinstance(value, int) and not isinstance(value, bool):
        if value <= 0:
            raise StageExecutionError(f"parallel.{field} must be a positive whole number, got {value!r}")
        return value
    number = _parallel_number(field, value)
    if not number.is_integer():
        raise StageExecutionError(f"parallel.{field} must be a positive whole number, got {value!r}")
    return int(number)


def _parallel_values(field: str, value: Any) -> list[Any]:
    """A walk()-resolved ``parallel.foreach`` step's values, or a stage failure.

    Like the numeric fields, both step kinds also accept a template string, so a
    template resolving to another template arrives here as text: iterated, it
    ran one iteration per character (``individual``) or escaped as a bare
    TypeError (``combinations``).
    """
    if not isinstance(value, list):
        raise StageExecutionError(f"parallel.foreach {field} must resolve to a list, got {value!r}")
    return value


def _none_is_a_value(model: BaseModel, field: str) -> bool:
    """The fields exempt from `_render_declared`: None there is something the
    scenario can mean, not a setting that vanished — a JSON body of ``null``
    (``request_builder`` sends it as such) and free-text descriptions."""
    return field == "description" or (isinstance(model, JsonBody) and field == "json")


# Where a value sits in a dumped model: field names and dict keys, list indices.
type _Keys = tuple[str | int, ...]


def _rendered_away(declared: Any, substituted: Any, keys: _Keys = ()) -> Iterator[tuple[_Keys, str]]:
    """``(keys, template)`` for each model field that was declared but rendered
    to None, ``keys`` locating it in ``substituted``.

    ``substituted`` is ``declared`` dumped and substituted but not yet
    validated, so it is read alongside the declared models, which say what is a
    model field and what a dict value; dicts and lists are followed to the
    models inside them (``verify.headers`` holds its matchers in a dict). Only
    model fields count: a dict value or list item that renders to None — a query
    parameter, a user-function kwarg — is a value handed on, not a field left
    undeclared. Only a string can render to None (walk() maps containers
    element-wise and dumps other models to dicts first), so what was declared is
    always a template.
    """

    def field(declared_value: Any, substituted_value: Any, field_keys: _Keys) -> Iterator[tuple[_Keys, str]]:
        if isinstance(declared_value, RootModel):
            # Dumped as its bare root value, not as {"root": ...}: the root is the
            # field, at the model's own place (a user-function name).
            declared_value = declared_value.root
        if declared_value is not None and substituted_value is None:
            yield field_keys, declared_value
        else:
            yield from _rendered_away(declared_value, substituted_value, field_keys)

    match declared, substituted:
        case RootModel(), _:
            yield from field(declared, substituted, keys)
        case BaseModel(), dict():
            # Only the declared fields were dumped (`_render_declared`).
            for name in type(declared).model_fields:
                if name in substituted and not _none_is_a_value(declared, name):
                    yield from field(getattr(declared, name), substituted[name], (*keys, name))
        case dict(), dict():
            for key, declared_value in declared.items():
                if key in substituted:
                    yield from _rendered_away(declared_value, substituted[key], (*keys, key))
        case list() | tuple(), list() | tuple():
            # Substitution rewrites a sequence element-wise, so the lengths match.
            for i, (declared_value, substituted_value) in enumerate(zip(declared, substituted, strict=True)):
                yield from _rendered_away(declared_value, substituted_value, (*keys, i))


def _rendered_whole_away(declared: Any, rendered: Any, keys: _Keys = ()) -> Iterator[tuple[_Keys, str]]:
    """`_rendered_away` for a model that one template rendered whole: a header
    matcher written as ``"{{ {'contains': ct, 'not_contains': 'text/html'} }}"``,
    or saved from the response and used as ``"{{ matcher }}"``.

    Declared as a single string, such a model's fields are known only once
    validation has built it, so ``rendered`` is the validated model. A field the
    rendered mapping set explicitly (``model_fields_set``) to None is refused; one
    it left out was never declared.
    """
    match declared, rendered:
        case BaseModel(), BaseModel() if type(declared) is type(rendered):
            for name in type(declared).model_fields:
                yield from _rendered_whole_away(getattr(declared, name), getattr(rendered, name), (*keys, name))
        case (str() as template, BaseModel()) | (RootModel(root=str() as template), BaseModel()):
            for name in type(rendered).model_fields:
                if name in rendered.model_fields_set and getattr(rendered, name) is None and not _none_is_a_value(rendered, name):
                    yield (*keys, name), template
        case dict(), dict():
            for key, declared_value in declared.items():
                if key in rendered:
                    yield from _rendered_whole_away(declared_value, rendered[key], (*keys, key))
        case list() | tuple(), list() | tuple():
            for i, (declared_value, rendered_value) in enumerate(zip(declared, rendered, strict=True)):
                yield from _rendered_whole_away(declared_value, rendered_value, (*keys, i))


def _replaced(structure: Any, keys: _Keys, value: Any) -> Any:
    """``structure`` with ``value`` at ``keys``, copied along the way."""
    if not keys:
        return value
    key, rest = keys[0], keys[1:]
    if isinstance(structure, dict):
        return {**structure, key: _replaced(structure[key], rest, value)}
    return type(structure)(_replaced(item, rest, value) if i == key else item for i, item in enumerate(structure))


def _render_declared[M: BaseModel](
    declared: M, context: Mapping[str, Any], where: str, error: type[StageExecutionError] = StageExecutionError, validate: Callable[[Any], M] | None = None
) -> M:
    """``walk()`` a declared scenario model, refusing any field a template
    rendered to None.

    walk() re-validates what it renders, but an optional field accepts None: a
    template that rendered away — a JMESPath save of a missing key, ``get()``
    without a default — validates cleanly, and the consumer then treats the field
    as never declared. A header matcher or ``verify.status`` goes unchecked,
    ``calls_per_sec`` stops limiting, ``ssl.cert`` or ``auth`` is not applied,
    and the stage is green. "Never declared" and "declared but rendered to
    nothing" must never look the same, so every model the carrier renders goes
    through here, and a new optional field is covered without being listed:
    exempting one (`_none_is_a_value`) is the explicit choice. ``where`` names
    the model in the message; ``error`` is the failure type of the step. A model
    one template rendered whole (a header matcher written as ``"{{ matcher }}"``)
    has no declared fields to compare, so it is checked once validated
    (`_rendered_whole_away`).

    ``validate`` builds the rendered model, by default as the declared one's
    type. A model rendered on its own that was declared as one member of a
    union (a scenario's auth: the template string a user function's name
    takes can render a built-in scheme) passes the union's instead.

    The substituted form is checked before validation judges it. Where the None
    is also invalid — a matcher's only field, a required field such as ``url`` —
    pydantic's report names neither the template nor the None (a matcher's even
    asks for a field the scenario did set), so the same message is raised
    instead, without the claim that the None would have disabled anything. Which
    errors the Nones account for is settled by putting the declared templates
    back, which validated in those places before: whatever still fails is
    reported as pydantic has it, under the refusal.
    """
    # walk()'s own model step (hands back a model with no template untouched;
    # otherwise dump, substitute, re-validate), taken apart at the re-validation.
    # Only the declared fields are dumped, so the rendered model keeps the
    # declared one's `model_fields_set`: a request's timeout and redirect
    # setting count only where declared (`request_builder`), and a full dump
    # made every default look declared.
    if not contains_template(declared):
        return declared
    validate = validate or type(declared).model_validate
    substituted = walk(declared.model_dump(mode="python", exclude_unset=True), context)
    vanished = list(_rendered_away(declared, substituted))
    try:
        rendered = validate(substituted)
    except ValidationError as e:
        if not vanished:
            raise
        refusal = _rendered_to_none(where, *vanished[0])
        restored = substituted
        for keys, template in vanished:
            restored = _replaced(restored, keys, template)
        try:
            validate(restored)
        except ValidationError as other:
            raise error(f"{refusal}\n{other}") from e
        raise error(refusal) from e
    vanished = vanished or list(_rendered_whole_away(declared, rendered))
    if vanished:
        raise error(f"{_rendered_to_none(where, *vanished[0])}, which would silently disable it")
    return rendered


def _rendered_to_none(where: str, keys: _Keys, template: str) -> str:
    path = where + "".join(f"[{key}]" if isinstance(key, int) else f".{key}" for key in keys)
    return f"'{path}' was declared as {template!r} but rendered to None"


def fresh_chain_state() -> dict[str, Any]:
    """The mutable class state one chain of a scenario owns, in its pristine form.

    A scenario runs as one chain, or as one per param of a parametrized fixture
    (`Carrier.begin_chain`), and each chain starts from this: no abort, no
    exchanges, no client yet. The saves live in ``global_context``, reset
    alongside it. What a scenario resolves once for all its chains is in
    `fresh_scenario_state`.
    """
    return {
        "client": None,
        "aborted": False,
        "last_request": None,
        "last_response": None,
        "last_exchanges": [],
        "last_iterations_attempted": 0,
        "last_shown_exchange_is_failed": False,
        "active_context_managers": [],
    }


def fresh_scenario_state() -> dict[str, Any]:
    """The per-scenario mutable class state, in its pristine form.

    Single source of truth for the state a scenario must own rather than share:
    ``factory.create_test_class`` seeds every subclass with it and
    `Carrier.teardown_class` re-applies it. It is the chain state plus the
    scenario's initialization, which its chains share. New per-scenario state
    goes here, or in `fresh_chain_state` when each chain must start without it.
    """
    return {
        **fresh_chain_state(),
        "_initialized": False,
        "_init_failed": None,
        "_client_kwargs": None,
        "_client_config": None,
        "_chain_key": None,
    }


@dataclass(slots=True, frozen=True)
class IterationResult:
    """A successful stage iteration. ``started`` is when the request went on the
    wire, which is what HAR waterfalls are built from."""

    saved_context: dict[str, Any]
    request: httpx.Request
    response: httpx.Response
    started: datetime


class _PoolCancel(threading.Event):
    """A parallel stage's cancellation, set as soon as one of its iterations
    ends in anything but success (`Carrier._run_iteration`), which `claim`
    records: the first iteration to end so is the stage's outcome, and every
    other one's is secondary (`_fold_in_secondary`), whichever order the
    stage's thread reads them in.

    The stage's thread learns of an iteration's end only once its future
    completes, after the iteration's exits, and an exit can take a while (a
    rollback): meanwhile another iteration, which the cancellation stopped or
    which was still running, can end and be read first. Taking the first
    outcome read as the stage's reported that one instead, a skip or an xfail
    included, and dropped the failure that ended the stage.
    """

    def __init__(self) -> None:
        super().__init__()
        self._claim_lock = threading.Lock()
        self._claimed_by: int | None = None

    def claim(self, idx: int) -> bool:
        """Cancel the pool for iteration ``idx``, which did not succeed, and
        tell whether it is the first to have: the one whose outcome is the stage's."""
        with self._claim_lock:
            if self._claimed_by is None:
                self._claimed_by = idx
            first = self._claimed_by == idx
        self.set()
        return first


class _IterationExitError(StageExecutionError):
    """An iteration whose context managers raised on exit (`Carrier._run_iteration`).

    It fails the iteration. ``own_failure`` is how the iteration itself ended
    if not in success, and ``failures`` puts a failure first (`_failure_lines`).
    For an iteration still running when another one ended the stage, or one
    cancelled by it, `_fold_in_secondary` reports ``exit_errors`` alone,
    and folds ``result`` (the iteration's own success, if it had one) in with
    the others: its request went on the wire. The iteration's exchange, its
    success's or its own failure's, is carried as the failure's, for the
    report and the HAR to show when the iteration fails the stage.
    """

    def __init__(self, exit_errors: list[str], own_failure: BaseException | None = None, result: IterationResult | None = None):
        exchange = result if result is not None else own_failure if isinstance(own_failure, StageExecutionError) else None
        self.failures = _failure_lines(own_failure, exit_errors)
        super().__init__(
            "\n".join(self.failures),
            request=exchange.request if exchange is not None else None,
            response=exchange.response if exchange is not None else None,
            started=exchange.started if exchange is not None else None,
        )
        self.exit_errors = exit_errors
        self.own_failure = own_failure
        self.result = result


def _parallel_failure(idx: int, exc: Exception) -> str:
    """How a parallel stage reports the iteration ``idx`` that failed it.

    What its context managers raised on exit, below the first line, is
    labelled with the iteration, as a straggler's is (`_fold_in_secondary`):
    the lines left unlabelled are the stage's own.
    """
    first, *rest = exc.failures if isinstance(exc, _IterationExitError) else [str(exc)]
    return "\n".join([f"Parallel execution failed at iteration {idx}: {first}", *(f"Iteration {idx}: {line}" for line in rest)])


def _fold_in_secondary(idx: int, e: BaseException, results: list[IterationResult | None], exit_errors: list[str]) -> None:
    """Take in how iteration ``idx`` of a parallel stage ended when that is
    secondary to the stage's outcome: another iteration ended the stage first
    (`_PoolCancel`), and this one was cancelled by it or still running then.

    Its own failure, a skip, an xfail or a user function's pytest.fail()
    included, is dropped: re-raised, it would turn a failed stage into a
    skipped or xfailed one, or replace its failure message. Not what its
    context managers raised on exit: a commit that failed is a side effect the
    user must hear of, so it goes on ``exit_errors``, labelled with the
    iteration. A success its exit failed is still folded into ``results``: its
    request went on the wire.
    """
    if isinstance(e, _IterationExitError):
        if e.result is not None:
            results[idx] = e.result
        exit_errors.extend(f"Iteration {idx}: {error}" for error in e.exit_errors)


class Carrier:
    """Base class of the generated scenario test classes; runs their stages."""

    # Placeholders only: create_test_class() overrides every one of these per
    # scenario (the mutable ones from `fresh_scenario_state`). This state must
    # stay at class level — the stage methods are classmethods sharing one
    # running context via `cls`.
    scenario: ClassVar[Scenario | None] = None
    scenario_dir: ClassVar[Path | None] = None
    client: ClassVar[httpx.Client | None] = None
    aborted: ClassVar[bool] = False
    last_request: ClassVar[httpx.Request | None] = None
    last_response: ClassVar[httpx.Response | None] = None
    last_exchanges: ClassVar[list[Exchange]] = []
    last_iterations_attempted: ClassVar[int] = 0
    last_shown_exchange_is_failed: ClassVar[bool] = False
    record_all_exchanges: ClassVar[bool] = False
    # The report's rules (httpchain_redact_*), for failure messages that echo a
    # header's value: they are printed next to the report sections.
    redaction: ClassVar[Redaction] = DEFAULT_REDACTION
    global_context: ClassVar[ChainMap[str, Any]] = ChainMap()
    # Entered by the running stage outside its iterations (in `always_run`,
    # `substitutions`, `parallel`): exited at its end. Each iteration exits its own.
    active_context_managers: ClassVar[list[_EnteredContextManager]] = []
    max_parallel_iterations: ClassVar[int] = 10_000
    _initialized: ClassVar[bool] = False
    _init_failed: ClassVar[str | None] = None
    _client_kwargs: ClassVar[dict[str, Any] | None] = None
    # The resolved `client` block, for what each request applies itself (params, the base_url check).
    _client_config: ClassVar[ClientConfig | None] = None
    _chain_key: ClassVar[Hashable | None] = None
    _context_resolved_at_collection: ClassVar[bool] = False

    @classmethod
    def begin_chain(cls, chain_key: Hashable) -> None:
        """Enter the chain the next stage belongs to, ending the previous one.

        A scenario runs as several chains when a fixture it requests is
        parametrized above function scope: one complete pass per param (the
        plugin keys and orders them). Each must start as the first did — no
        saves, no abort, a fresh client — but class teardown provides that only
        once per class, so a change of chain ends the previous one too. The
        scenario's initialization is not repeated: it cannot see the param, and
        its user functions must not run again (`_ensure_initialized`).
        """
        if cls._chain_key is not None and cls._chain_key != chain_key:
            cls._end_chain()
        cls._chain_key = chain_key

    @classmethod
    def _ensure_initialized(cls) -> None:
        """Resolve scenario substitutions, ``ssl``, ``client`` and ``auth``, and
        build the shared client on first use.

        Deferred from collection so ``--collect-only`` and IDE discovery neither
        run user code nor allocate a client per scenario. Runs at most once per
        scenario, success or failure: side-effectful substitutions and auth are
        never re-invoked, and after a failure every later stage skips, in every
        chain. A later chain only gets a client of its own, built from the
        arguments the first one resolved.
        """
        with _INIT_LOCK:
            if cls._init_failed is not None:
                raise StageExecutionError(f"Failed to initialize scenario: {cls._init_failed}")
            if cls._initialized:
                if cls.client is None and cls._client_kwargs is not None:
                    cls.client = httpx.Client(**cls._client_kwargs)
                return
            scenario = cls.scenario
            assert scenario is not None, "create_test_class() seeds cls.scenario"
            try:
                if not cls._context_resolved_at_collection:
                    cls.global_context = base_global_context(process_substitutions(scenario.substitutions))

                resolved_ssl = _render_declared(scenario.ssl, cls.global_context, "ssl")
                resolved_client = _render_declared(scenario.client, cls.global_context, "client")
                resolved_auth = _render_declared(scenario.auth, cls.global_context, "auth", validate=validate_rendered_scenario_auth) if scenario.auth is not None else None
                cls._client_kwargs = build_client_kwargs(resolved_client, resolved_ssl, resolved_auth, cls.scenario_dir, declared_auth=scenario.auth)
                cls._client_config = resolved_client
                cls.client = httpx.Client(**cls._client_kwargs)
            except Exception as e:
                cls._init_failed = str(e)
                raise StageExecutionError(f"Failed to initialize scenario: {e}") from e
            cls._initialized = True

    @classmethod
    def _resolve_always_run(cls, stage: Stage, stage_fixtures: dict[str, Any]) -> bool:
        """Resolve ``always_run``, evaluating a template form against the
        stage-start context (stage substitutions do not exist yet).

        Only a template form still missing its context initializes, and it must:
        the scenario substitutions it is promised (see `scoping`'s scope table)
        exist only once the context is built, and an abort raised before any stage
        body ran — a fixture error in stage one — leaves it empty otherwise. A
        static bool reads no context, and a collection-resolved context is already
        populated; initializing for either would run auth and allocate the client
        for a stage about to skip.
        """
        if isinstance(stage.always_run, bool):
            return stage.always_run
        if not cls._context_resolved_at_collection:
            cls._ensure_initialized()
        try:
            return bool(walk(stage.always_run, stage_start_context(cls.global_context, stage_fixtures)))
        except TemplatesError as e:
            raise StageExecutionError(f"Failed to evaluate always_run template: {e}") from e

    @classmethod
    def execute_stage(cls, stage: Stage, fixture_kwargs: dict[str, Any]) -> None:
        """Execute one stage end to end.

        Gates on the abort/``always_run`` flow, layers the stage context, runs
        the iteration matrix, and on full success commits the collected saves as
        a new global-context layer. A failure is reported via ``pytest.fail``
        and commits no saves, so the context never carries a timing-dependent
        subset. The report hook owns chain-abort classification because only
        pytest's final report knows whether xfail/strict and setup/teardown made
        the item a genuine failure. However the stage ends, the context managers
        its factory fixtures returned are exited before this returns (each
        iteration's by `_run_iteration`), and before the saves are committed:
        one that raises on exit fails the stage, which then commits none.
        """
        # Reset before anything can fail or skip: a stage that never records an
        # exchange must report nothing, not the previous stage's.
        cls.last_request = None
        cls.last_response = None
        cls.last_exchanges = []
        cls.last_iterations_attempted = 0
        cls.last_shown_exchange_is_failed = False

        # Ahead of the always_run machinery: a failed initialization leaves no
        # context and no client, so every stage skips.
        if cls._init_failed is not None:
            pytest.skip(reason=f"Scenario initialization failed: {cls._init_failed}")

        outcome: BaseException | None = None
        saves: dict[str, Any] | None = None
        # What the context managers of a parallel stage's iterations still
        # running when another one ended the stage raised on exit (`_run_iterations`).
        iteration_exit_errors: list[str] = []
        try:
            stage_fixtures = cls._build_stage_fixtures(fixture_kwargs)

            if cls.aborted and not cls._resolve_always_run(stage, stage_fixtures):
                pytest.skip(reason="Flow aborted")

            cls._ensure_initialized()

            stage_context = stage_start_context(cls.global_context, stage_fixtures)
            stage_substitutions = process_substitutions(stage.substitutions, stage_context)
            local_context = with_stage_substitutions(stage_context, stage_substitutions)

            # Guarded: context dumps carry every saved value (auth tokens
            # included) and pytest attaches captured logs to failure reports.
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("global context on start: %s", _context_dump(cls.global_context))
                logger.debug("local context on start: %s", _context_dump(local_context))

            parallel_config: ParallelConfig | None = _render_declared(stage.parallel, local_context, "parallel") if stage.parallel else None
            iteration_substitutions = cls._build_iteration_substitutions(parallel_config, cls.max_parallel_iterations)

            total = len(iteration_substitutions)
            if total == 0:
                # The models reject the static empty cases, but a template- or
                # $ref-sourced config can still resolve to empty at runtime.
                raise StageExecutionError("Parallel configuration produced zero iterations; foreach/repeat must yield at least one item")

            results, first_error = cls._run_iterations(stage, local_context, iteration_substitutions, parallel_config, iteration_exit_errors)
            completed = [iter_result for iter_result in results if iter_result is not None]

            if first_error is None:
                cls._record_exchanges(completed, failed=None, attempted=total)
                saves = {}
                for iter_result in completed:
                    saves.update(iter_result.saved_context)
            else:
                idx, exc = first_error
                cls._record_exchanges(completed, failed=exc, attempted=total)
                # Label the failure as parallel only when the user asked for
                # parallel, else a plain stage failure would be misreported.
                if parallel_config is not None:
                    raise StageExecutionError(_parallel_failure(idx, exc)) from exc
                raise exc

        except _STAGE_OUTCOMES as e:
            # Held back, a skip or an xfail too: exiting the context managers may fail the stage.
            outcome = e
        except BaseException:
            # An interrupt or a plugin bug is what gets reported; the context
            # managers are still exited.
            _log_exit_errors(_exit_context_managers(cls.active_context_managers, iteration_exit_errors))
            raise

        # At the stage's end, not the class's: a factory fixture's context
        # manager is typically built on other fixtures (a transaction on a
        # connection), which pytest tears down as soon as this stage returns.
        # The iterations' exit errors go in, so that an interrupt raised by one
        # of these exits logs them rather than dropping them.
        exit_errors = _exit_context_managers(cls.active_context_managers, iteration_exit_errors)

        # Deliberately outside the handler: raising there would set
        # `Failed.__context__` to the original exception, and pytest's
        # repr_excinfo walks the whole __cause__/__context__ chain even under
        # pytrace=False — printing the one message 2-4 times, since plugin
        # errors and httpx transport errors are themselves chained.
        if exit_errors:
            pytest.fail(reason=_exit_failure(outcome, exit_errors), pytrace=False)
        if isinstance(outcome, _STAGE_FAILURE_EXCEPTIONS):
            pytest.fail(reason=str(outcome), pytrace=False)
        if outcome is not None:
            # What a user function's pytest.skip/xfail/fail raised, unchanged.
            raise outcome

        # Only now that the context managers have exited cleanly: a stage their
        # exit fails commits no saves, like any failed stage.
        assert saves is not None, "a stage that did not fail collected its saves"
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("updates for global context: %s", _context_dump(saves))
        cls.global_context = with_saves(cls.global_context, saves)

    @classmethod
    def _build_stage_fixtures(cls, fixture_kwargs: dict[str, Any]) -> dict[str, Any]:
        """Wrap callable (factory) fixtures so templates can invoke them; plain
        values pass through."""
        return {name: cls._wrap_factory_fixture(name, value) if callable(value) and not inspect.isclass(value) else value for name, value in fixture_kwargs.items()}

    @staticmethod
    def _build_iteration_substitutions(parallel_config: ParallelConfig | None, max_parallel_iterations: int) -> list[dict[str, Any]]:
        """Expand a resolved parallel config into per-iteration substitutions:
        no config -> one empty dict; ``repeat`` -> N empties; ``foreach`` -> the
        cross-product of its steps.

        The cap is checked before anything is materialized, since a runaway
        template-driven count would otherwise OOM while building the list the cap
        was about to reject. The cross-product order is the *reverse* of pytest's
        stacked ``parametrize``.
        """

        def check_cap(count: int) -> None:
            if count > max_parallel_iterations:
                raise StageExecutionError(
                    f"Parallel iteration count ({count}) exceeds maximum ({max_parallel_iterations}). Set 'httpchain_max_parallel_iterations' in pytest.ini to increase the limit."
                )

        iteration_substitutions: list[dict[str, Any]] = [{}]
        match parallel_config:
            case None:
                pass
            case ParallelRepeatConfig(repeat=repeat_count):
                # int(): the field is `PositiveInt | NumberOrTemplate`, but the
                # config arrives walk()-resolved.
                repeat_total = _parallel_int("repeat", repeat_count)
                check_cap(repeat_total)
                iteration_substitutions = [{} for _ in range(repeat_total)]
            case ParallelForeachConfig(foreach=foreach_steps):
                # (name, values) per step — None for a `combinations` step, whose
                # values are whole dicts. Collecting this shape first keeps the
                # cap check ahead of the expansion.
                steps: list[tuple[str | None, list[Any]]] = []
                for step in foreach_steps:
                    match step:
                        case IndividualParameter(individual=individual):
                            param_name = next(iter(individual))
                            steps.append((param_name, _parallel_values(f"individual '{param_name}'", individual[param_name])))
                        case CombinationsParameter(combinations=combinations):
                            steps.append((None, _parallel_values("combinations", combinations)))
                        case _:
                            raise RuntimeError(f"Unhandled foreach step: {type(step).__name__}")
                check_cap(math.prod(len(values) for _, values in steps))

                for param_name, values in steps:
                    additions = [{param_name: value} if param_name is not None else value for value in values]
                    # Clause order is load-bearing: new values outer, accumulated
                    # dicts inner. Swapping them changes the iteration order.
                    iteration_substitutions = [{**existing, **addition} for addition in additions for existing in iteration_substitutions]
            case _:
                raise RuntimeError(f"Unhandled parallel config: {type(parallel_config).__name__}")
        return iteration_substitutions

    @classmethod
    def _record_exchanges(cls, completed: list[IterationResult], failed: Exception | None, attempted: int) -> None:
        """Record this stage's HTTP exchanges for the report and the HAR file.

        ``last_request``/``last_response`` are the one exchange the report shows:
        the failing iteration's when it carries request info (its response may
        legitimately be None), else the last completed one. ``last_exchanges``
        holds every iteration only when HAR output is on, so an ordinary run
        never retains more than one response per stage.
        """
        failed_request, failed_response, failed_started = (failed.request, failed.response, failed.started) if isinstance(failed, StageExecutionError) else (None, None, None)

        exchanges: list[Exchange] = []
        for r in completed:
            # A redirect chain lives on .history, each hop carrying its own
            # request: expand it so the HAR shows every wire exchange. Individual
            # hop start times are not tracked; the iteration's start is the
            # closest truthful anchor (the first hop IS the request sent then).
            exchanges.extend((hop.request, hop, r.started) for hop in r.response.history)
            exchanges.append((r.request, r.response, r.started))
        if failed_request is not None:
            if failed_response is not None:
                exchanges.extend((hop.request, hop, failed_started) for hop in failed_response.history)
            exchanges.append((failed_request, failed_response, failed_started))
        if not cls.record_all_exchanges:
            exchanges = exchanges[-1:]

        cls.last_exchanges = exchanges
        cls.last_iterations_attempted = attempted

        if failed_request is not None:
            cls.last_request, cls.last_response = failed_request, failed_response
            cls.last_shown_exchange_is_failed = True
        elif exchanges:
            cls.last_request, cls.last_response, _ = exchanges[-1]

    @classmethod
    def _run_iterations(
        cls,
        stage: Stage,
        local_context: ChainMap[str, Any],
        iteration_substitutions: list[dict[str, Any]],
        parallel_config: ParallelConfig | None,
        exit_errors: list[str],
    ) -> tuple[list[IterationResult | None], tuple[int, Exception] | None]:
        """Run the iterations and return ``(results_by_index, first_error)``.

        One iteration runs inline; many run in a pool capped at
        ``max_concurrency`` with an optional global rate limiter. The first
        iteration to end in anything but success cancels the pool, and its
        outcome is the stage's whenever it is read (`_PoolCancel`), unless
        another iteration raises an interrupt (or, read first, a plugin bug).
        What the context managers of the iterations it cancelled or that were
        still running then raise on exit goes on ``exit_errors``, however this
        returns or raises (see `_fold_in_secondary`).
        """
        total = len(iteration_substitutions)
        results: list[IterationResult | None] = [None] * total
        first_error: tuple[int, Exception] | None = None
        limiter: Limiter | None = None

        try:
            if total == 1:
                # No limiter and no delay budget: a single iteration cannot block
                # on a fresh bucket, so the pool's rate-limiting arguments have
                # nothing to do here.
                try:
                    results[0] = cls._run_iteration(stage, local_context, iteration_substitutions[0])
                except _STAGE_FAILURE_EXCEPTIONS as e:
                    first_error = (0, e)
            else:
                # Only a parallel config can yield more than one iteration:
                # `_build_iteration_substitutions(None, ...)` returns exactly one.
                assert parallel_config is not None, "more than one iteration implies a parallel config"
                # Guarded rather than cast: the config arrives walk()-resolved,
                # and a resolved value can still be unusable (see `_parallel_number`).
                max_concurrency = _parallel_int("max_concurrency", parallel_config.max_concurrency)
                # None is "never declared" here: `_render_declared` already
                # refused a template that rendered to None.
                calls_per_sec = _parallel_int("calls_per_sec", parallel_config.calls_per_sec) if parallel_config.calls_per_sec is not None else None
                max_rate_limit_delay = _parallel_number("max_rate_limit_delay", parallel_config.max_rate_limit_delay)
                limiter = Limiter(Rate(calls_per_sec, Duration.SECOND)) if calls_per_sec is not None else None

                workers = min(max_concurrency, total)
                cancel = _PoolCancel()
                futures: dict[Future[IterationResult], int] = {}
                read: set[int] = set()
                try:
                    with ThreadPoolExecutor(max_workers=workers) as executor:
                        for idx, iter_vars in enumerate(iteration_substitutions):
                            future = executor.submit(cls._run_iteration, stage, local_context, iter_vars, limiter, max_rate_limit_delay, cancel, idx)
                            futures[future] = idx

                        try:
                            for future in as_completed(futures):
                                idx = futures[future]
                                read.add(idx)
                                try:
                                    results[idx] = future.result()
                                except _STAGE_OUTCOMES as e:
                                    # Its worker claimed the pool already; this
                                    # only asks whether it was the first to.
                                    if not cancel.claim(idx):
                                        # Another iteration ended the stage first:
                                        # its outcome, still to be read, is the one
                                        # to report.
                                        _fold_in_secondary(idx, e, results, exit_errors)
                                        continue
                                    if not isinstance(e, _STAGE_FAILURE_EXCEPTIONS):
                                        # A user function's skip, xfail or pytest.fail().
                                        raise
                                    first_error = (idx, e)
                                    executor.shutdown(wait=False, cancel_futures=True)
                                    break
                        except BaseException:
                            # A skip, KeyboardInterrupt or a plugin bug: without
                            # cancelling, the executor exit would run every queued
                            # iteration to completion, making a runaway parallel
                            # stage unstoppable.
                            cancel.set()
                            executor.shutdown(wait=False, cancel_futures=True)
                            raise
                finally:
                    # Leaving the pool waited for the iterations still running.
                    cls._fold_in_unread(futures, read, results, exit_errors)
        finally:
            # Every Limiter owns a daemon thread that lives until closed.
            if limiter is not None:
                limiter.close()

        return results, first_error

    @staticmethod
    def _fold_in_unread(
        futures: Mapping[Future[IterationResult], int],
        read: set[int],
        results: list[IterationResult | None],
        exit_errors: list[str],
    ) -> None:
        """Take in the iterations whose results the pool's loop did not read:
        those still running when one failed the stage (or skipped it, or the
        run was interrupted), which ended once the pool shut down.

        Their requests hit the wire, so each success is folded into
        ``results`` for the HAR; each failure is secondary (`_fold_in_secondary`).
        An interrupt one of them raised on exit still stops the run, once
        every other one is taken in: pytest.exit()'s, an Exception, was taken
        for a failure and dropped, and a KeyboardInterrupt left the rest's
        exit errors unreported.
        """
        interrupt: BaseException | None = None
        for future, idx in futures.items():
            if idx in read or not future.done() or future.cancelled():
                continue
            try:
                results[idx] = future.result()
            except _INTERRUPTS as e:
                if interrupt is None:
                    interrupt = e
            except (Exception, pytest.skip.Exception, pytest.fail.Exception) as e:
                _fold_in_secondary(idx, e, results, exit_errors)
        if interrupt is not None:
            raise interrupt

    @classmethod
    def _execute_http_request(cls, request_kwargs: dict[str, Any]) -> httpx.Response:
        """Send the request, mapping every failure to `RequestError`.

        The catch-all is deliberate: ``request()`` runs user auth code and httpx
        raises non-HTTPError types, and anything escaping raw would bypass the
        chain-abort machinery. The assert sits inside the try for the same reason.
        """
        try:
            assert cls.client is not None, "_ensure_initialized() builds cls.client before any request"
            return cls.client.request(**request_kwargs)
        except httpx.TimeoutException as e:
            raise _request_error("HTTP request timed out", e, cls.redaction) from e
        except httpx.ConnectError as e:
            raise _request_error("HTTP connection error", e, cls.redaction) from e
        except httpx.HTTPError as e:
            raise _request_error("HTTP request failed", e, cls.redaction) from e
        except Exception as e:
            raise _request_error("Unexpected error during HTTP request", e, cls.redaction) from e

    @staticmethod
    def _acquire_rate_slot(limiter: Limiter, timeout: float, cancel: threading.Event | None) -> bool:
        """Poll for a rate-limit slot so a pool-wide cancellation interrupts the
        wait; a blocking ``try_acquire`` would pin the thread (and delay the
        stage's failure report) for up to the full timeout.

        The 50ms interval is a cancellation-latency budget, NOT a throughput
        knob: pyrate_limiter's bucket is a sliding-window log, so a whole
        window's budget frees at once and one wake admits many calls. Deriving
        it from ``calls_per_sec`` was measured to change achieved throughput by
        under 2% while tripling CPU burnt in this loop.
        """
        deadline = time.monotonic() + timeout
        while True:
            if limiter.try_acquire("api", blocking=False):
                return True
            if cancel is not None and cancel.is_set():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))

    @classmethod
    def _run_iteration(
        cls,
        stage: Stage,
        local_context: ChainMap[str, Any],
        iter_vars: Mapping[str, Any],
        limiter: Limiter | None = None,
        max_rate_limit_delay: float = 60,
        cancel: _PoolCancel | None = None,
        idx: int = 0,
    ) -> IterationResult:
        """Execute one iteration, then exit the context managers it entered,
        in the thread that ran it.

        A parallel stage's iteration runs in a pool worker, and a thread-bound
        context manager (a ``sqlite3`` connection) can only be exited there;
        exiting at the iteration's end also releases what it holds (a
        transaction, a pooled connection) to the iterations still running. An
        exit error fails the iteration like any failure (an `_IterationExitError`,
        which keeps its exchange).

        Iteration ``idx`` of a parallel stage that does not succeed cancels the
        rest of the pool itself (`_PoolCancel.claim`), and before its exits
        when it failed on its own: the stage's thread only learns of the
        failure once they are done, and an exit can take a while (a rollback),
        during which the queued iterations would go on sending.
        """
        entered = _IterationContextManagers()
        token = _ITERATION_CONTEXT_MANAGERS.set(entered)
        try:
            try:
                result = cls._execute_single_iteration(stage, local_context, iter_vars, limiter, max_rate_limit_delay, cancel)
            except BaseException as e:
                if cancel is not None:
                    cancel.claim(idx)
                if not isinstance(e, _STAGE_OUTCOMES):
                    # An interrupt or a plugin bug is what gets reported.
                    _log_exit_errors(_exit_context_managers(entered.close()))
                    raise
                exit_errors = _exit_context_managers(entered.close())
                if not exit_errors:
                    raise
                raise _IterationExitError(exit_errors, own_failure=e) from e
            exit_errors = _exit_context_managers(entered.close())
            if exit_errors:
                if cancel is not None:
                    cancel.claim(idx)
                raise _IterationExitError(exit_errors, result=result)
            return result
        finally:
            _ITERATION_CONTEXT_MANAGERS.reset(token)

    @classmethod
    def _execute_single_iteration(
        cls,
        stage: Stage,
        local_context: ChainMap[str, Any],
        iter_vars: Mapping[str, Any],
        limiter: Limiter | None = None,
        max_rate_limit_delay: float = 60,
        cancel: threading.Event | None = None,
    ) -> IterationResult:
        """Resolve the request against the iteration context, take a rate-limit
        slot, send it, and run the response steps in order.

        ``cancel`` is the pool-wide cancellation signal: once another iteration
        fails (or the run is interrupted), in-flight iterations stop before
        sending rather than adding side-effecting traffic to a failed stage.
        """
        if cancel is not None and cancel.is_set():
            raise RequestError("Iteration cancelled: the stage already failed")

        iter_context = iteration_context(local_context, iter_vars)

        # Rendering re-validates the model it substitutes into, so no further
        # model_validate is needed here.
        request_model = _render_declared(stage.request, iter_context, "request", RequestError)
        request_kwargs = build_request_kwargs(request_model, cls.scenario_dir, cls._client_config, cls.redaction, declared_auth=stage.request.auth)

        if limiter is not None and not cls._acquire_rate_slot(limiter, max_rate_limit_delay, cancel):
            if cancel is not None and cancel.is_set():
                raise RequestError("Iteration cancelled while waiting for a rate-limit slot: the stage already failed")
            raise RequestError(f"Rate limit exceeded: could not acquire a request slot within {max_rate_limit_delay}s")

        if cancel is not None and cancel.is_set():
            raise RequestError("Iteration cancelled: the stage already failed")

        # Stamped after the acquire, so it reflects when the request went on the
        # wire; this feeds the HAR entry's startedDateTime.
        started = datetime.now(UTC)
        try:
            response = cls._execute_http_request(request_kwargs)
        except StageExecutionError as e:
            e.started = started
            raise

        try:
            saved_context: dict[str, Any] = {}
            response_meta = _response_meta(response)
            for step in stage.response:
                step_context = response_step_context(iter_context, response_meta)
                match step:
                    case SaveStep():
                        # A SubstitutionsSave renders its own entries strictly in
                        # order inside process_save; pre-walking it here would
                        # evaluate later entries before earlier ones' names exist
                        # and re-evaluate already-rendered values — so response-
                        # derived text containing '{{ }}' would be executed as an
                        # expression.
                        save_model = step.save if isinstance(step.save, SubstitutionsSave) else _render_declared(step.save, step_context, "save", SaveError)
                        step_saved = process_save(save_model, response, step_context)
                        # The static HTTPCHAIN027 check cannot see dynamically
                        # produced keys, so the shadowing is surfaced here too.
                        if RESPONSE_META_NAME in step_saved:
                            try:
                                warnings.warn(
                                    ScenarioValidationWarning(
                                        f"Save step produced the reserved name '{RESPONSE_META_NAME}': inside response steps it is "
                                        f"shadowed by the response metadata namespace (HTTPCHAIN027)"
                                    ),
                                    stacklevel=2,
                                )
                            except ScenarioValidationWarning as promoted:
                                # Promoted by filterwarnings=error: convert it to
                                # a stage failure so reporting and abort engage.
                                raise SaveError(str(promoted)) from None
                        iter_context = with_saves(iter_context, step_saved)
                        saved_context.update(step_saved)

                    case VerifyStep():
                        # Through the guard, not a bare walk(): process_verify sees
                        # the rendered model alone and cannot tell an absent
                        # assertion from one a template rendered away.
                        verify_model = _render_declared(step.verify, step_context, "verify", VerificationError)
                        process_verify(verify_model, response, cls.scenario_dir, cls.redaction)

                    case _:
                        raise RuntimeError(f"Unhandled response step: {type(step).__name__}")
        except StageExecutionError as e:
            e.request = response.request
            e.response = response
            e.started = started
            raise
        except (TemplatesError, ValidationError) as e:
            raise StageExecutionError(str(e), request=response.request, response=response, started=started) from e

        return IterationResult(
            saved_context=saved_context,
            request=response.request,
            response=response,
            started=started,
        )

    @classmethod
    def _wrap_factory_fixture(cls, name: str, fixture: Callable) -> Callable:
        """Wrap a callable fixture so a context-manager result is entered and
        registered to be exited: by the running iteration when it ends, or
        outside an iteration by the stage when it ends.

        Each call opens a resource, so the wrapped value must be invoked once per
        instance needed. The registry is looked up at call time, not at
        wrapping: a wrapper saved into the context and called in a later stage
        registers with that stage.

        One entered by a thread the iteration started, with an empty context
        or after the iteration's exits (`_IterationContextManagers`), goes on
        the stage's list. If the stage has ended by then, the next stage's end
        exits it, or the chain's (`_end_chain`).
        """

        def wrapped(*args, **kwargs):
            result = fixture(*args, **kwargs)

            if isinstance(result, AbstractContextManager):
                value = result.__enter__()
                entry = (name, result)
                entered = _ITERATION_CONTEXT_MANAGERS.get()
                if entered is None or not entered.add(entry):
                    # list.append is atomic, so it needs no lock.
                    cls.active_context_managers.append(entry)
                return value

            return result

        return wrapped

    @classmethod
    def teardown_class(cls) -> None:
        cls._end_chain()
        # Reset the initialization too, so a re-run of this class (e.g. a rerun
        # plugin) actually re-executes.
        for name, value in fresh_scenario_state().items():
            setattr(cls, name, value)

    @classmethod
    def _end_chain(cls) -> None:
        """Clean up after a chain and return the class to `fresh_chain_state`,
        keeping the scenario's initialization for the next chain.

        Each stage exits the context managers it entered, so the stage's list
        is empty by now, unless a thread a stage started entered one after the
        stage had ended (`_wrap_factory_fixture`) and no later stage did: it is
        exited here, and what its exit raises logged, as no stage is left to
        fail for it.
        """
        try:
            _log_exit_errors(_exit_context_managers(cls.active_context_managers))
        finally:
            if cls.client is not None:
                cls.client.close()

            for name, value in fresh_chain_state().items():
                setattr(cls, name, value)
            # maps[-1] is the pristine scenario context: saves only ever prepend layers.
            cls.global_context = base_global_context(cls.global_context.maps[-1])


def _context_dump(data: Mapping[str, Any]) -> str:
    """Render a context for DEBUG logging; a saved value can be anything, and
    logging must never break a stage."""
    try:
        return json.dumps(dict(data), indent=2, default=str)
    except Exception as e:
        return f"<unserializable context: {e}>"
