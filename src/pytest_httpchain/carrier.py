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

import functools
import inspect
import json
import logging
import math
import threading
import time
import warnings
from collections import ChainMap
from collections.abc import Callable, Collection, Hashable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import AbstractContextManager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, LiteralString, NamedTuple, TypeGuard, cast

import httpx
import pytest
from pydantic import BaseModel, RootModel, ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError
from pyrate_limiter import Duration, Limiter, Rate

from pytest_httpchain.body_schema import UNBOUNDED, ReferenceBounds
from pytest_httpchain.errors import RequestError, SaveError, StageExecutionError, VerificationError
from pytest_httpchain.har_writer import Exchange
from pytest_httpchain.models import (
    ClientConfig,
    CombinationsParameter,
    FileSpec,
    IndividualParameter,
    JMESPathMatcher,
    JsonBody,
    ParallelConfig,
    ParallelForeachConfig,
    ParallelRepeatConfig,
    ParallelThresholds,
    RegexCapture,
    ResponseBody,
    RetryConfig,
    SaveStep,
    Scenario,
    Stage,
    SubstitutionsSave,
    VarsNamespace,
    Verify,
    VerifyStep,
    validate_rendered,
    validate_rendered_scenario_auth,
    validate_rendered_verify,
)
from pytest_httpchain.parallel_stats import IterationEnd, ParallelStats, failed_iterations, parallel_stats, threshold_failure
from pytest_httpchain.redaction import DEFAULT_REDACTION, Redaction
from pytest_httpchain.request_builder import build_client_kwargs, build_request_kwargs
from pytest_httpchain.response_steps import RenderFailure, RenderOutcome, VerifyRender, process_save, process_verify
from pytest_httpchain.scoping import (
    RESPONSE_META_NAME,
    base_global_context,
    iteration_context,
    response_step_context,
    saved_in_response,
    stage_start_context,
    with_saves,
    with_stage_substitutions,
)
from pytest_httpchain.templates import TemplatesError, needs_rendering, walk, walker
from pytest_httpchain.utils import path_segment, process_substitutions
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


def _response_meta(response: httpx.Response) -> VarsNamespace:
    """The ``response`` namespace response-step templates see: metadata only,
    since ``save`` is what extracts body data. Read by attribute or by key,
    as a ``vars`` object is (``response['status']``)."""
    try:
        elapsed_ms = response.elapsed.total_seconds() * 1000
    except RuntimeError:
        elapsed_ms = None
    return VarsNamespace(
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


# The failures sending a request that a stage's ``retry`` (``on: request``)
# retries: the request timed out, its connection could not be made or broke
# off, or the server closed it without a complete response. Not what httpx
# refuses to send as written (a header value, a scheme), too many redirects,
# what an auth function raised, or a rate-limit slot that never came: the next
# attempt would repeat those, or wait as long again.
_NETWORK_ERRORS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)


def _request_error(what: str, e: Exception, redaction: Redaction) -> RequestError:
    """``e`` as the stage failure it is, its text shown through ``redaction``:
    the message prints above the request's report section, whose header values
    it may quote (h11 refusing ``b'Bearer <token>\\n'``). Retryable when the
    network failed it (`_NETWORK_ERRORS`)."""
    request = _error_request(e)
    text = str(e) if request is None else redaction.error_text(str(e), request.headers)
    return RequestError(f"{what}: {text}", request=request, retryable=isinstance(e, _NETWORK_ERRORS))


