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
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import httpx
import pytest
from pydantic import ValidationError
from pyrate_limiter import Duration, Limiter, Rate

from pytest_httpchain.errors import RequestError, SaveError, StageExecutionError
from pytest_httpchain.models import (
    CombinationsParameter,
    IndividualParameter,
    ParallelConfig,
    ParallelForeachConfig,
    ParallelRepeatConfig,
    SaveStep,
    Scenario,
    Stage,
    SubstitutionsSave,
    VerifyStep,
)
from pytest_httpchain.request_builder import build_client_kwargs, build_request_kwargs
from pytest_httpchain.response_steps import check_rendered_assertions, process_save, process_verify
from pytest_httpchain.scoping import (
    RESPONSE_META_NAME,
    base_global_context,
    iteration_context,
    response_step_context,
    stage_start_context,
    with_saves,
    with_stage_substitutions,
)
from pytest_httpchain.templates import TemplatesError, walk
from pytest_httpchain.utils import process_substitutions
from pytest_httpchain.warnings import ScenarioValidationWarning

logger = logging.getLogger(__name__)

# An expected stage failure (bad scenario, failed verification, unreachable
# server) rather than a plugin bug: both execution paths turn these into a clean
# pytest failure instead of a traceback.
_STAGE_FAILURE_EXCEPTIONS = (StageExecutionError, TemplatesError, ValidationError)

# Makes _ensure_initialized's check-then-act atomic under thread-based runners.
_INIT_LOCK = threading.Lock()


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


