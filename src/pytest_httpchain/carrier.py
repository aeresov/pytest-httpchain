"""Runtime execution engine: the base class of every generated scenario test class.

``factory.create_test_class`` builds one ``test NN - <stage name>`` method per
stage on a fresh `Carrier` subclass, which the plugin's collection hooks keep
contiguous and in stage order; the per-scenario state lives at the *class*
level, so stage methods share one running context across the chain while
scenarios stay isolated from each other.

This module owns sequencing only. What the individual pieces mean lives next
door: ``rendering`` (a declared model rendered against the context),
``stage_settings`` (a stage's ``parallel`` and ``retry`` blocks once
rendered: the iteration matrix, the retry policy), ``request_builder``
(models -> httpx arguments), ``response_steps`` (one verify/save step),
``scoping`` (context layering), ``connections`` (a connection stage's
sessions, readers and waits).
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
from collections.abc import Callable, Collection, Hashable, Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import AbstractContextManager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, TypeGuard

import httpx
import pytest
from pydantic import ValidationError
from pyrate_limiter import Duration, Limiter, Rate

from pytest_httpchain.body_schema import UNBOUNDED, ReferenceBounds
from pytest_httpchain.connections import ConnectionExchange, Connections, ExchangeRecord, Miss, expect_failure, not_open_failure
from pytest_httpchain.errors import RequestError, SaveError, StageExecutionError, VerificationError
from pytest_httpchain.har_writer import Exchange
from pytest_httpchain.models import (
    ClientConfig,
    ConnectionConfig,
    ConnectionStep,
    ParallelConfig,
    SaveStep,
    Scenario,
    Stage,
    SubstitutionsSave,
    VarsNamespace,
    VerifyStep,
    validate_rendered_scenario_auth,
)
from pytest_httpchain.models.types import convert_namespace_to_dict
from pytest_httpchain.parallel_stats import IterationEnd, ParallelStats, failed_iterations, parallel_stats, threshold_failure
from pytest_httpchain.redaction import DEFAULT_REDACTION, Redaction
from pytest_httpchain.rendering import listed_expectations, render_declared, render_save, rendered_match, verify_renderer
from pytest_httpchain.request_builder import build_client_kwargs, build_request_kwargs
from pytest_httpchain.response_steps import (
    NO_MESSAGE,
    Match,
    MessageSubject,
    ResponseCodec,
    Subject,
    message_matches,
    process_save,
    process_verify,
)
from pytest_httpchain.scoping import (
    CONNECTION_BINDINGS,
    MESSAGE_META_NAME,
    RESPONSE_META_NAME,
    SENT_NAME,
    DefinedNames,
    base_global_context,
    connection_bindings,
    connection_step_context,
    extract_template_variables,
    iteration_context,
    response_step_context,
    saved_in_response,
    stage_start_context,
    with_saves,
    with_stage_substitutions,
)
from pytest_httpchain.stage_settings import (
    NO_RETRY,
    RateSettings,
    RetryPolicy,
    build_iteration_substitutions,
    parallel_flag,
    rate_settings,
    retry_policy,
    setting_number,
    threshold_limits,
)
from pytest_httpchain.templates import TemplatesError, walk
from pytest_httpchain.utils import INTERRUPTS as _INTERRUPTS
from pytest_httpchain.utils import describe_exception as _describe_exception
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
# is exited (`utils.INTERRUPTS`).


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
        request = e.request  # ty: ignore[missing-attribute]
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
                # The frame that called `Carrier.execute_stage`, past
                # `Carrier._settle_iterations`, which calls this.
                stacklevel=4,
            )
        except ScenarioValidationWarning as promoted:
            # Promoted by filterwarnings=error: a stage failure, so that
            # reporting and the abort engage.
            raise StageExecutionError(str(promoted)) from None
    return {**saves, name: stats.saved()}


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


# What a connection stage binds over its response steps, by what shadows a
# save of that name there, in the validator's words (`scoping.CONNECTION_BINDINGS`).
_CONNECTION_RESERVED = {name: f"{what} (HTTPCHAIN045)" for name, what in CONNECTION_BINDINGS.items()}


def _message_miss(match: Any, rendered: Mapping[str, Any] | None, redaction: Redaction, listed: Sequence[Any], message: dict[str, Any]) -> Miss | None:
    """The predicate a connection stage's wait takes (`connections.MatchPredicate`):
    `response_steps.message_matches`' verdict on ``message``.

    A miss's reason is for the report and the timeout message only, so it is
    shown through ``redaction`` here (`Redaction.message_text`): what it quotes
    of the message under a listed key, and what an expectation on a listed key
    (``body.token``) expected (``listed``, `listed_expectations`, the same
    for every message of the wait), which a template may have rendered from a
    secret. An unusable operand raises instead of returning a miss; its
    exception's text is shown through the same redaction before it escapes."""
    try:
        verdict = message_matches(match, rendered, message)
    except VerificationError as error:
        error.args = (redaction.message_text(str(error), message, *listed),)
        raise
    if isinstance(verdict, Match):
        return None
    return Miss(redaction.message_text(verdict.reason, message, *listed), verdict.error)