def _setting_number(setting: str, value: Any, must_be: str = "a positive number", accepts: Callable[[float], bool] = lambda number: number > 0) -> float:
    """A walk()-resolved numeric ``parallel`` or ``retry`` setting as a float,
    or a stage failure saying the ``setting`` ``must_be`` what ``accepts`` takes.

    The numeric fields are `PositiveInt | NumberOrTemplate` and the like, and
    walk() re-validates the config it resolves, but NumberOrTemplate accepts
    any complete template: a template resolving to another template string
    satisfies it and arrives here as text. A bare ``float()``/``int()`` would
    raise ValueError, which is not a stage failure and so escapes as a plugin
    traceback.
    """
    if isinstance(value, bool):
        # float(True) is 1.0: read as it is, a bool would configure one
        # attempt or a one-second wait. The retry model refuses one, written
        # or rendered, so none of its settings brings one here; the parallel
        # model reads one as 1 before it gets here. Kept so that whatever a
        # model lets through, this never reads a bool as a number.
        raise StageExecutionError(f"{setting} must be {must_be}, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError, ArithmeticError):
        raise StageExecutionError(f"{setting} must be {must_be}, got {value!r}") from None
    if not math.isfinite(number) or not accepts(number):
        raise StageExecutionError(f"{setting} must be {must_be}, got {value!r}")
    return number


def _setting_int(setting: str, value: Any) -> int:
    """`_setting_number` for a positive whole-number setting. Truncating
    instead would turn a resolved 0.5 into 0 — no workers, a silently disabled
    limiter, no attempt at all.

    An int is taken as it stands: `PositiveInt` has no upper bound, and routing
    one through ``float()`` would raise OverflowError on a value the callers
    handle perfectly well (``max_concurrency`` clamps against the iteration
    count, and pyrate_limiter accepts any int rate).
    """
    if isinstance(value, int) and not isinstance(value, bool):
        if value <= 0:
            raise StageExecutionError(f"{setting} must be a positive whole number, got {value!r}")
        return value
    number = _setting_number(setting, value)
    if not number.is_integer():
        raise StageExecutionError(f"{setting} must be a positive whole number, got {value!r}")
    return int(number)


def _parallel_number(field: str, value: Any) -> float:
    """A walk()-resolved ``parallel`` setting as a positive float, or a stage failure."""
    return _setting_number(f"parallel.{field}", value)


def _parallel_int(field: str, value: Any) -> int:
    """A walk()-resolved ``parallel`` setting as a positive int, or a stage failure."""
    return _setting_int(f"parallel.{field}", value)


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


def _parallel_flag(field: str, value: Any) -> bool:
    """A walk()-resolved ``parallel`` switch, or a stage failure.

    Its template branch accepts any complete template, so one that rendered
    to another template arrives here as text, which is truthy: read as it is,
    it would turn the switch on. Any other value that is not a bool the
    model's own validation refused on re-validating what was rendered, but a
    1 or a 0, which it took as true or false, as it does in every bool
    setting (``client.http2``, a regex save's ``all``).
    """
    if not isinstance(value, bool):
        raise StageExecutionError(f"parallel.{field} must resolve to true or false, got {value!r}")
    return value


def _threshold_limits(thresholds: ParallelThresholds | None) -> dict[str, float]:
    """A walk()-resolved ``parallel.thresholds`` as the limits the stage's
    stats are held to, by threshold, or a stage failure naming one it cannot
    use: resolved before any request, as the rest of the ``parallel`` config
    is, so a limit a template rendered to template text fails the stage then,
    not once every iteration has run. None is "never declared" here:
    `_render_declared` already refused a template that rendered to None."""
    if thresholds is None:
        return {}
    limits: dict[str, float] = {}
    for name in type(thresholds).model_fields:
        value = getattr(thresholds, name)
        if value is None:
            continue
        setting = f"parallel.thresholds.{name}"
        if name == "min_success_ratio":
            limits[name] = _setting_number(setting, value, "a number from 0 to 1", lambda number: 0 <= number <= 1)
        else:
            limits[name] = _setting_number(setting, value)
    return limits


def _merged_saves(saved: Sequence[Mapping[str, Any]], collect: bool, declared: Iterable[str] = ()) -> dict[str, Any]:
    """What a stage whose iterations all succeeded commits, from each one's
    saves (``saved[i]`` is iteration i's): in iteration order, never in the
    order the iterations completed in, so what a stage commits does not depend
    on timing.

    By default they merge, and of the iterations that save the same name the
    one with the highest index wins. With ``collect`` (``parallel.collect_saves``)
    every name any iteration saved becomes a list of one entry per iteration,
    entry i iteration i's, and None where iteration i did not save the name
    (a user function's save can return different names each time): creating
    N resources and deleting them all later needs every id, and the index
    keeps each entry beside its iteration's ``foreach`` parameters.

    An iteration that failed, which only a ``min_success_ratio`` below 1 lets
    the stage run on after, saved nothing: ``saved[i]`` is empty, so its
    entry is None, and a merge takes its names from the others. Every name in
    ``declared``, those the stage's response steps save as written (which
    every iteration that passes saves), is committed all the same: None
    merged, a list of None collected, when no iteration saved it, which is
    when none passed (a ``min_success_ratio`` of 0). A later stage reading it
    finds it, as `validate` says it will, where it would find no such name.
    """
    if not collect:
        merged: dict[str, Any] = {}
        for iteration in saved:
            merged.update(iteration)
        for name in declared:
            merged.setdefault(name, None)
        return merged
    names = dict.fromkeys([*(name for iteration in saved for name in iteration), *declared])
    return {name: [iteration.get(name) for iteration in saved] for name in names}


def _with_stats(saves: dict[str, Any], name: str, stats: ParallelStats, declared: Collection[str] = ()) -> dict[str, Any]:
    """``saves`` with a parallel stage's stats under ``name``
    (``parallel.stats_as``). Committed with the iterations' saves, after the
    response steps', they replace a response save of the same name. The
    validator reports that as HTTPCHAIN040 where a step declares the name
    (``declared``, `saved_in_response`); this warns the same where it cannot
    see it, a name only a user function's save returned, so that one
    collision is warned of once."""
    if name in saves and name not in declared:
        try:
            warnings.warn(
                ScenarioValidationWarning(f"parallel.stats_as '{name}' is also a name the stage's response saves: the stats replace what it saved (HTTPCHAIN040)"),
                stacklevel=3,
            )
        except ScenarioValidationWarning as promoted:
            # Promoted by filterwarnings=error: a stage failure, so that
            # reporting and the abort engage.
            raise StageExecutionError(str(promoted)) from None
    return {**saves, name: stats.saved()}


# The error each kind a stage's ``retry.on`` names is a failure of: a verify
# step's, a save step's, the request's.
_RETRY_ON_ERRORS: dict[str, type[StageExecutionError]] = {"verify": VerificationError, "save": SaveError, "request": RequestError}


@dataclass(frozen=True, slots=True)
class _RetryPolicy:
    """A stage's ``retry`` resolved (`_retry_policy`): how many attempts it
    makes at most, how long it waits before each after the first, and after
    which failures. `_NO_RETRY` is a stage's without one: one attempt."""

    attempts: int = 1
    delay: float = 1
    backoff: float = 1
    max_delay: float | None = None
    on: tuple[type[StageExecutionError], ...] = ()

    def retries(self, error: BaseException) -> TypeGuard[StageExecutionError]:
        """Whether ``error`` ending an attempt makes another, if one is left:
        a failure of a kind ``on`` names, and one another attempt may not
        repeat (`StageExecutionError.retryable`). A template or validation
        error is no stage failure (it is the scenario's own), nor is a pytest
        outcome a user function raised, and neither is retried."""
        return isinstance(error, self.on) and error.retryable

    def waits(self) -> Iterator[float]:
        """The wait before each attempt after the first, in order: ``delay``,
        multiplied by ``backoff`` after each attempt, never more than
        ``max_delay``. Exhausted once no attempt is left."""
        wait = self.delay
        for _ in range(self.attempts - 1):
            yield wait if self.max_delay is None else min(wait, self.max_delay)
            # Past float's range this is inf, which the cap (or the wait's
            # own, `_wait_to_retry`) takes.
            wait *= self.backoff


_NO_RETRY = _RetryPolicy()


def _retry_policy(config: RetryConfig | None) -> _RetryPolicy:
    """A walk()-resolved ``retry`` as the policy the iterations follow, or a
    stage failure naming a setting it cannot use. Resolved before any request,
    as the ``parallel`` config is: a setting a template rendered to template
    text fails the stage then, not once the first attempt has failed."""
    if config is None:
        return _NO_RETRY
    return _RetryPolicy(
        attempts=_setting_int("retry.attempts", config.attempts),
        delay=_setting_number("retry.delay", config.delay, "a number of seconds, 0 or more", lambda number: number >= 0),
        backoff=_setting_number("retry.backoff", config.backoff, "a number, 1 or more", lambda number: number >= 1),
        # None is "never declared" here: `_render_declared` already refused a
        # template that rendered to None.
        max_delay=_setting_number("retry.max_delay", config.max_delay, "a number of seconds, 0 or more", lambda number: number >= 0) if config.max_delay is not None else None,
        on=tuple(_RETRY_ON_ERRORS[kind] for kind in ([config.on] if isinstance(config.on, str) else config.on)),
    )


def _wait_to_retry(seconds: float, cancel: threading.Event | None) -> bool:
    """Wait ``seconds`` before an iteration's next attempt: True once they
    have passed, False as soon as ``cancel`` (a parallel stage's, `_PoolCancel`)
    is set, since another iteration ended the stage and a later attempt would
    add traffic to a stage already failed. A plain sleep would hold the
    stage's failure back until the wait ran out.

    An event's wait, even without a pool: an interrupt (Ctrl-C) ends it as it
    ends a sleep. A wait past what a lock can wait (an overflowed backoff) is
    that long instead, which is as good as forever.
    """
    event = cancel if cancel is not None else threading.Event()
    return not event.wait(min(seconds, threading.TIMEOUT_MAX))


def _with_attempts(message: str, attempt: int) -> str:
    """``message`` saying ``attempt`` attempts ran: at the end of its first
    line, the one a parallel stage's failure quotes, and before a colon that
    ends it: ``3 verification checks failed (after 3 attempts):``."""
    first, newline, rest = message.partition("\n")
    note = f"(after {attempt} attempts)"
    if first.endswith(":"):
        first = f"{first[:-1]} {note}:"
    else:
        first = f"{first} {note}" if first else note
    return f"{first}{newline}{rest}"


def _after_attempts(error: BaseException, attempt: int, earlier: tuple[Exchange, ...]) -> StageExecutionError:
    """How an iteration fails whose ``attempt``-th attempt, after earlier ones
    that failed, ended in ``error``: that failure, its message saying how many
    attempts ran (`_with_attempts`), carrying the exchanges of the earlier
    ones (``earlier``, for the HAR file) and which attempt it is (for the
    report). A template or validation error ending a later attempt becomes a
    stage failure, which alone carries these.
    """
    if not isinstance(error, StageExecutionError):
        failure = StageExecutionError(str(error))
        failure.__cause__ = error
        error = failure
    error.args = (_with_attempts(str(error), attempt),)
    error.attempt = attempt
    error.earlier_exchanges = earlier
    return error


# Where a pytest outcome a user function raised carries the exchanges of its
# iteration's attempts (`_note_outcome_exchanges`): pytest's exception has no
# field for them, and it is the outcome that reaches the stage.
_OUTCOME_EXCHANGES = "_httpchain_exchanges"


def _outcome_exchanges(outcome: BaseException) -> tuple[Exchange, ...]:
    """The exchanges noted on ``outcome`` (`_note_outcome_exchanges`), oldest first."""
    return getattr(outcome, _OUTCOME_EXCHANGES, ())


def _note_outcome_exchanges(outcome: BaseException, exchanges: Iterable[Exchange]) -> None:
    """Note ``exchanges`` on ``outcome``, a skip, xfail or fail() a user
    function raised once requests had gone on the wire, before those it
    carries already: the HAR file records them (`Carrier.execute_stage`), as
    it records a failed stage's, being real traffic."""
    setattr(outcome, _OUTCOME_EXCHANGES, (*exchanges, *_outcome_exchanges(outcome)))


def _outcome_after_attempts(outcome: BaseException, attempt: int, earlier: tuple[Exchange, ...]) -> None:
    """``outcome``, a skip, xfail or fail() a user function raised on the
    ``attempt``-th attempt, after earlier ones that failed: it ends the stage
    as it would have on the first attempt, never retried, carrying the earlier
    attempts' exchanges (``earlier``) for the HAR file. A fail() says how many
    attempts ran, as a stage failure does (`_with_attempts`); a skip's or an
    xfail's reason, which is no failure, is left as the function wrote it."""
    _note_outcome_exchanges(outcome, earlier)
    if isinstance(outcome, pytest.fail.Exception) and not isinstance(outcome, pytest.xfail.Exception):
        outcome.msg = _with_attempts(outcome.msg or "", attempt)


def _wire_exchanges(request: httpx.Request, response: httpx.Response | None, started: datetime | None) -> list[Exchange]:
    """The exchanges one request made on the wire, for the HAR file: its
    redirect chain first, which lives on ``response.history``, each hop
    carrying its own request. Individual hop start times are not tracked; the
    request's start is the closest truthful anchor (the first hop IS the
    request sent then)."""
    hops: list[Exchange] = [(hop.request, hop, started) for hop in response.history] if response is not None else []
    return [*hops, (request, response, started)]


def _none_is_a_value(model: BaseModel, field: str) -> bool:
    """The fields exempt from `_render_declared`: None there is something the
    scenario can mean, not a setting that vanished — a JSON body of ``null``
    (``request_builder`` sends it as such) and free-text descriptions."""
    return field == "description" or (isinstance(model, JsonBody) and field == "json")


def _none_is_compared(model: BaseModel, field: str) -> bool:
    """The fields where a None is not "undeclared" but an operand the check
    compares with: a `JMESPathMatcher`'s eq, ne, contains and not_contains,
    where the scenario writes null to mean null. A template there that rendered
    to None is still refused (it more likely lost the value it was written
    for), but it would not have disabled the check."""
    return isinstance(model, JMESPathMatcher) and field in JMESPathMatcher.NULL_OPERANDS


def _none_is_not_set(model: BaseModel, field: str) -> bool:
    """The fields a model itself reads an explicit None in as not set: a
    `FileSpec`'s sources, of which it counts the ones not None, and takes
    exactly one. An object rendered whole holding None in another source, as
    a user function or a saved object may fill the keys it does not use,
    sends the file from the one it sets, as the same object written out does:
    the None disables nothing. Declared as a template of its own, a source
    that rendered to None leaves the file none, which `_rendered_away` reports
    through the validation error."""
    return isinstance(model, FileSpec) and field in FileSpec.SOURCES


def _none_would(model: BaseModel, field: str) -> str:
    """What a None let through ``field`` would have done, for the refusal to
    say. An optional field reads None as never declared: for most that turns
    a check or a setting off, but a regex save's group left out picks the
    default one (group 1, or the whole match), so the save still runs, from
    another group, and a multipart file's filename or content type left out
    is sent as the default one."""
    if isinstance(model, RegexCapture) and field == "group":
        return "save the default group instead"
    if isinstance(model, FileSpec) and field in ("filename", "content_type"):
        return f"send the default {field.replace('_', ' ')} instead"
    return "disable it"


# Where a value sits in a dumped model: field names and dict keys, list indices.
type _Keys = tuple[str | int, ...]

# Where a value sits in a model built to validate it, to where the scenario
# declares it (`_validate_substituted`).
type _Relocation = Callable[[_Keys], _Keys]


class _Vanished(NamedTuple):
    """A declared field a template rendered to None: where it sits, the
    template as written, whether None is an operand there
    (`_none_is_compared`), and what it would have done otherwise
    (`_none_would`)."""

    keys: _Keys
    template: str
    compared: bool = False
    would: str = "disable it"


def _rendered_away(declared: Any, substituted: Any, keys: _Keys = ()) -> Iterator[_Vanished]:
    """Each model field that was declared but rendered to None, ``keys``
    locating it in ``substituted``.

    ``substituted`` is ``declared`` dumped and substituted but not yet
    validated, so it is read alongside the declared models, which say what is a
    model field and what a dict value; dicts and lists are followed to the
    models inside them (``verify.headers`` holds its matchers in a dict). Only
    model fields count: a dict value or list item that renders to None — a query
    parameter, a user-function kwarg, a ``verify.jmespath`` value compared with
    null — is a value handed on, not a field left undeclared. Only a string can
    render to None (walk() maps containers element-wise and dumps other models
    to dicts first), so what was declared is always a template.
    """

    def field(declared_value: Any, substituted_value: Any, field_keys: _Keys, compared: bool = False, would: str = "disable it") -> Iterator[_Vanished]:
        if isinstance(declared_value, RootModel):
            # Dumped as its bare root value, not as {"root": ...}: the root is the
            # field, at the model's own place (a user-function name).
            declared_value = declared_value.root
        if declared_value is not None and substituted_value is None:
            yield _Vanished(field_keys, declared_value, compared, would)
        else:
            yield from _rendered_away(declared_value, substituted_value, field_keys)

    match declared, substituted:
        case RootModel(), _:
            yield from field(declared, substituted, keys)
        case BaseModel(), dict():
            # Only the declared fields were dumped (`_render_declared`).
            for name in type(declared).model_fields:
                if name in substituted and not _none_is_a_value(declared, name):
                    yield from field(getattr(declared, name), substituted[name], (*keys, name), _none_is_compared(declared, name), _none_would(declared, name))
        case dict(), dict():
            for key, declared_value in declared.items():
                if key in substituted:
                    yield from _rendered_away(declared_value, substituted[key], (*keys, key))
        case list() | tuple(), list() | tuple():
            # Substitution rewrites a sequence element-wise, so the lengths match.
            for i, (declared_value, substituted_value) in enumerate(zip(declared, substituted, strict=True)):
                yield from _rendered_away(declared_value, substituted_value, (*keys, i))


def _rendered_whole_away(declared: Any, rendered: Any, keys: _Keys = ()) -> Iterator[_Vanished]:
    """`_rendered_away` for a model that one template rendered whole: a header
    matcher written as ``"{{ {'contains': ct, 'not_contains': 'text/html'} }}"``,
    or saved from the response and used as ``"{{ matcher }}"``, and each model
    in a list one rendered (multipart files, ``"images": "{{ files }}"``).

    Declared as a single string, such a model's fields are known only once
    validation has built it, so ``rendered`` is the validated model. A field the
    rendered mapping set explicitly (``model_fields_set``) to None is refused; one
    it left out was never declared. A field the model itself reads None in as
    not set (`_none_is_not_set`) is neither.

    A `JMESPathMatcher`, whose null operands (`_none_is_compared`) this would
    misread, is never rendered whole: a template where a ``verify.jmespath``
    value is written renders a value (`validate_rendered_verify`).
    """
    match declared, rendered:
        case BaseModel(), BaseModel() if type(declared) is type(rendered):
            for name in type(declared).model_fields:
                yield from _rendered_whole_away(getattr(declared, name), getattr(rendered, name), (*keys, name))
        case (str() as template, BaseModel()) | (RootModel(root=str() as template), BaseModel()):
            for name in type(rendered).model_fields:
                if name in rendered.model_fields_set and getattr(rendered, name) is None and not (_none_is_a_value(rendered, name) or _none_is_not_set(rendered, name)):
                    yield _Vanished((*keys, name), template, would=_none_would(rendered, name))
        case str(), list() | tuple():
            # The models are its items (a list of files does not nest), and
            # any other list a template renders (a JSON body, a query
            # parameter's values) holds none: those items are not walked.
            for i, item in enumerate(rendered):
                if isinstance(item, BaseModel):
                    yield from _rendered_whole_away(declared, item, (*keys, i))
        case dict(), dict():
            for key, declared_value in declared.items():
                if key in rendered:
                    yield from _rendered_whole_away(declared_value, rendered[key], (*keys, key))
        case list() | tuple(), list() | tuple():
            for i, (declared_value, rendered_value) in enumerate(zip(declared, rendered, strict=True)):
                yield from _rendered_whole_away(declared_value, rendered_value, (*keys, i))


def _at(structure: Any, keys: _Keys) -> Any:
    """What sits at ``keys`` in ``structure``: a model's field, a dict's value,
    a list's item."""
    for key in keys:
        structure = getattr(structure, str(key)) if isinstance(structure, BaseModel) else structure[key]
    return structure


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
    reported as pydantic has it, under the refusal. Where None is an operand
    (`_none_is_compared`) it validates but disables nothing either: the
    message says how to compare with null instead.
    """
    # walk()'s own model step (hands back a model with nothing to render
    # untouched; otherwise dump, substitute, re-validate), taken apart at the
    # re-validation. Only the declared fields are dumped, so the rendered model
    # keeps the declared one's `model_fields_set`: a request's timeout and
    # redirect setting count only where declared (`request_builder`), and a full
    # dump made every default look declared. A model holding an escaped `\{{`
    # and no template is rendered too: only rendering removes the escape.
    if not needs_rendering(declared):
        return declared
    return _validate_substituted(declared, walk(declared.model_dump(mode="python", exclude_unset=True), context), where, error, validate)


def _validate_substituted[M: BaseModel](
    declared: M,
    substituted: Any,
    where: str,
    error: type[StageExecutionError] = StageExecutionError,
    validate: Callable[[Any], M] | None = None,
    relocate: _Relocation | None = None,
) -> M:
    """`_render_declared` from the substitution on: ``substituted`` is
    ``declared`` dumped (its declared fields) with its templates rendered,
    validated here as rendered text (by default `validate_rendered`), and
    refused where a template rendered a field to None.

    ``relocate`` maps where a value sits in ``declared`` to where its messages
    say it is, when ``declared`` holds it elsewhere than the scenario does
    (`_validated_values`): the guard's refusal and pydantic's report alike."""
    validate = validate or functools.partial(validate_rendered, type(declared))
    if relocate is not None:
        validate = _relocating(validate, relocate)
    vanished = list(_rendered_away(declared, substituted))
    try:
        rendered = validate(substituted)
    except ValidationError as e:
        if not vanished:
            raise
        refusal = _rendered_to_none(where, vanished[0], relocate)
        restored = substituted
        for keys, template, *_ in vanished:
            restored = _replaced(restored, keys, template)
        try:
            validate(restored)
        except ValidationError as other:
            raise error(f"{refusal}\n{other}") from e
        raise error(refusal) from e
    vanished = vanished or list(_rendered_whole_away(declared, rendered))
    if vanished:
        first = vanished[0]
        refusal = _rendered_to_none(where, first, relocate)
        raise error(refusal if first.compared else f"{refusal}, which would silently {first.would}")
    return rendered


def _relocation(in_part: _Keys, at: _Keys) -> _Relocation | None:
    """Keys in a verify step cut down to one value (`_verify_part`), which
    holds it at ``in_part``, as they are in the step, which holds it at
    ``at``: a list item validated alone, at index 0, keeps its own index. None
    where the two are the same place."""
    if in_part == at:
        return None
    return lambda keys: (*at, *keys[len(in_part) :]) if keys[: len(in_part)] == in_part else keys


def _relocating[M](validate: Callable[[Any], M], relocate: _Relocation) -> Callable[[Any], M]:
    """``validate``, its `ValidationError` located by ``relocate``."""

    def relocated(value: Any) -> M:
        try:
            return validate(value)
        except ValidationError as e:
            raise _relocated(e, relocate) from None

    return relocated


def _relocated(e: ValidationError, relocate: _Relocation) -> ValidationError:
    """``e``, each error at the location ``relocate`` gives its own: the same
    errors, in the same order, each of its type, message and input. One of
    pydantic's own (it has a ``url``) is rebuilt as that type with its
    context, which writes its message, the link included, as pydantic did; a
    custom one, whose template is not kept, as a custom error of the same type
    and message."""
    details: list[InitErrorDetails] = []
    for error in e.errors():
        detail: InitErrorDetails = {"type": error["type"], "loc": relocate(error["loc"]), "input": error["input"]}
        if "url" not in error:
            detail["type"] = PydanticCustomError(cast(LiteralString, error["type"]), cast(LiteralString, error["msg"]))
        elif "ctx" in error:
            detail["ctx"] = error["ctx"]
        details.append(detail)
    return ValidationError.from_exception_data(e.title, details)


def _verify_renderer(declared: Verify, context: Mapping[str, Any]) -> VerifyRender:
    """How `process_verify` renders a declared verify step: value by value, so
    that a template that cannot be rendered is one failure of the step, listed
    with the others. Rendered whole, the step ended at the first, before any
    check had run.

    Each value is substituted on its own, and every one with the evaluator
    one `walker` builds from ``context`` for the step: a walk() per value
    would rebuild it from the whole context, which gains a layer per stage and
    per save step, once for each value. A template error is that value's
    failure. A pytest outcome that a function a template calls raises is that
    value's, and ends the rendering there, as it ended the whole step's. What
    was substituted is then validated (`_validated_values`).
    """

    def render(values: list[tuple[_Keys, Any]]) -> dict[_Keys, Any]:
        rendered: dict[_Keys, Any] = {}
        substituted: dict[_Keys, Any] = {}
        # Built for the first value holding a template or an escape: a step
        # without one needs neither.
        substitute: Callable[[Any], Any] | None = None
        dumped: dict[str, Any] = {}
        for at, value in values:
            if not needs_rendering(value):
                rendered[at] = value
                continue
            if substitute is None:
                substitute, dumped = walker(context), declared.model_dump(mode="python", exclude_unset=True)
            try:
                substituted[at] = substitute(_at(dumped, at))
            except TemplatesError as e:
                error = VerificationError(str(e))
                error.__cause__ = e
                rendered[at] = RenderFailure(error)
            except (pytest.skip.Exception, pytest.fail.Exception) as e:
                rendered[at] = RenderOutcome(e)
                break
        if substituted:
            rendered |= _validated_values(declared, dumped, substituted)
        return rendered

    return render


def _validated_values(declared: Verify, dumped: dict[str, Any], substituted: dict[_Keys, Any]) -> dict[_Keys, Any]:
    """Each value of ``substituted`` (by where it is declared, its templates
    substituted) as its check takes it, or a `RenderFailure`. It goes through
    the guard every declared model does (`_validate_substituted`), so a
    template that rendered to None is refused, not read as undeclared, and a
    ``verify.jmespath`` value stays a value, an object it rendered included
    (`validate_rendered_verify`). Pydantic's report becomes the step's error
    type, its message kept.

    The step is validated whole first, the values put in ``dumped`` (its
    declared fields, dumped for this rendering alone). When every value is
    valid, the common case, that is all it costs, as when the step rendered
    whole. Otherwise each value is validated in a model of it alone
    (`_verify_part`), so that each that fails is a failure of its own: a
    ``headers`` or ``jmespath`` entry alone in its map, a list item alone in
    its list, so every item of a list costs the list once, not once per item,
    failing or not, and no value is rendered twice.

    A list item's messages give its index as it is in the step
    (``body.contains.1``, ``verify.user_functions[1]``), not the 0 it has
    alone (`_relocation`). Validated again with its list whole to have that
    index, a failing item cost the list once more, and a list of templates
    that all failed took time quadratic in its length.
    """
    for at, value in substituted.items():
        _at(dumped, at[:-1])[at[-1]] = value
    try:
        step = _validated_verify(declared, dumped)
    except VerificationError:
        pass
    else:
        return {at: _at(step, at) for at in substituted}
    values: dict[_Keys, Any] = {}
    for at, value in substituted.items():
        part, in_part = _verify_part(declared, at)
        try:
            values[at] = _at(_validated_verify(part, _replaced(part.model_dump(mode="python", exclude_unset=True), in_part, value), _relocation(in_part, at)), in_part)
        except VerificationError as e:
            values[at] = RenderFailure(e)
    return values


def _validated_verify(declared: Verify, substituted: Any, relocate: _Relocation | None = None) -> Verify:
    """``substituted``, ``declared`` dumped with its templates substituted,
    validated as it rendered (`_validate_substituted`, ``relocate`` locating
    its messages), or the `VerificationError` saying why it is not valid."""
    try:
        return _validate_substituted(declared, substituted, "verify", VerificationError, functools.partial(validate_rendered_verify, declared), relocate)
    except ValidationError as e:
        raise VerificationError(str(e)) from e


def _render_save[M: BaseModel](declared: M, context: Mapping[str, Any]) -> M:
    """A save step rendered through the guard (`_render_declared`). A value
    it renders that does not validate is the step's `SaveError`, as a verify
    step's is its `VerificationError` (`_validated_verify`): a regex or a
    JMESPath expression a template rendered that does not compile, a group
    the pattern a template rendered does not have. Neither that nor the
    guard's refusal is retryable: it is the step's templates that failed, not
    the extraction from this response."""
    try:
        return _render_declared(declared, context, "save", SaveError)
    except ValidationError as e:
        raise SaveError(str(e), retryable=False) from e
    except SaveError as e:
        e.retryable = False
        raise


# What `_verify_part` cuts a step down from: nothing set (``model_fields_set``
# empty, so a copy's holds only the field it is given), and its defaults
# resolved once. model_construct() resolves each default factory, inspecting
# its signature every time: per value rendered, that was most of the cost.
_NO_VERIFY = Verify.model_construct()
_NO_BODY = ResponseBody.model_construct()


def _verify_part(declared: Verify, at: _Keys) -> tuple[Verify, _Keys]:
    """``declared`` cut down to the value at ``at`` (the body's field, for the
    body's), and where the value sits in it: a map to the one entry, a list to
    the one item, at index 0."""
    match at:
        case ("body", str() as name, *rest):
            values, in_part = _one_value(getattr(declared.body, name), name, rest)
            return _NO_VERIFY.model_copy(update={"body": _NO_BODY.model_copy(update=values)}), ("body", *in_part)
        case (str() as name, *rest):
            values, in_part = _one_value(getattr(declared, name), name, rest)
            return _NO_VERIFY.model_copy(update=values), in_part
        case _:
            raise RuntimeError(f"Unhandled verify location: {at!r}")


def _one_value(whole: Any, name: str, rest: list[str | int]) -> tuple[dict[str, Any], _Keys]:
    """The field ``name`` holding only the value at ``rest`` within it (none:
    the field's own value), and where the value is in the field so cut."""
    match rest:
        case [str() as key]:
            return {name: {key: whole[key]}}, (name, key)
        case [int() as i]:
            return {name: [whole[i]]}, (name, 0)
        case _:
            return {name: whole}, (name,)


def _rendered_to_none(where: str, vanished: _Vanished, relocate: _Relocation | None = None) -> str:
    keys = vanished.keys if relocate is None else relocate(vanished.keys)
    path = where + "".join(map(path_segment, keys))
    message = f"'{path}' was declared as {vanished.template!r} but rendered to None"
    return f"{message}; to compare with null, write null" if vanished.compared else message


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
        "last_shown_attempt": None,
        "last_parallel_stats": None,
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
    """A successful stage iteration: its attempt that succeeded. ``started`` is
    when the request went on the wire, which is what HAR waterfalls are built
    from. ``attempt`` is which attempt of the stage's ``retry`` it is, and
    ``earlier_exchanges`` the exchanges of those before it, when the HAR file
    records them (`Carrier._execute_attempts`). ``duration`` is the seconds
    its attempts' requests spent in the HTTP client, the iteration's latency
    in a parallel stage's stats (see `parallel_stats`)."""

    saved_context: dict[str, Any]
    request: httpx.Request
    response: httpx.Response
    started: datetime
    attempt: int = 1
    earlier_exchanges: tuple[Exchange, ...] = ()
    duration: float = 0.0


class _Cancelled(RequestError):
    """An iteration that another one ending the stage stopped: before it sent
    its request, or while it waited for a rate-limit slot or to retry. A
    parallel stage's stats count it as cancelled, not failed."""


def _iteration_end(outcome: BaseException | None) -> IterationEnd:
    """How an iteration that ended in ``outcome`` (None: in success, its exits
    too) counts in its stage's stats. One whose context managers raised on
    exit (`_IterationExitError`) counts as it ended itself, but for a success,
    which its exits failed."""
    if isinstance(outcome, _IterationExitError):
        if outcome.own_failure is None:
            return "failed"
        outcome = outcome.own_failure
    if outcome is None:
        return "passed"
    if isinstance(outcome, _Cancelled):
        return "cancelled"
    if isinstance(outcome, (pytest.skip.Exception, pytest.xfail.Exception)):
        return "skipped"
    return "failed"


class _ParallelRun:
    """A parallel stage's iterations as they end, for its stats and its
    thresholds: how each ended and, when it passed, how long its exchanges
    took, which its own thread records (`Carrier._run_iteration`); the stage's
    wall time, from the first iteration's start (`begin`) to the last one's
    end (`end`); and, where ``tolerate`` (a ``min_success_ratio`` below 1),
    the failures it tolerated, by iteration (`tolerates`). An iteration with
    no end recorded never started: the pool was cancelled first.

    Each thread writes its own iteration's entries, and the stage's thread
    reads them only once the pool has shut down, so they need no lock; the
    stage's thread alone writes the failures tolerated.
    """

    def __init__(self, stage: str, total: int, tolerate: bool = False) -> None:
        self.stage = stage
        self.tolerate = tolerate
        self.ends: list[IterationEnd | None] = [None] * total
        self.durations: list[float | None] = [None] * total
        self.tolerated: dict[int, Exception] = {}
        self._began: float | None = None
        self.wall_seconds = 0.0

    @property
    def began(self) -> bool:
        """Whether any iteration started: a stage that failed before its
        iterations did has no stats to report."""
        return self._began is not None

    def begin(self) -> None:
        self._began = time.perf_counter()

    def end(self) -> None:
        if self._began is not None:
            self.wall_seconds = time.perf_counter() - self._began

    def record(self, idx: int, outcome: BaseException | None, result: IterationResult | None = None) -> None:
        """Iteration ``idx`` ended in ``outcome`` (None: it succeeded with ``result``)."""
        self.ends[idx] = _iteration_end(outcome)
        if result is not None:
            self.durations[idx] = result.duration

    def tolerates(self, outcome: BaseException) -> TypeGuard[Exception]:
        """Whether ``outcome``, how an iteration ended, is a failure the stage
        tolerates: where ``tolerate``, an iteration's own failure, however
        the stage ends. Never a cancellation, which another iteration ending
        the stage caused, nor an error on exit (`_IterationExitError`), nor a
        user function's pytest outcome: each ends the stage, or is secondary
        to what did (`_fold_in_secondary`)."""
        return self.tolerate and isinstance(outcome, _STAGE_FAILURE_EXCEPTIONS) and not isinstance(outcome, (_Cancelled, _IterationExitError))

    def run_on(self, idx: int, failure: Exception) -> None:
        """Iteration ``idx`` failed, and the stage tolerates it (`tolerates`):
        the failure counts once every iteration has ended."""
        self.tolerated[idx] = failure
        logger.info("Stage '%s': iteration %d failed, tolerated (min_success_ratio): %s", self.stage, idx, str(failure).partition("\n")[0])

    def failures(self) -> list[tuple[int, Exception]]:
        """The failures the stage tolerated, lowest index first."""
        return sorted(self.tolerated.items())

    def failure_exchanges(self, but: Exception | None = None) -> list[Exchange]:
        """What the failures the stage tolerated sent, lowest index first,
        but ``but``'s: real traffic, which the HAR file records however the
        stage ends (`Carrier._run_iterations`)."""
        return [exchange for _, failure in self.failures() if failure is not but for exchange in _failure_exchanges(failure)]

    def stats(self, limits: Mapping[str, float] | None = None) -> ParallelStats:
        """The stage's stats, checked against ``limits`` when given."""
        return parallel_stats(self.ends, self.durations, self.wall_seconds, limits)


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
        if exchange is not None:
            self.attempt, self.earlier_exchanges = exchange.attempt, exchange.earlier_exchanges
        elif own_failure is not None:
            # A user function's skip, xfail or fail(), after its attempts' requests.
            self.earlier_exchanges = _outcome_exchanges(own_failure)
        self.exit_errors = exit_errors
        self.own_failure = own_failure
        self.result = result


def _parallel_failure(idx: int, exc: Exception, tolerated: Sequence[tuple[int, Exception]] = ()) -> str:
    """How a parallel stage reports the iteration ``idx`` that failed it.

    What its context managers raised on exit, below the first line, is
    labelled with the iteration, as a straggler's is (`_fold_in_secondary`):
    the lines left unlabelled are the stage's own. Then the iterations' own
    failures a ``min_success_ratio`` below 1 tolerated (``tolerated``, lowest
    index first), which only a stage an error on exit ended has: counted in
    its summary, they would otherwise go unexplained.
    """
    first, *rest = exc.failures if isinstance(exc, _IterationExitError) else [str(exc)]
    return "\n".join(
        [
            f"Parallel execution failed at iteration {idx}: {first}",
            *(f"Iteration {idx}: {line}" for line in rest),
            *failed_iterations([(i, str(failure)) for i, failure in tolerated], " tolerated by min_success_ratio"),
        ]
    )


def _result_exchanges(result: IterationResult) -> list[Exchange]:
    """The exchanges a successful iteration made on the wire, oldest first:
    its earlier attempts', then the one that succeeded."""
    return [*result.earlier_exchanges, *_wire_exchanges(result.request, result.response, result.started)]


def _failure_exchanges(failure: BaseException) -> list[Exchange]:
    """The exchanges an iteration that ended in ``failure`` made on the wire,
    oldest first: its earlier attempts', then the one its last attempt sent,
    if it sent one. A user function's pytest outcome carries them noted
    (`_outcome_exchanges`)."""
    if not isinstance(failure, StageExecutionError):
        return list(_outcome_exchanges(failure))
    exchanges = list(failure.earlier_exchanges)
    if failure.request is not None:
        exchanges.extend(_wire_exchanges(failure.request, failure.response, failure.started))
    return exchanges


def _first_sent(failures: Iterable[Exception]) -> StageExecutionError | None:
    """The first of ``failures`` whose last attempt sent a request, the one a
    report can show (`Carrier._record_exchanges`); None when none did (a
    template that did not render, a rate-limit slot that did not come)."""
    return next((failure for failure in failures if isinstance(failure, StageExecutionError) and failure.request is not None), None)


def _fold_in_secondary(idx: int, e: BaseException, results: list[IterationResult | None], exit_errors: list[str], exchanges: list[Exchange] | None = None) -> None:
    """Take in how iteration ``idx`` of a parallel stage ended when that is
    secondary to the stage's outcome: another iteration ended the stage first
    (`_PoolCancel`), and this one was cancelled by it or still running then.

    Its own failure, a skip, an xfail or a user function's pytest.fail()
    included, is dropped: re-raised, it would turn a failed stage into a
    skipped or xfailed one, or replace its failure message. Not what its
    context managers raised on exit: a commit that failed is a side effect the
    user must hear of, so it goes on ``exit_errors``, labelled with the
    iteration. Nor what it sent: a success its exit failed is still folded
    into ``results``, and a failure's exchanges go on ``exchanges`` (when the
    HAR file records every exchange; None otherwise), its every attempt's, one
    cancelled while waiting to retry included: its requests went on the wire.
    """
    if isinstance(e, _IterationExitError) and e.result is not None:
        results[idx] = e.result
    elif exchanges is not None:
        exchanges.extend(_failure_exchanges(e))
    if isinstance(e, _IterationExitError):
        exit_errors.extend(f"Iteration {idx}: {error}" for error in e.exit_errors)


class Carrier:
    """Base class of the generated scenario test classes; runs their stages."""

    # Placeholders only: create_test_class() overrides every one of these per
    # scenario (the mutable ones from `fresh_scenario_state`). This state must
    # stay at class level — the stage methods are classmethods sharing one
    # running context via `cls`.
    scenario: ClassVar[Scenario | None] = None
    scenario_dir: ClassVar[Path | None] = None
    # What a body schema's references to files are held to: pytest's rootdir
    # and the parent traversal depth, as for the scenario's $include.
    ref_bounds: ClassVar[ReferenceBounds] = UNBOUNDED
    client: ClassVar[httpx.Client | None] = None
    aborted: ClassVar[bool] = False
    last_request: ClassVar[httpx.Request | None] = None
    last_response: ClassVar[httpx.Response | None] = None
    last_exchanges: ClassVar[list[Exchange]] = []
    last_iterations_attempted: ClassVar[int] = 0
    last_shown_exchange_is_failed: ClassVar[bool] = False
    # (attempt, attempts) of the stage's retry the shown exchange came from,
    # when that was not its first attempt.
    last_shown_attempt: ClassVar[tuple[int, int] | None] = None
    # A parallel stage's stats, for its report's Parallel Summary section.
    last_parallel_stats: ClassVar[ParallelStats | None] = None
    record_all_exchanges: ClassVar[bool] = False
    # The report's rules (httpchain_redact_*), for failure messages that echo a
    # header's value: they are printed next to the report sections.
    redaction: ClassVar[Redaction] = DEFAULT_REDACTION
    global_context: ClassVar[ChainMap[str, Any]] = ChainMap()
    # Entered by the running stage outside its iterations (in `always_run`,
    # `substitutions`, `skip_if`, `parallel`): exited at its end. Each
    # iteration exits its own.
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

    @staticmethod
    def _skip_if_holds(template: str, local_context: Mapping[str, Any]) -> bool:
        """Evaluate a template ``skip_if`` against the stage-local context:
        what the request sees but the ``parallel.foreach`` parameters, since
        the stage decides once, before any iteration exists, and before
        ``response``.

        Unlike ``always_run``, the result must be a bool, as a verify
        expression's must: a skip is silent, so a value that only looks like
        a condition — a saved string ``"false"``, which is truthy, or the None
        a JMESPath save of a missing key leaves — fails the stage rather than
        skip it, or run it, by truthiness.
        """
        try:
            value = walk(template, local_context)
        except TemplatesError as e:
            raise StageExecutionError(f"Failed to evaluate skip_if template: {e}") from e
        if not isinstance(value, bool):
            # The type alone, never the value: `{{ token }}`, written for "skip
            # when there is a token", renders a credential, which the report
            # redacts in the request but a failure line would print as it is.
            got = "None" if value is None else type(value).__name__
            raise StageExecutionError(f"skip_if must evaluate to bool, got {got} from {template.strip()!r}")
        return value

    @classmethod
    def execute_stage(cls, stage: Stage, fixture_kwargs: dict[str, Any]) -> None:
        """Execute one stage end to end.

        Gates on the abort/``always_run`` flow, layers the stage context, skips
        the stage when its ``skip_if`` holds, runs the iteration matrix, each
        iteration making the attempts its ``retry`` allows, holds a parallel
        stage's stats to its ``thresholds``, and on success commits the
        iterations' saves (`_merged_saves`), and the stats under
        ``stats_as``, as a new global-context layer. A ``min_success_ratio``
        below 1 lets it run on after failed iterations, whose saves are none.
        A failure is reported via ``pytest.fail`` and commits no saves, so the
        context never carries a timing-dependent subset; nor does a skip. The
        report hook owns chain-abort classification because only pytest's final
        report knows whether xfail/strict and setup/teardown made the item a
        genuine failure. However the stage ends, the context managers its
        factory fixtures returned are exited before this returns (each
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
        cls.last_shown_attempt = None
        cls.last_parallel_stats = None

        # Ahead of the always_run machinery: a failed initialization leaves no
        # context and no client, so every stage skips.
        if cls._init_failed is not None:
            pytest.skip(reason=f"Scenario initialization failed: {cls._init_failed}")

        outcome: BaseException | None = None
        saves: dict[str, Any] | None = None
        # What the context managers of a parallel stage's iterations still
        # running when another one ended the stage raised on exit (`_run_iterations`).
        iteration_exit_errors: list[str] = []
        # And what they sent, for the HAR file when it records every exchange.
        secondary_exchanges: list[Exchange] = []
        # A parallel stage's iterations as they end, once they start.
        run: _ParallelRun | None = None
        try:
            stage_fixtures = cls._build_stage_fixtures(fixture_kwargs)

            if cls.aborted and not cls._resolve_always_run(stage, stage_fixtures):
                pytest.skip(reason="Flow aborted")

            # A literal reads no context, so the stage skips before the
            # scenario initializes or its own substitutions call anything, as
            # a literal always_run is read without initializing.
            if stage.skip_if is True:
                pytest.skip(reason="skip_if: true")

            cls._ensure_initialized()

            stage_context = stage_start_context(cls.global_context, stage_fixtures)
            stage_substitutions = process_substitutions(stage.substitutions, stage_context)
            local_context = with_stage_substitutions(stage_context, stage_substitutions)

            # Guarded: context dumps carry every saved value (auth tokens
            # included) and pytest attaches captured logs to failure reports.
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("global context on start: %s", _context_dump(cls.global_context))
                logger.debug("local context on start: %s", _context_dump(local_context))

            # A skip is no failure: the report hook leaves the chain healthy.
            # Like any outcome it is held back (`outcome`), so what the
            # substitutions or the template entered is exited before it is
            # raised, and the stage commits no saves.
            if isinstance(stage.skip_if, str) and cls._skip_if_holds(stage.skip_if, local_context):
                pytest.skip(reason=f"skip_if: {stage.skip_if.strip()}")

            parallel_config: ParallelConfig | None = _render_declared(stage.parallel, local_context, "parallel") if stage.parallel else None
            iteration_substitutions = cls._build_iteration_substitutions(parallel_config, cls.max_parallel_iterations)

            total = len(iteration_substitutions)
            if total == 0:
                # The models reject the static empty cases, but a template- or
                # $ref-sourced config can still resolve to empty at runtime.
                raise StageExecutionError("Parallel configuration produced zero iterations; foreach/repeat must yield at least one item")
            # Before any request: a switch the stage cannot read must fail it
            # before its iterations send anything, not once they all have.
            collect_saves = _parallel_flag("collect_saves", parallel_config.collect_saves) if parallel_config is not None else False
            limits = _threshold_limits(parallel_config.thresholds) if parallel_config is not None else {}
            # Against the same context as the parallel config, and before any
            # request too: a setting the stage cannot use fails it now.
            retry = _retry_policy(_render_declared(stage.retry, local_context, "retry")) if stage.retry is not None else _NO_RETRY

            if parallel_config is not None:
                run = _ParallelRun(stage.name, total, tolerate=limits.get("min_success_ratio", 1) < 1)
            results, first_error = cls._run_iterations(stage, local_context, iteration_substitutions, parallel_config, iteration_exit_errors, retry, secondary_exchanges, run)
            completed = [iter_result for iter_result in results if iter_result is not None]

            if first_error is None:
                # No iteration ended the stage: every one ran to its end, and
                # the thresholds are checked against what they measured. One
                # not met fails the stage, listing the failures it ran on after.
                stats = cls.last_parallel_stats = run.stats(limits) if run is not None else None
                violated = stats.violated if stats is not None else ()
                tolerated = run.failures() if run is not None else []
                # The report shows the first failure that sent a request when
                # too few passed, as a stage failing at an iteration shows
                # that one's, and when none passed, which leaves no passed
                # exchange to show. The others' exchanges went on the wire too.
                shown = _first_sent(failure for _, failure in tolerated) if not completed or any(check.name == "min_success_ratio" for check in violated) else None
                if cls.record_all_exchanges and run is not None:
                    secondary_exchanges.extend(run.failure_exchanges(but=shown))
                cls._record_exchanges(completed, failed=shown, attempted=total, attempts=retry.attempts, secondary=secondary_exchanges)
                if violated:
                    raise StageExecutionError(threshold_failure(violated, [(idx, str(failure)) for idx, failure in tolerated]))
                # Every iteration passed, or failed and was tolerated, so
                # `results` holds a result for each that passed, None for the
                # others: entry i of a collected list is iteration i's.
                assert len(completed) + len(tolerated) == total, "a stage whose iterations all ended has every one's result or failure"
                saves = _merged_saves(
                    [iter_result.saved_context if iter_result is not None else {} for iter_result in results],
                    collect_saves,
                    # Sorted: the order a stage commits in never depends on a set's.
                    sorted(saved_in_response(stage)) if tolerated else (),
                )
                if parallel_config is not None and parallel_config.stats_as is not None:
                    assert stats is not None, "a parallel stage has stats"
                    saves = _with_stats(saves, parallel_config.stats_as, stats, saved_in_response(stage))
            else:
                idx, exc = first_error
                if run is not None:
                    # Ended at an iteration: no threshold is checked.
                    cls.last_parallel_stats = run.stats()
                cls._record_exchanges(completed, failed=exc, attempted=total, attempts=retry.attempts, secondary=secondary_exchanges)
                # Label the failure as parallel only when the user asked for
                # parallel, else a plain stage failure would be misreported.
                if parallel_config is not None:
                    raise StageExecutionError(_parallel_failure(idx, exc, run.failures() if run is not None else ())) from exc
                raise exc

        except _STAGE_OUTCOMES as e:
            # Held back, a skip or an xfail too: exiting the context managers may fail the stage.
            outcome = e
            if run is not None and run.began and cls.last_parallel_stats is None:
                # A user function's skip, xfail or fail() ended the stage at
                # an iteration: the summary says how the others ended.
                cls.last_parallel_stats = run.stats()
            if not isinstance(e, _STAGE_FAILURE_EXCEPTIONS) and (exchanges := _outcome_exchanges(e)):
                # A user function's outcome, once requests had gone on the
                # wire, its iteration's and a parallel stage's other
                # iterations' (`_run_iterations`): the HAR file records them.
                cls.last_exchanges = list(exchanges if cls.record_all_exchanges else exchanges[-1:])
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
            # What a user function's pytest.skip/xfail/fail raised, unchanged
            # but for the attempts a fail() on a later one counts.
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
    def _record_exchanges(cls, completed: list[IterationResult], failed: Exception | None, attempted: int, attempts: int = 1, secondary: Sequence[Exchange] = ()) -> None:
        """Record this stage's HTTP exchanges for the report and the HAR file.

        ``last_request``/``last_response`` are the one exchange the report shows:
        the failing iteration's when it carries request info (its response may
        legitimately be None), else the last completed one: each an iteration's
        last attempt, which ``last_shown_attempt`` numbers out of the
        ``attempts`` the stage's retry allows when it was not the first.
        ``last_exchanges`` holds every iteration, every attempt of each, only
        when HAR output is on, so an ordinary run never retains more than one
        response per stage: the completed iterations', then ``secondary``,
        those of the iterations that failed or were cancelled after the one
        that failed the stage (`_fold_in_secondary`), then that one's.
        """
        failed_request, failed_response = (failed.request, failed.response) if isinstance(failed, StageExecutionError) else (None, None)

        exchanges = [exchange for r in completed for exchange in _result_exchanges(r)]
        exchanges.extend(secondary)
        if failed is not None:
            exchanges.extend(_failure_exchanges(failed))
        if not cls.record_all_exchanges:
            exchanges = exchanges[-1:]

        cls.last_exchanges = exchanges
        cls.last_iterations_attempted = attempted

        shown_attempt = 1
        if failed_request is not None:
            assert isinstance(failed, StageExecutionError), "only a stage failure carries a request"
            cls.last_request, cls.last_response = failed_request, failed_response
            cls.last_shown_exchange_is_failed = True
            shown_attempt = failed.attempt
        elif completed:
            cls.last_request, cls.last_response = completed[-1].request, completed[-1].response
            shown_attempt = completed[-1].attempt
        cls.last_shown_attempt = (shown_attempt, attempts) if shown_attempt > 1 else None

    @classmethod
    def _run_iterations(
        cls,
        stage: Stage,
        local_context: ChainMap[str, Any],
        iteration_substitutions: list[dict[str, Any]],
        parallel_config: ParallelConfig | None,
        exit_errors: list[str],
        retry: _RetryPolicy = _NO_RETRY,
        secondary_exchanges: list[Exchange] | None = None,
        run: _ParallelRun | None = None,
    ) -> tuple[list[IterationResult | None], tuple[int, Exception] | None]:
        """Run the iterations and return ``(results_by_index, first_error)``.

        One iteration runs inline; many run in a pool capped at
        ``max_concurrency`` with an optional global rate limiter, which every
        attempt of an iteration (``retry``) takes a slot of. The first
        iteration to end in anything but success cancels the pool, and its
        outcome is the stage's whenever it is read (`_PoolCancel`), unless
        another iteration raises an interrupt (or, read first, a plugin bug).
        Where ``run`` tolerates failures (a ``min_success_ratio`` below 1), an
        iteration's own failure goes on ``run.tolerated`` instead, whenever it
        is read, and the others run on; a pytest outcome, an exit error and an
        interrupt still end the stage. ``run`` records how each iteration
        ended, and the wall time.
        What the context managers of the iterations it cancelled or that were
        still running then raise on exit goes on ``exit_errors``, however this
        returns or raises, and what those that failed sent goes on
        ``secondary_exchanges`` when the HAR file records every exchange (see
        `_fold_in_secondary`). So does what the failures ``run`` tolerated
        sent when an exit error or a pytest outcome ends the stage: when
        every iteration ended, the stage takes those in itself, once it knows
        which one its report shows.
        """
        total = len(iteration_substitutions)
        # Only for the HAR file: an ordinary run keeps one exchange per stage.
        secondary = secondary_exchanges if cls.record_all_exchanges else None
        results: list[IterationResult | None] = [None] * total
        first_error: tuple[int, Exception] | None = None
        limiter: Limiter | None = None

        try:
            # A single iteration's only attempt cannot block on a fresh bucket,
            # so without retries the rate-limiting settings have nothing to do.
            max_rate_limit_delay: float = 60
            if parallel_config is not None and (total > 1 or retry.attempts > 1):
                # Guarded rather than cast: the config arrives walk()-resolved,
                # and a resolved value can still be unusable (see `_setting_number`).
                # None is "never declared" here: `_render_declared` already
                # refused a template that rendered to None.
                calls_per_sec = _parallel_int("calls_per_sec", parallel_config.calls_per_sec) if parallel_config.calls_per_sec is not None else None
                max_rate_limit_delay = _parallel_number("max_rate_limit_delay", parallel_config.max_rate_limit_delay)
                limiter = Limiter(Rate(calls_per_sec, Duration.SECOND)) if calls_per_sec is not None else None

            if total == 1:
                if run is not None:
                    run.begin()
                try:
                    results[0] = cls._run_iteration(stage, local_context, iteration_substitutions[0], limiter, max_rate_limit_delay, retry=retry, run=run)
                except _STAGE_FAILURE_EXCEPTIONS as e:
                    if run is not None and run.tolerates(e):
                        run.run_on(0, e)
                    else:
                        first_error = (0, e)
            else:
                # Only a parallel config can yield more than one iteration:
                # `_build_iteration_substitutions(None, ...)` returns exactly one.
                assert parallel_config is not None, "more than one iteration implies a parallel config"
                max_concurrency = _parallel_int("max_concurrency", parallel_config.max_concurrency)

                workers = min(max_concurrency, total)
                cancel = _PoolCancel()
                futures: dict[Future[IterationResult], int] = {}
                read: set[int] = set()
                if run is not None:
                    run.begin()
                try:
                    with ThreadPoolExecutor(max_workers=workers) as executor:
                        for idx, iter_vars in enumerate(iteration_substitutions):
                            future = executor.submit(cls._run_iteration, stage, local_context, iter_vars, limiter, max_rate_limit_delay, cancel, idx, retry, run)
                            futures[future] = idx

                        try:
                            for future in as_completed(futures):
                                idx = futures[future]
                                read.add(idx)
                                try:
                                    results[idx] = future.result()
                                except _STAGE_OUTCOMES as e:
                                    if run is not None and run.tolerates(e):
                                        # An iteration's own failure, which its
                                        # worker did not cancel the pool for
                                        # (`_run_iteration`): the stage runs on,
                                        # and counts it at the end.
                                        run.run_on(idx, e)
                                        continue
                                    # Its worker claimed the pool already; this
                                    # only asks whether it was the first to.
                                    if not cancel.claim(idx):
                                        # Another iteration ended the stage first:
                                        # its outcome, still to be read, is the one
                                        # to report.
                                        _fold_in_secondary(idx, e, results, exit_errors, secondary)
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
                    cls._fold_in_unread(futures, read, results, exit_errors, secondary, run)
        except (pytest.skip.Exception, pytest.fail.Exception) as outcome:
            # A user function's outcome that ended the stage, once the
            # iterations still running were taken in: what the others sent
            # went on the wire too, the failures tolerated included, and the
            # stage records the outcome's.
            if secondary is not None:
                tolerated = run.failure_exchanges() if run is not None else []
                _note_outcome_exchanges(outcome, [*(exchange for r in results if r is not None for exchange in _result_exchanges(r)), *secondary, *tolerated])
            raise
        finally:
            if run is not None:
                run.end()
            # Every Limiter owns a daemon thread that lives until closed.
            if limiter is not None:
                limiter.close()

        if first_error is not None and secondary is not None and run is not None:
            # An iteration's exits ended the stage: what the failures it
            # tolerated sent went on the wire too. When every iteration ended,
            # the stage takes them in itself, once it knows which the report shows.
            secondary.extend(run.failure_exchanges())
        return results, first_error

    @staticmethod
    def _fold_in_unread(
        futures: Mapping[Future[IterationResult], int],
        read: set[int],
        results: list[IterationResult | None],
        exit_errors: list[str],
        exchanges: list[Exchange] | None = None,
        run: _ParallelRun | None = None,
    ) -> None:
        """Take in the iterations whose results the pool's loop did not read:
        those still running when one failed the stage (or skipped it, or the
        run was interrupted), which ended once the pool shut down.

        Their requests hit the wire, so each success is folded into
        ``results`` for the HAR; each failure is secondary (`_fold_in_secondary`),
        what it sent going on ``exchanges``, but one ``run`` tolerates
        (`_ParallelRun.tolerates`), which goes with the others it tolerated.
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
                if run is not None and run.tolerates(e):
                    run.run_on(idx, e)
                else:
                    _fold_in_secondary(idx, e, results, exit_errors, exchanges)
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
        retry: _RetryPolicy = _NO_RETRY,
        run: _ParallelRun | None = None,
    ) -> IterationResult:
        """Execute one iteration, its every attempt (`_execute_attempts`), then
        exit the context managers it entered, in the thread that ran it, and
        record how it ended on ``run``, a parallel stage's.

        A parallel stage's iteration runs in a pool worker, and a thread-bound
        context manager (a ``sqlite3`` connection) can only be exited there;
        exiting at the iteration's end also releases what it holds (a
        transaction, a pooled connection) to the iterations still running. An
        exit error fails the iteration like any failure (an `_IterationExitError`,
        which keeps its exchange).

        What the attempts of a ``retry`` enter is exited once the last one is
        done, not attempt by attempt: an exit is not told whether the attempt
        failed, so exiting early could not undo a failed attempt any better,
        and an exit error would then have to fail an iteration whose next
        attempt might pass, or go unreported. What an iteration enters, it
        exits when it ends.

        Iteration ``idx`` of a parallel stage that does not succeed cancels the
        rest of the pool itself (`_PoolCancel.claim`), and before its exits
        when it failed on its own: the stage's thread only learns of the
        failure once they are done, and an exit can take a while (a rollback),
        during which the queued iterations would go on sending. Where ``run``
        tolerates failures, a failure of its own (`_ParallelRun.tolerates`) does
        not cancel the pool, but a pytest outcome and an exit error still do.
        """
        entered = _IterationContextManagers()
        token = _ITERATION_CONTEXT_MANAGERS.set(entered)
        try:
            try:
                result = cls._execute_attempts(stage, local_context, iter_vars, retry, limiter, max_rate_limit_delay, cancel)
            except BaseException as e:
                if cancel is not None and not (run is not None and run.tolerates(e)):
                    cancel.claim(idx)
                if not isinstance(e, _STAGE_OUTCOMES):
                    # An interrupt or a plugin bug is what gets reported.
                    _log_exit_errors(_exit_context_managers(entered.close()))
                    raise
                exit_errors = _exit_context_managers(entered.close())
                if not exit_errors:
                    if run is not None:
                        run.record(idx, e)
                    raise
                if cancel is not None:
                    # Never tolerated: the stage must fail for what its exits did.
                    cancel.claim(idx)
                exit_failure = _IterationExitError(exit_errors, own_failure=e)
                if run is not None:
                    run.record(idx, exit_failure)
                raise exit_failure from e
            exit_errors = _exit_context_managers(entered.close())
            if exit_errors:
                if cancel is not None:
                    cancel.claim(idx)
                exit_failure = _IterationExitError(exit_errors, result=result)
                if run is not None:
                    run.record(idx, exit_failure)
                raise exit_failure
            if run is not None:
                run.record(idx, None, result)
            return result
        finally:
            _ITERATION_CONTEXT_MANAGERS.reset(token)

    @classmethod
    def _execute_attempts(
        cls,
        stage: Stage,
        local_context: ChainMap[str, Any],
        iter_vars: Mapping[str, Any],
        retry: _RetryPolicy,
        limiter: Limiter | None = None,
        max_rate_limit_delay: float = 60,
        cancel: threading.Event | None = None,
    ) -> IterationResult:
        """Make an iteration's attempts: the first, and after each that fails
        in a way ``retry`` retries (`_RetryPolicy.retries`), a wait of its
        schedule and another, until one succeeds or none is left.

        Each attempt is `_execute_single_iteration` whole: it renders the
        request anew (a fresh ``uuid4()``), takes a rate-limit slot, sends,
        and runs every response step in an iteration context of its own, so a
        failed attempt's saves go with it. The iteration ends as its last
        attempt did, a failure saying how many attempts ran (`_after_attempts`).
        A failure no attempt could change (a template, a validation error, a
        user function that crashed) ends it at once, and so does a user
        function's pytest outcome, carrying the earlier attempts' exchanges
        (`_outcome_after_attempts`). A wait ends early when ``cancel`` is set,
        and the iteration then fails as cancelled, carrying its attempts'
        exchanges as any failure after the first attempt does.

        The earlier attempts' exchanges are kept for the HAR file only when it
        records every exchange: they are real traffic, but an ordinary run
        must not hold a response per attempt.
        """
        earlier: list[Exchange] = []
        # Each attempt's time in the HTTP client, which the iteration's
        # duration sums: waits for a rate-limit slot and to retry left out.
        spent: list[float] = []
        waits = retry.waits()
        attempt = 1
        while True:
            try:
                result = cls._execute_single_iteration(stage, local_context, iter_vars, limiter, max_rate_limit_delay, cancel, exchange_seconds=spent)
            except _STAGE_FAILURE_EXCEPTIONS as e:
                failure = e
            except (pytest.skip.Exception, pytest.fail.Exception) as e:
                # A user function ended the stage, which no attempt retries.
                if attempt > 1:
                    _outcome_after_attempts(e, attempt, tuple(earlier))
                raise
            else:
                return replace(result, attempt=attempt, earlier_exchanges=tuple(earlier), duration=math.fsum(spent))
            # Outside the handler: the failure raised is the attempt's own, with
            # nothing handled behind it.
            if not (retry.retries(failure) and (wait := next(waits, None)) is not None):
                raise failure if attempt == 1 else _after_attempts(failure, attempt, tuple(earlier))
            if cls.record_all_exchanges and failure.request is not None:
                earlier.extend(_wire_exchanges(failure.request, failure.response, failure.started))
            logger.info("Stage '%s': attempt %d of %d failed, next in %gs: %s", stage.name, attempt, retry.attempts, wait, str(failure).partition("\n")[0])
            if not _wait_to_retry(wait, cancel):
                # Secondary to the stage's failure, whose message is the one
                # shown, but with the attempts made, which went on the wire:
                # the stage folds them into the HAR file (`_fold_in_secondary`).
                cancelled = _Cancelled("Iteration cancelled while waiting to retry: the stage already failed")
                cancelled.earlier_exchanges = tuple(earlier)
                raise cancelled
            attempt += 1

    @classmethod
    def _execute_single_iteration(
        cls,
        stage: Stage,
        local_context: ChainMap[str, Any],
        iter_vars: Mapping[str, Any],
        limiter: Limiter | None = None,
        max_rate_limit_delay: float = 60,
        cancel: threading.Event | None = None,
        exchange_seconds: list[float] | None = None,
    ) -> IterationResult:
        """One attempt of an iteration: resolve the request against the
        iteration context, take a rate-limit slot, send it, and run the
        response steps in order.

        ``cancel`` is the pool-wide cancellation signal: once another iteration
        fails (or the run is interrupted), in-flight iterations stop before
        sending rather than adding side-effecting traffic to a failed stage.
        The time the request spends in the HTTP client, sent or failed, is
        appended to ``exchange_seconds``.
        """
        if cancel is not None and cancel.is_set():
            raise _Cancelled("Iteration cancelled: the stage already failed")

        iter_context = iteration_context(local_context, iter_vars)

        # Rendering re-validates the model it substitutes into, so no further
        # model_validate is needed here.
        request_model = _render_declared(stage.request, iter_context, "request", RequestError)
        request_kwargs = build_request_kwargs(request_model, cls.scenario_dir, cls._client_config, cls.redaction, declared_auth=stage.request.auth)

        if limiter is not None and not cls._acquire_rate_slot(limiter, max_rate_limit_delay, cancel):
            if cancel is not None and cancel.is_set():
                raise _Cancelled("Iteration cancelled while waiting for a rate-limit slot: the stage already failed")
            raise RequestError(f"Rate limit exceeded: could not acquire a request slot within {max_rate_limit_delay}s")

        if cancel is not None and cancel.is_set():
            raise _Cancelled("Iteration cancelled: the stage already failed")

        # Stamped after the acquire, so it reflects when the request went on the
        # wire; this feeds the HAR entry's startedDateTime.
        started = datetime.now(UTC)
        sending = time.perf_counter()
        try:
            response = cls._execute_http_request(request_kwargs)
        except StageExecutionError as e:
            e.started = started
            raise
        finally:
            if exchange_seconds is not None:
                exchange_seconds.append(time.perf_counter() - sending)

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
                        save_model = step.save if isinstance(step.save, SubstitutionsSave) else _render_save(step.save, step_context)
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
                                # The scenario's own, which no attempt changes.
                                raise SaveError(str(promoted), retryable=False) from None
                        iter_context = with_saves(iter_context, step_saved)
                        saved_context.update(step_saved)

                    case VerifyStep():
                        # Each check's value is rendered on its own, all before the
                        # first check, through the guard (`_verify_renderer`): a
                        # template that fails is one failure among the step's.
                        process_verify(step.verify, response, cls.scenario_dir, cls.redaction, render=_verify_renderer(step.verify, step_context), ref_bounds=cls.ref_bounds)

                    case _:
                        raise RuntimeError(f"Unhandled response step: {type(step).__name__}")
        except StageExecutionError as e:
            e.request = response.request
            e.response = response
            e.started = started
            raise
        except (TemplatesError, ValidationError) as e:
            raise StageExecutionError(str(e), request=response.request, response=response, started=started) from e
        except (pytest.skip.Exception, pytest.fail.Exception) as e:
            # A user function's skip, xfail or fail(): the request it answered
            # went on the wire.
            _note_outcome_exchanges(e, _wire_exchanges(response.request, response, started))
            raise

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