def fresh_scenario_state() -> dict[str, Any]:
    """The per-scenario mutable class state, in its pristine form.

    Single source of truth for the state a scenario must own rather than share:
    ``factory.create_test_class`` seeds every subclass with it and
    `Carrier.teardown_class` re-applies it. New per-scenario state goes here.
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
        "_initialized": False,
        "_init_failed": None,
    }


@dataclass(slots=True, frozen=True)
class IterationResult:
    """A successful stage iteration. ``started`` is when the request went on the
    wire, which is what HAR waterfalls are built from."""

    saved_context: dict[str, Any]
    request: httpx.Request
    response: httpx.Response
    started: datetime


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
    last_exchanges: ClassVar[list[tuple[httpx.Request, httpx.Response | None, datetime | None]]] = []
    last_iterations_attempted: ClassVar[int] = 0
    last_shown_exchange_is_failed: ClassVar[bool] = False
    record_all_exchanges: ClassVar[bool] = False
    global_context: ClassVar[ChainMap[str, Any]] = ChainMap()
    active_context_managers: ClassVar[list[AbstractContextManager]] = []
    max_parallel_iterations: ClassVar[int] = 10_000
    _initialized: ClassVar[bool] = False
    _init_failed: ClassVar[str | None] = None
    _context_resolved_at_collection: ClassVar[bool] = False

    @classmethod
    def _ensure_initialized(cls) -> None:
        """Resolve scenario substitutions and build the shared client on first use.

        Deferred from collection so ``--collect-only`` and IDE discovery neither
        run user code nor allocate a client per scenario. Runs at most once per
        scenario, success or failure: side-effectful substitutions and auth are
        never re-invoked, and after a failure every later stage skips.
        """
        with _INIT_LOCK:
            if cls._initialized:
                return
            if cls._init_failed is not None:
                raise StageExecutionError(f"Failed to initialize scenario: {cls._init_failed}")
            scenario = cls.scenario
            assert scenario is not None, "create_test_class() seeds cls.scenario"
            try:
                if not cls._context_resolved_at_collection:
                    cls.global_context = base_global_context(process_substitutions(scenario.substitutions))

                resolved_ssl = walk(scenario.ssl, cls.global_context)
                resolved_auth = walk(scenario.auth, cls.global_context) if scenario.auth else None
                cls.client = httpx.Client(**build_client_kwargs(resolved_ssl, resolved_auth, cls.scenario_dir))
            except Exception as e:
                cls._init_failed = str(e)
                raise StageExecutionError(f"Failed to initialize scenario: {e}") from e
            cls._initialized = True

    @classmethod
    def _resolve_always_run(cls, stage: Stage, stage_fixtures: dict[str, Any]) -> bool:
        """Resolve ``always_run``, evaluating a template form against the
        stage-start context (stage substitutions do not exist yet)."""
        if isinstance(stage.always_run, bool):
            return stage.always_run
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
        the item a genuine failure.
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

        failure_reason: str | None = None
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

            parallel_config: ParallelConfig | None = walk(stage.parallel, local_context) if stage.parallel else None
            iteration_substitutions = cls._build_iteration_substitutions(parallel_config, cls.max_parallel_iterations)

            total = len(iteration_substitutions)
            if total == 0:
                # The models reject the static empty cases, but a template- or
                # $ref-sourced config can still resolve to empty at runtime.
                raise StageExecutionError("Parallel configuration produced zero iterations; foreach/repeat must yield at least one item")

            results, first_error = cls._run_iterations(stage, local_context, iteration_substitutions, parallel_config)
            completed = [iter_result for iter_result in results if iter_result is not None]

            if first_error is None:
                cls._record_exchanges(completed, failed=None, attempted=total)
                all_saves: dict[str, Any] = {}
                for iter_result in completed:
                    all_saves.update(iter_result.saved_context)
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("updates for global context: %s", _context_dump(all_saves))
                cls.global_context = with_saves(cls.global_context, all_saves)
            else:
                idx, exc = first_error
                cls._record_exchanges(completed, failed=exc, attempted=total)
                # Label the failure as parallel only when the user asked for
                # parallel, else a plain stage failure would be misreported.
                if parallel_config is not None:
                    raise StageExecutionError(f"Parallel execution failed at iteration {idx}: {exc}") from exc
                raise exc

        except _STAGE_FAILURE_EXCEPTIONS as e:
            failure_reason = str(e)

        # Deliberately outside the handler: raising there would set
        # `Failed.__context__` to the original exception, and pytest's
        # repr_excinfo walks the whole __cause__/__context__ chain even under
        # pytrace=False — printing the one message 2-4 times, since plugin
        # errors and httpx transport errors are themselves chained.
        if failure_reason is not None:
            pytest.fail(reason=failure_reason, pytrace=False)

    @classmethod
    def _build_stage_fixtures(cls, fixture_kwargs: dict[str, Any]) -> dict[str, Any]:
        """Wrap callable (factory) fixtures so templates can invoke them; plain
        values pass through."""
        return {name: cls._wrap_factory_fixture(value) if callable(value) and not inspect.isclass(value) else value for name, value in fixture_kwargs.items()}

    @staticmethod
    def _build_iteration_substitutions(parallel_config: "ParallelConfig | None", max_parallel_iterations: int) -> list[dict[str, Any]]:
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
                repeat_total = int(repeat_count)
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
                            steps.append((param_name, individual[param_name]))
                        case CombinationsParameter(combinations=combinations):
                            steps.append((None, cast(list[Any], combinations)))
                        case _:
                            raise RuntimeError(f"Unhandled foreach step: {type(step).__name__}")
                check_cap(math.prod(len(values) for _, values in steps))

                for param_name, values in steps:
                    additions = [{param_name: value} if param_name is not None else (vars(value) if isinstance(value, SimpleNamespace) else value) for value in values]
                    # Clause order is load-bearing: new values outer, accumulated
                    # dicts inner. Swapping them changes the iteration order.
                    iteration_substitutions = [{**existing, **addition} for addition in additions for existing in iteration_substitutions]
            case _:
                raise RuntimeError(f"Unhandled parallel config: {type(parallel_config).__name__}")
        return iteration_substitutions

    @classmethod
    def _record_exchanges(cls, completed: list["IterationResult"], failed: Exception | None, attempted: int) -> None:
        """Record this stage's HTTP exchanges for the report and the HAR file.

        ``last_request``/``last_response`` are the one exchange the report shows:
        the failing iteration's when it carries request info (its response may
        legitimately be None), else the last completed one. ``last_exchanges``
        holds every iteration only when HAR output is on, so an ordinary run
        never retains more than one response per stage.
        """
        failed_request, failed_response, failed_started = (failed.request, failed.response, failed.started) if isinstance(failed, StageExecutionError) else (None, None, None)

        exchanges: list[tuple[httpx.Request, httpx.Response | None, datetime | None]] = []
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
            cls.last_request, cls.last_response = exchanges[-1][0], exchanges[-1][1]

    @classmethod
    def _run_iterations(
        cls,
        stage: Stage,
        local_context: ChainMap[str, Any],
        iteration_substitutions: list[dict[str, Any]],
        parallel_config: "ParallelConfig | None",
    ) -> tuple[list[IterationResult | None], tuple[int, Exception] | None]:
        """Run the iterations and return ``(results_by_index, first_error)``.

        One iteration runs inline; many run in a pool capped at
        ``max_concurrency`` with an optional global rate limiter. The first
        expected failure cancels the pool.
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
                    results[0] = cls._execute_single_iteration(stage, local_context, iteration_substitutions[0])
                except _STAGE_FAILURE_EXCEPTIONS as e:
                    first_error = (0, e)
            else:
                # Only a parallel config can yield more than one iteration:
                # `_build_iteration_substitutions(None, ...)` returns exactly one.
                assert parallel_config is not None, "more than one iteration implies a parallel config"
                # The numeric fields are `PositiveInt | NumberOrTemplate`, but the
                # config arrives walk()-resolved.
                max_concurrency = int(parallel_config.max_concurrency)
                calls_per_sec = int(parallel_config.calls_per_sec) if parallel_config.calls_per_sec else None
                max_rate_limit_delay = float(parallel_config.max_rate_limit_delay)
                limiter = Limiter(Rate(calls_per_sec, Duration.SECOND)) if calls_per_sec else None

                workers = min(max_concurrency, total)
                cancel = threading.Event()
                futures: dict[Future[IterationResult], int] = {}
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    for idx, iter_vars in enumerate(iteration_substitutions):
                        future = executor.submit(cls._execute_single_iteration, stage, local_context, iter_vars, limiter, max_rate_limit_delay, cancel)
                        futures[future] = idx

                    try:
                        for future in as_completed(futures):
                            idx = futures[future]
                            try:
                                results[idx] = future.result()
                            except _STAGE_FAILURE_EXCEPTIONS as e:
                                first_error = (idx, e)
                                cancel.set()
                                executor.shutdown(wait=False, cancel_futures=True)
                                break
                    except BaseException:
                        # KeyboardInterrupt or a plugin bug: without cancelling,
                        # the executor exit would run every queued iteration to
                        # completion, making a runaway parallel stage unstoppable.
                        cancel.set()
                        executor.shutdown(wait=False, cancel_futures=True)
                        raise

                # In-flight iterations that completed after the early exit hit
                # the wire; fold their results in so the HAR reflects actual
                # traffic. Their failures are secondary to first_error.
                if first_error is not None:
                    for future, idx in futures.items():
                        if results[idx] is None and future.done() and not future.cancelled():
                            try:
                                results[idx] = future.result()
                            except Exception:
                                # Sibling failures: first_error already represents
                                # the stage's failure.
                                pass
        finally:
            # Every Limiter owns a daemon thread that lives until closed.
            if limiter is not None:
                limiter.close()

        return results, first_error

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
            raise RequestError(f"HTTP request timed out: {e}", request=_error_request(e)) from e
        except httpx.ConnectError as e:
            raise RequestError(f"HTTP connection error: {e}", request=_error_request(e)) from e
        except httpx.HTTPError as e:
            raise RequestError(f"HTTP request failed: {e}", request=_error_request(e)) from e
        except Exception as e:
            raise RequestError(f"Unexpected error during HTTP request: {e}", request=_error_request(e)) from e

    @staticmethod
    def _acquire_rate_slot(limiter: Limiter, timeout: float, cancel: threading.Event | None) -> bool:
        """Poll for a rate-limit slot so a pool-wide cancellation interrupts the
        wait; a blocking ``try_acquire`` would pin the thread (and delay the
        stage's failure report) for up to the full timeout."""
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

        # walk() re-validates the model it substitutes into, so no further
        # model_validate is needed here.
        request_model = walk(stage.request, iter_context)
        request_kwargs = build_request_kwargs(request_model, cls.scenario_dir)

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
                        save_model = step.save if isinstance(step.save, SubstitutionsSave) else walk(step.save, step_context)
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
                        verify_model = walk(step.verify, step_context)
                        # Compared against the pre-walk step, the only place both
                        # forms are in scope: process_verify sees the rendered
                        # model alone and cannot tell an absent assertion from
                        # one a template rendered away.
                        check_rendered_assertions(step.verify, verify_model)
                        process_verify(verify_model, response, cls.scenario_dir)

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
    def _wrap_factory_fixture(cls, fixture: Callable) -> Callable:
        """Wrap a callable fixture so a context-manager result is entered and
        registered for LIFO teardown.

        Each call opens a resource, so the wrapped value must be invoked once per
        instance needed.
        """

        def wrapped(*args, **kwargs):
            result = fixture(*args, **kwargs)

            if isinstance(result, AbstractContextManager):
                value = result.__enter__()
                cls.active_context_managers.append(result)
                return value

            return result

        return wrapped

    @classmethod
    def teardown_class(cls) -> None:
        while cls.active_context_managers:
            ctx = cls.active_context_managers.pop()
            try:
                ctx.__exit__(None, None, None)
            except Exception as e:
                logger.error(f"Error while cleaning up context manager fixture: {e}")

        if cls.client is not None:
            cls.client.close()

        # Reset all per-run state so a re-run of this class (e.g. a rerun plugin)
        # actually re-executes. maps[-1] is the pristine scenario context: saves
        # only ever prepend layers.
        for name, value in fresh_scenario_state().items():
            setattr(cls, name, value)
        cls.global_context = base_global_context(cls.global_context.maps[-1])


def _context_dump(data: Mapping[str, Any]) -> str:
    """Render a context for DEBUG logging; a saved value can be anything, and
    logging must never break a stage."""
    try:
        return json.dumps(dict(data), indent=2, default=str)
    except Exception as e:
        return f"<unserializable context: {e}>"