def fresh_chain_state() -> dict[str, Any]:
    """The mutable class state one chain of a scenario owns, in its pristine form.

    A scenario runs as one chain, or as one per param of a parametrized fixture
    (`Carrier.begin_chain`), and each chain starts from this: no abort, no
    exchanges, no client yet, no connection open. The saves live in
    ``global_context``, reset alongside it. What a scenario resolves once for
    all its chains is in `fresh_scenario_state`.
    """
    return {
        "client": None,
        "connections": Connections(),
        "last_connection_exchange": None,
        "_connection_record": None,
        "_connection_params_context": None,
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


@dataclass(frozen=True, slots=True)
class _StagePlan:
    """What a stage runs, resolved before any request (`Carrier._stage_plan`):
    its ``parallel`` config rendered (None without one), each iteration's
    substitutions (`build_iteration_substitutions`), whether it collects its
    saves (``collect_saves``), the limits its stats are held to
    (`threshold_limits`), its retry policy, and how its iterations are paced
    (`rate_settings`)."""

    parallel: ParallelConfig | None
    iterations: list[dict[str, Any]]
    collect_saves: bool
    limits: dict[str, float]
    retry: RetryPolicy
    rate: RateSettings


@dataclass(slots=True, frozen=True)
class IterationResult:
    """A successful stage iteration: its attempt that succeeded. ``started`` is
    when the request went on the wire, which is what HAR waterfalls are built
    from. ``attempt`` is which attempt of the stage's ``retry`` it is, and
    ``earlier_exchanges`` the exchanges of those before it, when the HAR file
    records them (`Carrier._execute_attempts`). ``duration`` is the seconds
    its attempts' requests spent in the HTTP client, the iteration's latency
    in a parallel stage's stats (see `parallel_stats`). A connection stage's
    attempt makes no HTTP exchange: its ``request``, ``response`` and
    ``started`` are None."""

    saved_context: dict[str, Any]
    request: httpx.Request | None = None
    response: httpx.Response | None = None
    started: datetime | None = None
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
    its earlier attempts', then the one that succeeded, if it sent a request
    (a connection stage's sends none)."""
    if result.request is None:
        return list(result.earlier_exchanges)
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
    # The chain's connections, by name (`connections.Connections`).
    connections: ClassVar[Connections] = Connections()
    # What a connection stage did, for its report: a snapshot taken once its
    # step ran, before any close.
    last_connection_exchange: ClassVar[ConnectionExchange | None] = None
    # The record that snapshot was taken from: its last attempt's.
    _connection_record: ClassVar[ExchangeRecord | None] = None
    # Whether a passing stage's report sections are printed too (-rP, an
    # xdist worker, a stage marked xfail, which fails by passing), which the
    # plugin says for each item: a connection stage that passes then takes
    # the full snapshot a failing one always does. Otherwise it copies
    # nothing and does not call the adapter's debug_info(), for a section
    # nobody reads; a stage that fails after its attempt, or closes its
    # connection, takes it in full then (`execute_stage`).
    snapshot_passed_exchanges: ClassVar[bool] = True
    # What a connection's params render against: the scenario's substitutions
    # and fixtures and the saves so far, never the stage's own names, since
    # the connection outlives the stage. Set by a connection stage.
    _connection_params_context: ClassVar[ChainMap[str, Any] | None] = None
    # Each fixture the running stage's item requests, by name, with its
    # scope, set by the plugin as the item is set up: a fixture a
    # connection's params read must outlive the stage that opens it.
    fixture_scopes: ClassVar[Mapping[str, str]] = {}
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

                resolved_ssl = render_declared(scenario.ssl, cls.global_context, "ssl")
                resolved_client = render_declared(scenario.client, cls.global_context, "client")
                resolved_auth = render_declared(scenario.auth, cls.global_context, "auth", validate=validate_rendered_scenario_auth) if scenario.auth is not None else None
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
        the stage when its ``skip_if`` holds, resolves its settings
        (`_stage_plan`), runs the iteration matrix, each iteration making the
        attempts its ``retry`` allows, holds a parallel stage's stats to its
        ``thresholds``, and on success commits the iterations' saves
        (`_settle_iterations`, `_merged_saves`), and the stats under
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
        cls.last_connection_exchange = None
        cls._connection_record = None
        cls._connection_params_context = None

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
        # The connection the stage closes once it has run, whatever its
        # outcome but a skip (`close: true`).
        closing: str | None = None
        try:
            stage_fixtures = cls._build_stage_fixtures(fixture_kwargs)

            if cls.aborted and not cls._resolve_always_run(stage, stage_fixtures):
                pytest.skip(reason="Flow aborted")
            # An always_run stage after a failure can still use an open
            # connection (a farewell message), but never connects.
            if cls.aborted and stage.connection is not None and not cls.connections.is_open(stage.connection.name):
                pytest.skip(reason=f"Flow aborted, and connection '{stage.connection.name}' is not open")

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

            if stage.connection is not None:
                if stage.connection.close:
                    closing = stage.connection.name
                if stage.parallel is not None:
                    raise StageExecutionError(f"Stage '{stage.name}': parallel is not supported on a connection stage")
                cls._connection_params_context = cls._params_context(stage_fixtures)

            plan = cls._stage_plan(stage, local_context)
            if plan.parallel is not None:
                run = _ParallelRun(stage.name, len(plan.iterations), tolerate=plan.limits.get("min_success_ratio", 1) < 1)
            results, first_error = cls._run_iterations(stage, local_context, plan, iteration_exit_errors, secondary_exchanges, run)
            saves = cls._settle_iterations(stage, plan, run, results, first_error, secondary_exchanges)

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
        cls._finish_connection_stage(stage, outcome, closing, exit_errors)

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

    @classmethod
    def _stage_plan(cls, stage: Stage, local_context: ChainMap[str, Any]) -> _StagePlan:
        """What the stage runs (`_StagePlan`), resolved against the
        stage-local context before any request: its ``parallel`` config
        rendered and expanded into the iterations, ``collect_saves``, the
        thresholds, its ``retry`` rendered and resolved, then the rate and
        concurrency settings, in that order. A setting the stage cannot use
        fails it before its iterations send anything, not once they all have."""
        parallel_config: ParallelConfig | None = render_declared(stage.parallel, local_context, "parallel") if stage.parallel else None
        iteration_substitutions = build_iteration_substitutions(parallel_config, cls.max_parallel_iterations)

        total = len(iteration_substitutions)
        if total == 0:
            # The models reject the static empty cases, but a template- or
            # $ref-sourced config can still resolve to empty at runtime.
            raise StageExecutionError("Parallel configuration produced zero iterations; foreach/repeat must yield at least one item")
        # Before any request: a switch the stage cannot read must fail it
        # before its iterations send anything, not once they all have.
        collect_saves = parallel_flag("collect_saves", parallel_config.collect_saves) if parallel_config is not None else False
        limits = threshold_limits(parallel_config.thresholds) if parallel_config is not None else {}
        # Against the same context as the parallel config, and before any
        # request too: a setting the stage cannot use fails it now.
        retry = retry_policy(render_declared(stage.retry, local_context, "retry")) if stage.retry is not None else NO_RETRY
        rate = rate_settings(parallel_config, total, retry)
        return _StagePlan(parallel=parallel_config, iterations=iteration_substitutions, collect_saves=collect_saves, limits=limits, retry=retry, rate=rate)

    @classmethod
    def _settle_iterations(
        cls,
        stage: Stage,
        plan: _StagePlan,
        run: _ParallelRun | None,
        results: list[IterationResult | None],
        first_error: tuple[int, Exception] | None,
        secondary_exchanges: list[Exchange],
    ) -> dict[str, Any]:
        """What the stage commits once its iterations have run
        (`_run_iterations`), or the failure it ends in.

        Either way its exchanges are recorded for the report and the HAR file
        (`_record_exchanges`, ``secondary_exchanges`` among them), and a
        parallel stage's stats go on ``last_parallel_stats`` first. When no
        iteration ended the stage, the stats are held to its thresholds, and
        the stage commits its iterations' saves (`_merged_saves`), with the
        stats under ``stats_as``. When one did (``first_error``), its failure
        is raised, as a parallel one where the stage is parallel
        (`_parallel_failure`).
        """
        total = len(plan.iterations)
        completed = [iter_result for iter_result in results if iter_result is not None]

        if first_error is None:
            # No iteration ended the stage: every one ran to its end, and
            # the thresholds are checked against what they measured. One
            # not met fails the stage, listing the failures it ran on after.
            stats = cls.last_parallel_stats = run.stats(plan.limits) if run is not None else None
            violated = stats.violated if stats is not None else ()
            tolerated = run.failures() if run is not None else []
            # The report shows the first failure that sent a request when
            # too few passed, as a stage failing at an iteration shows
            # that one's, and when none passed, which leaves no passed
            # exchange to show. The others' exchanges went on the wire too.
            shown = _first_sent(failure for _, failure in tolerated) if not completed or any(check.name == "min_success_ratio" for check in violated) else None
            if cls.record_all_exchanges and run is not None:
                secondary_exchanges.extend(run.failure_exchanges(but=shown))
            cls._record_exchanges(completed, failed=shown, attempted=total, attempts=plan.retry.attempts, secondary=secondary_exchanges)
            if violated:
                raise StageExecutionError(threshold_failure(violated, [(idx, str(failure)) for idx, failure in tolerated]))
            # Every iteration passed, or failed and was tolerated, so
            # `results` holds a result for each that passed, None for the
            # others: entry i of a collected list is iteration i's.
            assert len(completed) + len(tolerated) == total, "a stage whose iterations all ended has every one's result or failure"
            saves = _merged_saves(
                [iter_result.saved_context if iter_result is not None else {} for iter_result in results],
                plan.collect_saves,
                # Sorted: the order a stage commits in never depends on a set's.
                sorted(saved_in_response(stage)) if tolerated else (),
            )
            if plan.parallel is not None and plan.parallel.stats_as is not None:
                assert stats is not None, "a parallel stage has stats"
                saves = _with_stats(saves, plan.parallel.stats_as, stats, saved_in_response(stage))
            return saves

        idx, exc = first_error
        if run is not None:
            # Ended at an iteration: no threshold is checked.
            cls.last_parallel_stats = run.stats()
        cls._record_exchanges(completed, failed=exc, attempted=total, attempts=plan.retry.attempts, secondary=secondary_exchanges)
        # Label the failure as parallel only when the user asked for
        # parallel, else a plain stage failure would be misreported.
        if plan.parallel is not None:
            raise StageExecutionError(_parallel_failure(idx, exc, run.failures() if run is not None else ())) from exc
        raise exc

    @classmethod
    def _finish_connection_stage(cls, stage: Stage, outcome: BaseException | None, closing: str | None, exit_errors: list[str]) -> None:
        """A connection stage's last steps, once the context managers the
        stage entered have exited (``exit_errors``, what their exits raised),
        for a stage that ended in ``outcome`` (None: it succeeded): its report
        gets what arrived just after the deadline of a wait that timed out and
        failed it, and its full snapshot where it is reported, before the
        connection it closes (``closing``, ``close: true``) is closed, a
        close's failure going on ``exit_errors``. A stage that ran no
        connection step has none of these to do."""
        # A wait that timed out and failed the stage: what arrived just after
        # its deadline goes in the report. Asked once, here, where the failure
        # is final, and before any close: not by every attempt.
        if isinstance(outcome, _STAGE_FAILURE_EXCEPTIONS) and (record := cls._connection_record) is not None and (exchange := cls.last_connection_exchange) is not None:
            if late := record.late():
                cls.last_connection_exchange = replace(exchange, late=late)
        # A stage whose attempt passed took its snapshot light, for a section
        # nobody reads. One that fails yet (an exit that raised) or still may
        # (its close) is reported: its snapshot is taken in full now, the
        # adapter's debug_info() asked before the close.
        if (record := cls._connection_record) is not None and (exchange := cls.last_connection_exchange) is not None and not exchange.full:
            if (exit_errors or closing is not None) and not isinstance(outcome, pytest.skip.Exception):
                cls.last_connection_exchange = record.freeze()
        # With the exits' errors, and as they do: a close that fails fails the
        # stage, its own failure first, and no saves commit.
        if closing is not None and not isinstance(outcome, pytest.skip.Exception):
            close_error = cls.connections.close(closing, stage.name)
            if close_error is not None:
                exit_errors.append(close_error)

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
        elif isinstance(failed, StageExecutionError) and cls.last_connection_exchange is not None:
            # A connection stage's failure carries no request: its report shows
            # the exchange of the attempt that failed (`last_connection_exchange`).
            shown_attempt = failed.attempt
        cls.last_shown_attempt = (shown_attempt, attempts) if shown_attempt > 1 else None

    @classmethod
    def _run_iterations(
        cls,
        stage: Stage,
        local_context: ChainMap[str, Any],
        plan: _StagePlan,
        exit_errors: list[str],
        secondary_exchanges: list[Exchange] | None = None,
        run: _ParallelRun | None = None,
    ) -> tuple[list[IterationResult | None], tuple[int, Exception] | None]:
        """Run the ``plan``'s iterations and return ``(results_by_index, first_error)``.

        One iteration runs inline; many run in a pool capped at
        ``max_concurrency`` with an optional global rate limiter, which every
        attempt of an iteration (the plan's ``retry``) takes a slot of. The
        first iteration to end in anything but success cancels the pool, and
        its outcome is the stage's whenever it is read (`_PoolCancel`), unless
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
        iteration_substitutions, retry, rate = plan.iterations, plan.retry, plan.rate
        total = len(iteration_substitutions)
        # Only for the HAR file: an ordinary run keeps one exchange per stage.
        secondary = secondary_exchanges if cls.record_all_exchanges else None
        results: list[IterationResult | None] = [None] * total
        first_error: tuple[int, Exception] | None = None
        limiter: Limiter | None = None

        try:
            # The plan resolved a rate only where one applies (`rate_settings`).
            limiter = Limiter(Rate(rate.calls_per_sec, Duration.SECOND)) if rate.calls_per_sec is not None else None

            if total == 1:
                if run is not None:
                    run.begin()
                try:
                    results[0] = cls._run_iteration(
                        stage, local_context, iteration_substitutions[0], limiter=limiter, max_rate_limit_delay=rate.max_rate_limit_delay, retry=retry, run=run
                    )
                except _STAGE_FAILURE_EXCEPTIONS as e:
                    if run is not None and run.tolerates(e):
                        run.run_on(0, e)
                    else:
                        first_error = (0, e)
            else:
                # Only a parallel config can yield more than one iteration:
                # `build_iteration_substitutions(None, ...)` returns exactly one,
                # and the plan resolved its max_concurrency (`rate_settings`).
                assert rate.max_concurrency is not None, "more than one iteration implies a parallel config"

                workers = min(rate.max_concurrency, total)
                cancel = _PoolCancel()
                futures: dict[Future[IterationResult], int] = {}
                read: set[int] = set()
                if run is not None:
                    run.begin()
                try:
                    with ThreadPoolExecutor(max_workers=workers) as executor:
                        for idx, iter_vars in enumerate(iteration_substitutions):
                            future = executor.submit(
                                cls._run_iteration,
                                stage,
                                local_context,
                                iter_vars,
                                limiter=limiter,
                                max_rate_limit_delay=rate.max_rate_limit_delay,
                                cancel=cancel,
                                idx=idx,
                                retry=retry,
                                run=run,
                            )
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
        *,
        limiter: Limiter | None = None,
        max_rate_limit_delay: float = 60,
        cancel: _PoolCancel | None = None,
        idx: int = 0,
        retry: RetryPolicy = NO_RETRY,
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
                result = cls._execute_attempts(stage, local_context, iter_vars, retry=retry, limiter=limiter, max_rate_limit_delay=max_rate_limit_delay, cancel=cancel)
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
        *,
        retry: RetryPolicy,
        limiter: Limiter | None = None,
        max_rate_limit_delay: float = 60,
        cancel: threading.Event | None = None,
    ) -> IterationResult:
        """Make an iteration's attempts: the first, and after each that fails
        in a way ``retry`` retries (`RetryPolicy.retries`), a wait of its
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
                result = cls._execute_single_iteration(
                    stage, local_context, iter_vars, limiter=limiter, max_rate_limit_delay=max_rate_limit_delay, cancel=cancel, exchange_seconds=spent
                )
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
        *,
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
        appended to ``exchange_seconds``. A connection stage's attempt is its
        connection step instead (`_execute_connection_attempt`).
        """
        if cancel is not None and cancel.is_set():
            raise _Cancelled("Iteration cancelled: the stage already failed")

        iter_context = iteration_context(local_context, iter_vars)
        if stage.request is None:
            return cls._execute_connection_attempt(stage, iter_context)

        # Rendering re-validates the model it substitutes into, so no further
        # model_validate is needed here.
        request_model = render_declared(stage.request, iter_context, "request", RequestError)
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
            response.extensions["httpchain_response_codec"] = stage.response_codec
        except StageExecutionError as e:
            e.started = started
            raise
        finally:
            if exchange_seconds is not None:
                exchange_seconds.append(time.perf_counter() - sending)

        try:
            response_meta = _response_meta(response)
            saved_context = cls._run_response_steps(
                stage,
                iter_context,
                response,
                functools.partial(response_step_context, response_meta=response_meta),
                {RESPONSE_META_NAME: "the response metadata namespace (HTTPCHAIN027)"},
                stage.response_codec,
            )
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
    def _run_response_steps(
        cls,
        stage: Stage,
        iter_context: ChainMap[str, Any],
        subject: Subject,
        step_context: Callable[[ChainMap[str, Any]], ChainMap[str, Any]],
        reserved: Mapping[str, str],
        codec: ResponseCodec = "auto",
    ) -> dict[str, Any]:
        """Run the stage's response steps in order on ``subject``, the HTTP
        response or a connection stage's message, each in ``step_context`` of
        the iteration context and the saves of the steps before it: what they
        saved. ``reserved`` names what the stage's kind layers over the steps,
        by what shadows a save of that name."""
        saved_context: dict[str, Any] = {}
        for step in stage.response:
            context = step_context(iter_context)
            match step:
                case SaveStep():
                    # A SubstitutionsSave renders its own entries strictly in
                    # order inside process_save; pre-walking it here would
                    # evaluate later entries before earlier ones' names exist
                    # and re-evaluate already-rendered values — so response-
                    # derived text containing '{{ }}' would be executed as an
                    # expression.
                    save_model = step.save if isinstance(step.save, SubstitutionsSave) else render_save(step.save, context)
                    step_saved = process_save(save_model, subject, context, codec=codec)
                    # The static checks cannot see dynamically produced keys,
                    # so the shadowing is surfaced here too.
                    for name in sorted(reserved.keys() & step_saved.keys()):
                        try:
                            warnings.warn(
                                ScenarioValidationWarning(f"Save step produced the reserved name '{name}': inside response steps it is shadowed by {reserved[name]}"),
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
                    # first check, through the guard (`verify_renderer`): a
                    # template that fails is one failure among the step's.
                    process_verify(
                        step.verify,
                        subject,
                        cls.scenario_dir,
                        cls.redaction,
                        render=verify_renderer(step.verify, context),
                        ref_bounds=cls.ref_bounds,
                        codec=codec,
                    )

                case _:
                    raise RuntimeError(f"Unhandled response step: {type(step).__name__}")
        return saved_context

    @classmethod
    def _params_context(cls, stage_fixtures: Mapping[str, Any]) -> ChainMap[str, Any]:
        """What a connection's ``params`` render against: the scenario's
        substitutions, the saves so far and the scenario's own fixtures, never
        the stage's fixtures, parameters or substitutions, which the
        connection outlives."""
        scenario_fixtures = cls.scenario.fixtures if cls.scenario is not None else []
        return stage_start_context(cls.global_context, {name: stage_fixtures[name] for name in scenario_fixtures if name in stage_fixtures})

    @classmethod
    def _execute_connection_attempt(cls, stage: Stage, iter_context: ChainMap[str, Any]) -> IterationResult:
        """One attempt of a connection stage: open, drain, send, expect, then
        the response steps on the message it took, in that order (closing is
        the stage's, once its attempts are done: `execute_stage`).

        It opens the connection on ``open: true``, and at the first stage of
        the chain that names it while it was never opened, unless that stage
        would only close it again (``close`` without a send or an expect):
        one never opened has nothing to close, and the stage does not
        connect for that. One a stage closed, or whose
        reopening failed, is not open, and a stage that does not open it
        fails, as no attempt changes that; so does one that sends on a
        connection whose reader has ended. A reader that ended since this
        attempt opened the connection is the connection ending under the
        stage, which a stage with ``open: true`` retries by connecting again.
        An ``expect`` takes the oldest matching message, after the stage's
        send if it sends (`connections._Session.expect`). The response steps
        see it as ``message`` (or, without an ``expect``, what an ``open``'s
        ``connect()`` returned) and what ``send()`` returned as ``sent``.

        Whatever happens, what the step did is kept for the report
        (``last_connection_exchange``), before any close: in full when the
        attempt failed or passing stages are reported too
        (``snapshot_passed_exchanges``), and otherwise without copying a
        message or calling the adapter's ``debug_info()``.
        """
        step = stage.connection
        assert step is not None, "a stage without a request has a connection step"
        config = cls.scenario.connections.get(step.name) if cls.scenario is not None else None
        if config is None:
            raise StageExecutionError(f"Stage '{stage.name}': connection '{step.name}' is not declared in the scenario's connections")
        cls._connection_record = record = ExchangeRecord(step.name, config.adapter)
        failed = True
        try:
            # What the response steps see: `message`, `sent`.
            bound: dict[str, Any] = {}
            subject = NO_MESSAGE
            session: Any = None
            # A stage that would only close a connection never opened again
            # does not connect for that.
            only_closes = step.close and step.send is None and step.expect is None
            if step.open or (not only_closes and cls.connections.never_opened(step.name)):
                session, connected = cls._open_connection(stage, step.name, config)
                record.opened, record.connected = True, connected
                if step.open and step.expect is None:
                    subject = MessageSubject(connected)
                    bound[MESSAGE_META_NAME] = connected
            elif cls.connections.never_opened(step.name):
                pass  # nothing to close
            elif (why := cls.connections.not_open(step.name)) is not None:
                raise not_open_failure(stage.name, step.name, why)
            else:
                session = cls.connections.session(step.name)
                assert session is not None, "a connection opened and not closed has a session"
            record.session = session
            end = session.end if session is not None else None
            # An end the reader came to before this stage: the connection was
            # not open for it. One since this attempt's own open is the
            # connection ending under the stage.
            ended_before = end is not None and not record.opened
            if end is not None and step.send is not None:
                session.observed = True
                if ended_before:
                    raise not_open_failure(stage.name, step.name, end.describe())
                gone = RequestError(f"Stage '{stage.name}': connection '{step.name}' {end.describe()} before the stage could send", retryable=step.open)
                gone.__cause__ = end.error
                raise gone

            if step.drain and session is not None:
                session.drain()

            after: int | None = None
            since = time.monotonic()
            if step.send is not None:
                record.sent = declared = cls._rendered_send(stage, step, iter_context)
                # A copy: an adapter that changes the message it is given (a
                # sequence number written into it) must not change the
                # scenario's own, which the next attempt sends again.
                outgoing = convert_namespace_to_dict(declared)
                # Before the send: a reply can arrive before send() returns.
                after = session.mark()
                since = time.monotonic()
                record.sent_return = bound[SENT_NAME] = session.send(outgoing)

            if step.expect is not None:
                subject = cls._expect(stage, step, session, iter_context, bound, record, after=after, since=since, ended_before=ended_before)
                if subject.present:
                    bound[MESSAGE_META_NAME] = subject.message

            try:
                bindings = connection_bindings(**bound)
                reserved = {name: what for name, what in _CONNECTION_RESERVED.items() if name in bindings}
                saved_context = cls._run_response_steps(stage, iter_context, subject, functools.partial(connection_step_context, bindings=bindings), reserved)
            except (TemplatesError, ValidationError) as e:
                raise StageExecutionError(str(e)) from e
            except RecursionError as e:
                raise StageExecutionError(f"Stage '{stage.name}': the message is nested too deeply for templates to read: {e}") from e
            failed = False
        finally:
            cls.last_connection_exchange = record.freeze(full=failed or cls.snapshot_passed_exchanges)
        return IterationResult(saved_context=saved_context)

    @classmethod
    def _open_connection(cls, stage: Stage, name: str, config: ConnectionConfig) -> tuple[Any, dict[str, Any] | None]:
        """Open connection ``name`` for ``stage``, its ``params`` rendered
        (`_params_context`): its session, and what ``connect()`` returned.
        Resources its templates enter belong to that session, and a failed
        open exits them before the next attempt.

        A fixture the params read must be class-scoped or wider, or the
        connection would outlive it; the plugin gives each item's fixture
        scopes (``fixture_scopes``), and without them nothing is checked.
        """
        context = cls._connection_params_context
        assert context is not None, "execute_stage sets a connection stage's params context before its attempts"
        scenario_fixtures = frozenset(cls.scenario.fixtures) if cls.scenario is not None else frozenset()
        read = extract_template_variables(config.params, defined=DefinedNames(scenario_fixtures, scenario_fixtures)) & scenario_fixtures
        if narrow := sorted(fixture for fixture in read if cls.fixture_scopes.get(fixture) == "function"):
            raise StageExecutionError(
                f"Stage '{stage.name}': connection '{name}' params read fixture '{narrow[0]}', which is function-scoped: "
                f"the connection outlives the stage that opens it, so a fixture its params read must be class-scoped or wider"
            )
        cls.connections.prepare_open(name)
        # Factories called by these templates create resources for the session,
        # which outlives the iteration opening it. Capture their exits separately
        # from the iteration's and transfer them only once the open succeeds.
        resources = _IterationContextManagers()
        try:
            try:
                token = _ITERATION_CONTEXT_MANAGERS.set(resources)
                try:
                    params = convert_namespace_to_dict(walk(config.params, context))
                finally:
                    _ITERATION_CONTEXT_MANAGERS.reset(token)
                # A timeout is used only by connect(), so factories providing
                # it keep their usual iteration lifetime.
                connect_timeout = walk(config.connect_timeout, context)
            except (TemplatesError, ValueError) as e:
                raise StageExecutionError(f"Stage '{stage.name}': connection '{name}' params did not render: {e}") from e
            timeout = setting_number(f"connections.{name}.connect_timeout", connect_timeout)
            session, connected = cls.connections.open(name, config.adapter, params, timeout, config.inbox_limit, label=cls.__name__)
        except BaseException as e:
            errors = _exit_context_managers(resources.close())
            if isinstance(e, _STAGE_OUTCOMES):
                if errors:
                    raise _IterationExitError(errors, own_failure=e) from e
            else:
                _log_exit_errors(errors)
            raise
        session.set_cleanup(lambda: _exit_context_managers(resources.close()))
        return session, connected

    @staticmethod
    def _rendered_send(stage: Stage, step: ConnectionStep, context: Mapping[str, Any]) -> dict[str, Any]:
        """The step's ``send``, rendered through the guard (`render_declared`)
        and validated again as a message, alone: the step is never rendered
        whole."""
        declared = ConnectionStep.model_construct(_fields_set={"name", "send"}, name=step.name, send=step.send)
        rendered = render_declared(declared, context, "connection").send
        if not isinstance(rendered, dict):
            raise StageExecutionError(f"Stage '{stage.name}': connection.send must render to an object, got {type(rendered).__name__}")
        return rendered

    @classmethod
    def _expect(
        cls,
        stage: Stage,
        step: ConnectionStep,
        session: Any,
        iter_context: ChainMap[str, Any],
        bound: Mapping[str, Any],
        record: ExchangeRecord,
        *,
        after: int | None,
        since: float,
        ended_before: bool,
    ) -> MessageSubject:
        """Wait as the step's ``expect`` says, its timeout and its match
        object's values rendered with ``sent`` in scope (the match string is
        never rendered): the message it took, the end an ``expect.closed`` saw
        (``{error, detail}``), or none for an absent one that passed."""
        expect = step.expect
        assert expect is not None, "only a step that expects waits"
        context = connection_step_context(iter_context, connection_bindings(**{name: bound[name] for name in (SENT_NAME,) if name in bound}))
        timeout = setting_number("connection.expect.timeout", walk(expect.timeout, context) if isinstance(expect.timeout, str) else expect.timeout)
        rendered = rendered_match(expect.match, context, cls.redaction) if isinstance(expect.match, dict) else None
        mode = "closed" if expect.closed else "absent" if expect.absent else "message"
        record.match, record.timeout, record.mode = expect.match, timeout, mode
        predicate = functools.partial(_message_miss, expect.match, rendered, cls.redaction, listed_expectations(rendered, cls.redaction)) if expect.match is not None else None
        record.since = since
        record.outcome = outcome = session.expect(predicate, timeout, after=after, since=since, mode=mode)
        sent = step.send is not None
        failure = expect_failure(
            outcome,
            stage=stage.name,
            connection=step.name,
            match=expect.match,
            timeout=timeout,
            mode=mode,
            sent=sent,
            reopens=step.open,
            ended_before=ended_before,
            redaction=cls.redaction,
        )
        if failure is not None:
            raise failure
        if outcome.result == "taken":
            assert outcome.taken is not None, "a wait that took a message has it"
            return MessageSubject(outcome.taken.message)
        if outcome.result == "closed":
            assert outcome.end is not None, "a wait that saw the end has it"
            return MessageSubject(outcome.end.as_message())
        return NO_MESSAGE

    @classmethod
    def _wrap_factory_fixture(cls, name: str, fixture: Callable) -> Callable:
        """Wrap a callable fixture so a context-manager result is entered and
        registered to be exited: by the running iteration when it ends, or
        outside an iteration by the stage when it ends. A connection's params
        temporarily use their own registry, transferred to its session.

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

        Then the chain's connections are closed, all together
        (`connections.close_all`), and the client. Each step is guarded, so
        one that fails leaves the rest, and the state's reset, to run: an
        error is logged, and an interrupt re-raised once all is done.
        """
        interrupts: list[BaseException] = []

        def guarded(what: str, action: Callable[[], object]) -> None:
            try:
                action()
            except _INTERRUPTS as e:
                interrupts.append(e)
            except BaseException as e:
                logger.error("%s at the end of the chain failed: %s", what, _describe_exception(e))

        guarded("Exiting the context managers", lambda: _log_exit_errors(_exit_context_managers(cls.active_context_managers)))
        guarded("Closing the connections", cls.connections.close_all)
        if (client := cls.client) is not None:
            guarded("Closing the HTTP client", client.close)

        for name, value in fresh_chain_state().items():
            setattr(cls, name, value)
        # maps[-1] is the pristine scenario context: saves only ever prepend layers.
        cls.global_context = base_global_context(cls.global_context.maps[-1])
        if interrupts:
            raise interrupts[0]


def _context_dump(data: Mapping[str, Any]) -> str:
    """Render a context for DEBUG logging; a saved value can be anything, and
    logging must never break a stage."""
    try:
        return json.dumps(dict(data), indent=2, default=str)
    except Exception as e:
        return f"<unserializable context: {e}>"
