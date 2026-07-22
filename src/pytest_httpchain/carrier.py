"""Runtime execution engine for pytest-httpchain scenarios.

`Carrier` is the base class of the dynamic test classes that
``factory.create_test_class`` builds at collection time — one
``test NN - <stage name>`` method per stage, ordered by the ``order(i)``
marker so the stages run as a chain. Each scenario gets its own subclass; the
per-scenario mutable state (``client``, ``global_context``, ``aborted``,
``last_request``/``last_response``, ``active_context_managers``) lives at the
*class* level and is overridden in the subclass dict, so the stage methods —
which are classmethods operating on ``cls`` — share one running context across
the chain while different scenarios stay isolated from each other.

Per stage, `Carrier.execute_stage` gates on the abort/``always_run`` flow,
layers fixtures and substitutions over the global context, expands any
``parallel`` config into iterations (sequential or thread-pooled, optionally
rate limited), executes the HTTP request via httpx, runs the response
verify/save steps, and on full success commits the collected saves as a new
global-context layer for later stages. Expected failures (bad scenario, failed
verification, unreachable server) are surfaced via ``pytest.fail(pytrace=False)``
so the report stays clean rather than dumping an internal traceback.
"""

import base64
import inspect
import json
import logging
import re
import threading
import warnings
from collections import ChainMap
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import httpx
import jmespath
import jmespath.exceptions
import jsonschema
import pytest
import referencing.exceptions
from pydantic import ValidationError
from pyrate_limiter import Duration, Limiter, Rate

from pytest_httpchain.errors import RequestError, SaveError, StageExecutionError, VerificationError
from pytest_httpchain.models import (
    Base64Body,
    BinaryBody,
    CombinationsParameter,
    FilesBody,
    FormBody,
    GraphQLBody,
    HeaderMatcher,
    IndividualParameter,
    JMESPathSave,
    JsonBody,
    ParallelConfig,
    ParallelForeachConfig,
    ParallelRepeatConfig,
    Request,
    Save,
    SaveStep,
    Scenario,
    SSLConfig,
    Stage,
    SubstitutionsSave,
    TextBody,
    UserFunctionsSave,
    Verify,
    VerifyStep,
    XmlBody,
    check_json_schema,
)
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
from pytest_httpchain.userfunc import UserFunctionError, call_user_function
from pytest_httpchain.utils import make_marker, optional_as_list, process_substitutions
from pytest_httpchain.warnings import ScenarioValidationWarning

logger = logging.getLogger(__name__)

# Exceptions that represent an expected stage failure (a bad scenario, a failed
# verification, an unreachable server) rather than a bug in the plugin. Both the
# sequential and the parallel execution paths catch these and turn them into a
# clean pytest failure instead of a raw traceback.
_STAGE_FAILURE_EXCEPTIONS = (StageExecutionError, TemplatesError, ValidationError)

# Guards Carrier._ensure_initialized's check-then-act so scenario initialization
# (side-effectful user functions, client construction) runs at most once per
# scenario class even under thread-based runners. A single module-level lock is
# enough: init is short and contention across scenario classes is negligible.
_INIT_LOCK = threading.Lock()


def _response_meta(response: httpx.Response) -> SimpleNamespace:
    """The ``response`` namespace exposed to response-step templates.

    Metadata only — the body stays out (``save`` handles data extraction):
    ``status`` (int), ``reason`` (str), ``headers`` (case-insensitive mapping),
    ``elapsed_ms`` (float, None if httpx hasn't recorded it)."""
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


def _is_active_xfail(mark: pytest.MarkDecorator) -> bool:
    """True when the mark is an xfail that will actually apply to the item.

    Mirrors pytest's ``evaluate_xfail_marks``: the condition comes from the
    ``condition=`` kwarg when present, else the positional args; no conditions
    means unconditionally active; with conditions, the mark is active when ANY
    is truthy. An INACTIVE xfail (all conditions falsy) means pytest treats
    the failure as genuine, so the carrier's abort machinery must too. String
    conditions (pytest evaluates those against its own config namespace) are
    conservatively treated as active."""
    if mark.name != "xfail":
        return False
    conditions = (mark.kwargs["condition"],) if "condition" in mark.kwargs else mark.args
    if not conditions:
        return True
    return any(isinstance(condition, str) or bool(condition) for condition in conditions)


def _error_request(e: Exception) -> httpx.Request | None:
    """The httpx.Request an httpx error was raised for, if it recorded one.

    ``httpx.HTTPError.request`` is a property that RAISES RuntimeError when
    unset (and arbitrary user exceptions have no such attribute), so this is
    a lookup, not a simple getattr default."""
    try:
        request = e.request  # ty: ignore[unresolved-attribute]
    except (AttributeError, RuntimeError):
        return None
    return request if isinstance(request, httpx.Request) else None


@dataclass
class IterationResult:
    """Result of a successful stage iteration (every stage runs at least one;
    a parallel stage runs many). ``started`` is the request's actual start
    time — HAR waterfalls are built from it."""

    saved_context: dict[str, Any]
    request: httpx.Request
    response: httpx.Response
    started: datetime


class Carrier:
    """Test carrier class that integrates HTTP chain test execution.

    This base class is subclassed dynamically to create test classes with scenario-specific test methods.
    It manages the shared state, context, and execution flow for all stages in a test scenario.
    """

    # These ClassVars are placeholders: create_test_class() overrides every one of
    # them in each per-scenario subclass dict (see the bottom of this module), so
    # the values here are never the ones used at runtime — do NOT rely on the
    # ChainMap()/None/[] defaults, and do NOT move this state to instance
    # attributes: the stage methods are classmethods that share one running context
    # across the chain via `cls`, while distinct scenarios stay isolated because
    # each gets its own subclass.
    scenario: ClassVar[Scenario | None] = None
    scenario_dir: ClassVar[Path | None] = None
    client: ClassVar[httpx.Client | None] = None
    aborted: ClassVar[bool] = False
    last_request: ClassVar[httpx.Request | None] = None
    last_response: ClassVar[httpx.Response | None] = None
    # Exchange bookkeeping for the report/HAR (see _record_exchanges):
    # last_exchanges holds the executed stage's (request, response, started) triples in
    # iteration order (response is None when none was received);
    # last_iterations_attempted is the stage's iteration count, so a parallel
    # stage's single shown exchange can be labeled honestly;
    # record_all_exchanges keeps EVERY iteration's exchange (HAR output on)
    # instead of just the shown one.
    last_exchanges: ClassVar[list[tuple[httpx.Request, httpx.Response | None, datetime | None]]] = []
    last_iterations_attempted: ClassVar[int] = 0
    record_all_exchanges: ClassVar[bool] = False
    global_context: ClassVar[ChainMap[str, Any]] = ChainMap()
    active_context_managers: ClassVar[list[AbstractContextManager]] = []
    max_parallel_iterations: ClassVar[int] = 10_000
    # Lazy-initialization state (see _ensure_initialized): _initialized flips
    # once the client is built; _init_failed records the first init failure so
    # every later stage skips instead of retrying; _context_resolved_at_collection
    # records that create_test_class already resolved the scenario substitutions
    # (forced by template-bearing stage parametrize values) so init must not
    # re-run them.
    _initialized: ClassVar[bool] = False
    _init_failed: ClassVar[str | None] = None
    _context_resolved_at_collection: ClassVar[bool] = False

    @classmethod
    def _resolve_scenario_path(cls, value: str | Path) -> Path:
        """Resolve a dialect file path against the scenario file's directory.

        Relative paths in scenario fields (``body.binary``, ``body.files``
        values, ``verify.body.schema``, ``ssl.cert``/``ssl.verify``) resolve
        against the scenario file's directory — matching ``$ref`` — not the
        pytest invocation CWD. Absolute paths pass through. Falls back to
        CWD-relative when no ``scenario_dir`` was seeded (hand-built
        subclasses in unit tests).
        """
        path = Path(value)
        if path.is_absolute() or cls.scenario_dir is None:
            return path
        return cls.scenario_dir / path

    @classmethod
    def _ensure_initialized(cls) -> None:
        """Resolve scenario substitutions and build the shared httpx client on first use.

        Deferred from collection time so that ``--collect-only`` and IDE test
        discovery neither execute user code (scenario substitutions may call
        user functions via templates; ``auth`` always does) nor allocate an
        ``httpx.Client`` per collected scenario. When stage parametrize values
        contain templates, create_test_class already resolved the scenario
        context (pytest needs concrete parameter values at collection) and it
        is reused here as-is.

        Initialization runs AT MOST ONCE per scenario — success or failure —
        so side-effectful user functions in substitutions/auth are never
        re-invoked. On failure, ``_init_failed`` records the root cause and the
        raised ``StageExecutionError`` fails the current stage cleanly; every
        later stage of the scenario (``always_run`` included) skips via the
        gate at the top of ``execute_stage``, mirroring the pre-lazy behavior
        where an initialization problem meant no stage ran at all. The lock
        makes the once-only guarantee hold even under thread-based runners.
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

                resolved_ssl: SSLConfig = walk(scenario.ssl, cls.global_context)
                # A Path-valued `verify` is a CA bundle file: scenario-relative.
                ssl_verify = resolved_ssl.verify
                if isinstance(ssl_verify, Path):
                    ssl_verify = str(cls._resolve_scenario_path(ssl_verify))
                client_kwargs: dict[str, Any] = {
                    "verify": ssl_verify,
                    "http2": True,
                }
                if resolved_ssl.cert is not None:
                    cert = resolved_ssl.cert
                    if isinstance(cert, list | tuple):
                        cert = tuple(cls._resolve_scenario_path(p) for p in cert)
                    else:
                        cert = cls._resolve_scenario_path(cert)
                    client_kwargs["cert"] = _normalize_cert(cert)
                if scenario.auth:
                    resolved_auth = walk(scenario.auth, cls.global_context)
                    client_kwargs["auth"] = call_user_function(resolved_auth)

                cls.client = httpx.Client(**client_kwargs)
            except Exception as e:
                cls._init_failed = str(e)
                raise StageExecutionError(f"Failed to initialize scenario: {e}") from e
            cls._initialized = True

    @classmethod
    def _resolve_always_run(cls, stage: Stage, stage_fixtures: dict[str, Any]) -> bool:
        """Resolve ``always_run``, evaluating a template form against the context
        available at stage start: fixtures and parametrize parameters plus the
        global context (scenario substitutions and earlier saves). Stage
        substitutions are not yet processed at this point. The result is coerced
        with Python truthiness."""
        if isinstance(stage.always_run, bool):
            return stage.always_run
        try:
            return bool(walk(stage.always_run, stage_start_context(cls.global_context, stage_fixtures)))
        except TemplatesError as e:
            raise StageExecutionError(f"Failed to evaluate always_run template: {e}") from e

    @classmethod
    def execute_stage(cls, stage: Stage, fixture_kwargs: dict[str, Any]) -> None:
        """Execute one stage end to end.

        Gates on the abort/``always_run`` flow, layers the stage context
        (fixtures + substitutions over the global context), builds the iteration
        matrix (``_build_iteration_substitutions``), runs the iterations
        sequentially or across a thread pool (``_run_iterations``), and on full
        success commits the collected saves as a new global-context layer.

        Any expected failure is reported via ``pytest.fail`` (clean, no
        traceback) and marks the chain aborted so later stages skip unless they
        opt into ``always_run`` — with one exemption: a stage marked ``xfail``
        is *expected* to fail, so its failure does not abort the chain and
        subsequent stages proceed. A failing stage commits **no** saves, so the
        global context is left unchanged (deterministic) rather than carrying a
        thread-timing-dependent subset of a parallel run.
        """
        # The exchange bookkeeping describes THIS stage only. Reset before
        # anything can fail or skip, so a stage that never reaches
        # _record_exchanges (skipped, or failed in substitutions/always_run/
        # parallel-config resolution) reports nothing instead of inheriting the
        # previous stage's exchanges, HAR entries, or iteration count.
        cls.last_request = None
        cls.last_response = None
        cls.last_exchanges = []
        cls.last_iterations_attempted = 0

        # Hard gate, ahead of the always_run machinery: a failed initialization
        # makes the whole scenario unusable (no context, no client), so even
        # always_run stages skip — matching the previous eager behavior where an
        # init problem failed collection and no stage ran at all. This also
        # keeps template-form always_run from being evaluated against the empty
        # context that an init failure leaves behind.
        if cls._init_failed is not None:
            pytest.skip(reason=f"Scenario initialization failed: {cls._init_failed}")

        try:
            stage_fixtures = cls._build_stage_fixtures(fixture_kwargs)

            if cls.aborted and not cls._resolve_always_run(stage, stage_fixtures):
                pytest.skip(reason="Flow aborted")

            cls._ensure_initialized()

            # Build base context for iterations (substitutions + fixtures + global)
            stage_context = stage_start_context(cls.global_context, stage_fixtures)
            stage_substitutions = process_substitutions(stage.substitutions, stage_context)
            local_context = with_stage_substitutions(stage_context, stage_substitutions)

            # DEBUG, guarded, and via _context_dump: full-context dumps carry
            # every saved value — chained auth tokens included — and pytest
            # attaches captured logs to failure reports, so they must be opt-in;
            # the guard also keeps every stage from paying O(context size)
            # serialization when the level is off (f-string args would evaluate
            # before the logger checks anything).
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("global context on start: %s", _context_dump(cls.global_context))
                logger.debug("local context on start: %s", _context_dump(local_context))

            parallel_config: ParallelConfig | None = walk(stage.parallel, local_context) if stage.parallel else None
            # The cap is enforced inside the builder, before materialization.
            iteration_substitutions = cls._build_iteration_substitutions(parallel_config, cls.max_parallel_iterations)

            total = len(iteration_substitutions)
            if total == 0:
                # The models reject the static empty cases (foreach/combinations
                # have min_length=1, repeat is PositiveInt), but a template- or
                # $ref-sourced parallel config can still resolve to empty at
                # runtime, so guard rather than silently send zero requests.
                raise StageExecutionError("Parallel configuration produced zero iterations; foreach/repeat must yield at least one item")

            results, first_error = cls._run_iterations(stage, local_context, iteration_substitutions, parallel_config)
            completed = [iter_result for iter_result in results if iter_result is not None]

            if first_error is None:
                cls._record_exchanges(completed, failed=None, attempted=total)
                # Commit saves only on full success. A failed (parallel) stage must
                # leave the global context untouched, not commit a non-deterministic
                # subset of iterations whose saves happened to land before the error.
                all_saves: dict[str, Any] = {}
                for iter_result in completed:
                    all_saves.update(iter_result.saved_context)
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("updates for global context: %s", _context_dump(all_saves))
                cls.global_context = with_saves(cls.global_context, all_saves)
            else:
                idx, exc = first_error
                cls._record_exchanges(completed, failed=exc, attempted=total)
                # Only label the failure as parallel when the user configured
                # `parallel`; otherwise re-raise the original error unchanged so a
                # plain stage failure isn't misreported as "Parallel execution failed".
                if parallel_config is not None:
                    raise StageExecutionError(f"Parallel execution failed at iteration {idx}: {exc}") from exc
                raise exc

        except _STAGE_FAILURE_EXCEPTIONS as e:
            # Detect xfail structurally (marker name == "xfail") rather than by a
            # substring scan of the raw mark string, so e.g. `skip(reason="...xfail...")`
            # or a custom `my_xfail` marker is not misclassified. Every mark already
            # round-tripped through make_marker() at collection time (create_test_class),
            # so parsing here cannot raise on a previously-validated scenario.
            is_xfail = any(_is_active_xfail(make_marker(mark)) for mark in stage.marks)
            # An xfail-marked stage's failure is EXPECTED: it must not stop the
            # chain, so later stages still run (only genuine failures abort).
            if not is_xfail:
                logger.error(str(e))
                cls.aborted = True
            pytest.fail(reason=str(e), pytrace=False)

    @classmethod
    def _build_stage_fixtures(cls, fixture_kwargs: dict[str, Any]) -> dict[str, Any]:
        """Resolve injected fixtures, wrapping callable (factory) fixtures so they
        can be invoked from template expressions while plain values pass through."""
        stage_fixtures: dict[str, Any] = {}
        for name, value in fixture_kwargs.items():
            if callable(value) and not inspect.isclass(value):
                stage_fixtures[name] = cls._wrap_factory_fixture(value)
            else:
                stage_fixtures[name] = value
        return stage_fixtures

    @staticmethod
    def _build_iteration_substitutions(parallel_config: "ParallelConfig | None", max_parallel_iterations: int) -> list[dict[str, Any]]:
        """Expand the (already template-resolved) parallel config into per-iteration
        substitution dicts: non-parallel -> a single empty dict; ``repeat`` -> N
        empties; ``foreach`` -> the cross-product of its parameter steps.

        The ``max_parallel_iterations`` cap is enforced BEFORE anything is
        materialized — the iteration count is known from the config alone
        (repeat value / product of step lengths), and the runaway
        template-driven counts the cap exists to catch would otherwise OOM the
        process building the list the cap was about to reject.

        Like pytest's stacked ``parametrize`` marker this is a cross-product, but the
        resulting iteration ORDER is the *reverse* of pytest's — do not assume the
        two orders match."""

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
                # int(): the model types repeat as `PositiveInt | NumberOrTemplate`
                # (templates are strings), but the config arrives walk()-resolved.
                repeat_total = int(repeat_count)
                check_cap(repeat_total)
                iteration_substitutions = [{} for _ in range(repeat_total)]
            case ParallelForeachConfig(foreach=foreach_steps):
                product = 1
                for step in foreach_steps:
                    match step:
                        case IndividualParameter(individual=individual):
                            product *= len(next(iter(individual.values())))
                        case CombinationsParameter(combinations=combinations):
                            product *= len(combinations)
                        case _:
                            raise RuntimeError(f"Unhandled foreach step: {type(step).__name__}")
                check_cap(product)
                for step in foreach_steps:
                    # Comprehension clause order is load-bearing: the new values are the
                    # OUTER loop and the accumulated dicts the INNER loop, which is what
                    # produces the (reverse-of-pytest) ordering noted above. Swapping the
                    # two `for` clauses silently changes the iteration order.
                    match step:
                        case IndividualParameter(individual=individual):
                            param_name = next(iter(individual.keys()))
                            values = individual[param_name]
                            iteration_substitutions = [{**existing, param_name: val} for val in values for existing in iteration_substitutions]
                        case CombinationsParameter(combinations=combinations):
                            # A still-string combinations value (template form) cannot
                            # reach here: the config arrives walk()-resolved.
                            combos: list[dict[str, Any]] = [vars(item) if isinstance(item, SimpleNamespace) else cast(dict[str, Any], item) for item in combinations]
                            iteration_substitutions = [{**existing, **combo} for combo in combos for existing in iteration_substitutions]
                        case _:
                            raise RuntimeError(f"Unhandled foreach step: {type(step).__name__}")
            case _:
                raise RuntimeError(f"Unhandled parallel config: {type(parallel_config).__name__}")
        return iteration_substitutions

    @classmethod
    def _record_exchanges(cls, completed: list["IterationResult"], failed: Exception | None, attempted: int) -> None:
        """Record the executed stage's HTTP exchanges for the report and HAR.

        ``last_request``/``last_response`` are the ONE exchange the test
        report shows, always a genuine pair from THIS stage (execute_stage
        resets them at stage start): the failing iteration's when it carries
        request info (its response may legitimately be None — a timeout got no
        response, and showing an earlier iteration's response instead would
        misattribute), else the last completed iteration in iteration-index
        order (deterministic).

        ``last_exchanges`` feeds the HAR file: with ``record_all_exchanges``
        on (HAR output enabled) it holds every completed iteration's exchange
        plus the failing one; otherwise only the shown exchange, so a scenario
        run without HAR output never retains more than one response per stage.
        """
        failed_request = failed.request if isinstance(failed, StageExecutionError) else None
        failed_response = failed.response if isinstance(failed, StageExecutionError) else None

        exchanges: list[tuple[httpx.Request, httpx.Response | None, datetime | None]] = [(r.request, r.response, r.started) for r in completed]
        if failed_request is not None:
            # The failing exchange's start time is not tracked (the exception
            # carries only request/response); None makes the HAR writer fall
            # back to write time for that one entry.
            exchanges.append((failed_request, failed_response, None))
        if not cls.record_all_exchanges:
            exchanges = exchanges[-1:]

        cls.last_exchanges = exchanges
        cls.last_iterations_attempted = attempted

        if failed_request is not None:
            cls.last_request, cls.last_response = failed_request, failed_response
        elif exchanges:
            # No request info on the failure (or no failure): show the stage's
            # own last completed exchange rather than nothing.
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

        A single iteration runs inline; multiple iterations run in a
        ``ThreadPoolExecutor`` capped at ``max_concurrency``, with an optional
        global rate limiter. On the first expected failure the pool is cancelled
        and ``(index, exception)`` is returned; otherwise ``first_error`` is None.
        """
        # The model types these as `PositiveInt | NumberOrTemplate` (templates
        # are strings), but the config arrives walk()-resolved, so the numeric
        # coercions are both the static narrowing and a runtime guarantee.
        max_concurrency = int(parallel_config.max_concurrency) if parallel_config else 1
        calls_per_sec = int(parallel_config.calls_per_sec) if parallel_config and parallel_config.calls_per_sec else None
        max_rate_limit_delay = float(parallel_config.max_rate_limit_delay) if parallel_config else 60.0

        total = len(iteration_substitutions)
        results: list[IterationResult | None] = [None] * total
        first_error: tuple[int, Exception] | None = None
        # No limiter for a single iteration: one acquire from a fresh bucket
        # can never block (calls_per_sec is a PositiveInt after resolution).
        limiter = Limiter(Rate(calls_per_sec, Duration.SECOND)) if calls_per_sec and total > 1 else None

        try:
            if total == 1:
                try:
                    results[0] = cls._execute_single_iteration(stage, local_context, iteration_substitutions[0], limiter, max_rate_limit_delay)
                except _STAGE_FAILURE_EXCEPTIONS as e:
                    first_error = (0, e)
            else:
                workers = min(max_concurrency, total)
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures: dict[Future[IterationResult], int] = {}
                    for idx, iter_vars in enumerate(iteration_substitutions):
                        future = executor.submit(cls._execute_single_iteration, stage, local_context, iter_vars, limiter, max_rate_limit_delay)
                        futures[future] = idx

                    for future in as_completed(futures):
                        idx = futures[future]
                        try:
                            results[idx] = future.result()
                        except _STAGE_FAILURE_EXCEPTIONS as e:
                            first_error = (idx, e)
                            executor.shutdown(wait=False, cancel_futures=True)
                            break
        finally:
            # Each Limiter owns a leaker daemon thread that keeps itself alive
            # until closed — leaking one per rate-limited stage execution
            # accumulates threads for the rest of the session.
            if limiter is not None:
                limiter.close()

        return results, first_error

    @classmethod
    def _build_request_kwargs(cls, request_model: Request) -> dict[str, Any]:
        request_kwargs: dict[str, Any] = {
            "method": request_model.method,
            "url": str(request_model.url),
            "headers": request_model.headers,
            "params": request_model.params or None,
            "timeout": request_model.timeout,
            "follow_redirects": request_model.allow_redirects,
        }

        if request_model.auth:
            try:
                auth_result = call_user_function(request_model.auth)
                request_kwargs["auth"] = auth_result
            except UserFunctionError as e:
                raise RequestError(f"Failed to configure authentication: {e}") from e

        match request_model.body:
            case None:
                pass

            case JsonBody(json=data):
                request_kwargs["json"] = data

            case GraphQLBody(graphql=gql):
                request_kwargs["json"] = {"query": gql.query, "variables": gql.variables}

            case FormBody(form=data):
                request_kwargs["data"] = data

            case XmlBody(xml=data) | TextBody(text=data):
                request_kwargs["content"] = data

            case Base64Body(base64=encoded_data):
                decoded_data = base64.b64decode(encoded_data)
                request_kwargs["content"] = decoded_data

            case BinaryBody(binary=file_path):
                try:
                    request_kwargs["content"] = cls._resolve_scenario_path(file_path).read_bytes()
                except FileNotFoundError as e:
                    raise RequestError(f"Binary file not found: {file_path}") from e
                except OSError as e:
                    raise RequestError(f"Cannot read binary file '{file_path}': {e}") from e

            case FilesBody(files=file_paths):
                files_list = []
                for field_name, file_path in file_paths.items():
                    path = cls._resolve_scenario_path(file_path)
                    try:
                        files_list.append((field_name, (path.name, path.read_bytes())))
                    except FileNotFoundError as e:
                        raise RequestError(f"File not found for upload: {file_path}") from e
                    except OSError as e:
                        raise RequestError(f"Cannot read file for upload '{file_path}': {e}") from e
                request_kwargs["files"] = files_list

            case _:
                # New body-type variant not handled here: a plugin bug — fail
                # loudly instead of silently sending a request with NO body.
                raise RuntimeError(f"Unhandled request body type: {type(request_model.body).__name__}")

        return request_kwargs

    @classmethod
    def _execute_http_request(cls, request_kwargs: dict[str, Any]) -> httpx.Response:
        # The terminal catch-all is deliberate: client.request() executes USER
        # code (a scenario `auth` httpx.Auth flow can raise anything) and httpx
        # raises non-HTTPError types like InvalidURL. Letting those propagate
        # raw would bypass the chain-abort machinery (aborted never set, a
        # parallel pool never cancelled), so every failure here becomes a
        # RequestError stage failure. The request that was on the wire is
        # attached when httpx recorded one, so a timed-out request still shows
        # up in the report and the HAR file.
        try:
            # Inside the try: the terminal catch-all below deliberately converts
            # ANY exception (including a violated invariant) into a clean
            # RequestError so the chain-abort machinery engages.
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
    def _process_save_step(save_model: Save, response: httpx.Response, context: ChainMap[str, Any]) -> dict[str, Any]:
        step_saved: dict[str, Any] = {}

        match save_model:
            case JMESPathSave():
                try:
                    response_json = response.json()
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    raise SaveError(f"Cannot extract variables, response is not valid JSON: {e}") from e

                for var_name, jmespath_expr in save_model.jmespath.items():
                    try:
                        saved_value = jmespath.search(jmespath_expr, response_json)
                        step_saved[var_name] = saved_value
                    except jmespath.exceptions.JMESPathError as e:
                        raise SaveError(f"Error saving variable {var_name}: {e}") from e

            case SubstitutionsSave():
                try:
                    substitution_result = process_substitutions(save_model.substitutions, context)
                    step_saved.update(substitution_result)
                except TemplatesError as e:
                    raise SaveError(f"Error processing substitutions: {e}") from e

            case UserFunctionsSave():
                for func_item in save_model.user_functions:
                    try:
                        func_result = call_user_function(func_item, response=response)

                        if not isinstance(func_result, dict):
                            raise SaveError(f"Save function must return dict, got {type(func_result).__name__}")

                        step_saved.update(func_result)  # ty: ignore[no-matching-overload]
                    except SaveError:
                        raise
                    except UserFunctionError as e:
                        raise SaveError(f"Error calling user function '{func_item}': {e}") from e

            case _:
                # New save variant not handled here: a plugin bug — fail loudly
                # instead of silently saving nothing.
                raise RuntimeError(f"Unhandled save type: {type(save_model).__name__}")

        return step_saved

    @classmethod
    def _process_verify_step(cls, verify_model: Verify, response: httpx.Response) -> None:
        if verify_model.status and response.status_code != verify_model.status:
            raise VerificationError(f"Status code doesn't match: expected {verify_model.status}, got {response.status_code}")

        for header_name, expected_value in verify_model.headers.items():
            match expected_value:
                case HeaderMatcher():
                    # An absent header behaves as an empty string, mirroring
                    # the body contains/matches semantics.
                    actual = response.headers.get(header_name) or ""
                    cls._verify_text_matchers(
                        f"Header '{header_name}' (value: {actual!r})",
                        actual,
                        contains=optional_as_list(expected_value.contains),
                        not_contains=optional_as_list(expected_value.not_contains),
                        matches=optional_as_list(expected_value.matches),
                        not_matches=optional_as_list(expected_value.not_matches),
                    )
                case _:
                    if response.headers.get(header_name) != expected_value:
                        raise VerificationError(f"Header '{header_name}' doesn't match: expected {expected_value}, got {response.headers.get(header_name)}")

        for i, expression in enumerate(verify_model.expressions):
            if not expression:
                raise VerificationError(f"Expression {i} failed: evaluated to {expression}")

        for func_item in verify_model.user_functions:
            try:
                result = call_user_function(func_item, response=response)

                if not isinstance(result, bool):
                    raise VerificationError(f"Verify function must return bool, got {type(result).__name__}")

                if not result:
                    raise VerificationError(f"Function '{func_item}' verification failed")

            except VerificationError:
                raise
            except UserFunctionError as e:
                raise VerificationError(f"Error calling user function '{func_item}': {e}") from e

        if verify_model.body.schema:
            schema = verify_model.body.schema
            if isinstance(schema, str | Path):
                schema_path = cls._resolve_scenario_path(schema)
                try:
                    schema = json.loads(schema_path.read_text())
                    check_json_schema(schema)
                except (OSError, json.JSONDecodeError) as e:
                    raise VerificationError(f"Error reading body schema file '{schema_path}': {e}") from e
                except jsonschema.SchemaError as e:
                    raise VerificationError(f"Invalid JSON Schema in file '{schema_path}': {e}") from e

            try:
                response_json = response.json()
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                raise VerificationError(f"Cannot validate schema, response is not valid JSON: {e}") from e

            try:
                jsonschema.validate(instance=response_json, schema=schema)
            except jsonschema.ValidationError as e:
                raise VerificationError(f"Body schema validation failed: {e}") from e
            except jsonschema.SchemaError as e:
                raise VerificationError(f"Invalid body validation schema: {e}") from e
            except referencing.exceptions.Unresolvable as e:
                # Inline schemas are standard JSON Schema, so a schema-internal
                # $ref jsonschema cannot resolve (typo'd "#/$defs/..." pointer,
                # or a pre-0.12 file-path $ref leftover) surfaces here — it must
                # fail the stage cleanly like any other verification failure,
                # not escape as a raw referencing traceback that would skip
                # exchange attribution and the chain-abort machinery.
                raise VerificationError(f"Cannot resolve $ref in body schema: {e}") from e

        cls._verify_text_matchers(
            "Body",
            response.text,
            contains=verify_model.body.contains,
            not_contains=verify_model.body.not_contains,
            matches=verify_model.body.matches,
            not_matches=verify_model.body.not_matches,
        )

    @staticmethod
    def _verify_text_matchers(
        subject: str,
        text: str,
        *,
        contains: Iterable[str],
        not_contains: Iterable[str],
        matches: Iterable[Any],
        not_matches: Iterable[Any],
    ) -> None:
        """The single encoding of the contains/matches check semantics, shared
        by body verification and header matchers (patterns use ``re.search``)."""
        for substring in contains:
            if substring not in text:
                raise VerificationError(f"{subject} doesn't contain '{substring}'")

        for substring in not_contains:
            if substring in text:
                raise VerificationError(f"{subject} contains '{substring}' while it shouldn't")

        for pattern in matches:
            if not re.search(pattern, text):
                raise VerificationError(f"{subject} doesn't match '{pattern}'")

        for pattern in not_matches:
            if re.search(pattern, text):
                raise VerificationError(f"{subject} matches '{pattern}' while it shouldn't")

    @classmethod
    def _execute_single_iteration(
        cls, stage: Stage, local_context: ChainMap[str, Any], iter_vars: Mapping[str, Any], limiter: Limiter | None = None, max_rate_limit_delay: float = 60
    ) -> IterationResult:
        """Run one iteration: resolve the request templates against the
        iteration context, acquire a rate-limit slot (waiting at most
        ``max_rate_limit_delay`` seconds), send the request, and process the
        response steps (verify/save) in order. Returns the iteration's saves
        and exchange; failures raise the stage-failure exceptions with the
        request/response attached."""
        iter_context = iteration_context(local_context, iter_vars)

        # walk() already returns a re-validated model (it dumps, substitutes, and
        # model_validates when templates are present, else returns the model as-is),
        # so a further model_validate would be a no-op (revalidate_instances='never').
        request_model = walk(stage.request, iter_context)
        request_kwargs = cls._build_request_kwargs(request_model)

        if limiter is not None and not limiter.try_acquire("api", blocking=True, timeout=max_rate_limit_delay):
            raise RequestError(f"Rate limit exceeded: could not acquire a request slot within {max_rate_limit_delay}s")

        # After the rate-limit acquire, so it stamps when the request actually
        # goes on the wire — this feeds the HAR entry's startedDateTime.
        started = datetime.now(UTC)
        response = cls._execute_http_request(request_kwargs)

        try:
            saved_context: dict[str, Any] = {}
            response_meta = _response_meta(response)
            for step in stage.response:
                # Response steps additionally see the `response` metadata
                # namespace (status/reason/headers/elapsed_ms) on top of the
                # evolving iteration context.
                step_context = response_step_context(iter_context, response_meta)
                match step:
                    case SaveStep():
                        save_model = walk(step.save, step_context)
                        step_saved = cls._process_save_step(save_model, response, step_context)
                        # The static HTTPCHAIN027 check cannot see dynamically
                        # produced keys (user_functions saves, template-form
                        # parameters) — surface the shadowing at runtime so the
                        # old read-your-saved-'response'-back behavior doesn't
                        # break silently.
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
                                # filterwarnings=error raises the warning as an
                                # exception; convert it to a clean stage failure
                                # so reporting and chain-abort engage instead of
                                # a raw warning traceback bypassing them.
                                raise SaveError(str(promoted)) from None
                        iter_context = with_saves(iter_context, step_saved)
                        saved_context.update(step_saved)

                    case VerifyStep():
                        verify_model = walk(step.verify, step_context)
                        cls._process_verify_step(verify_model, response)

                    case _:
                        # New response-step variant not handled here: a plugin bug —
                        # fail loudly rather than silently skipping the step.
                        raise RuntimeError(f"Unhandled response step: {type(step).__name__}")
        except StageExecutionError as e:
            e.request = response.request
            e.response = response
            raise
        except (TemplatesError, ValidationError) as e:
            raise StageExecutionError(str(e), request=response.request, response=response) from e

        return IterationResult(
            saved_context=saved_context,
            request=response.request,
            response=response,
            started=started,
        )

    @classmethod
    def _wrap_factory_fixture(cls, fixture: Callable) -> Callable:
        """Wrap a callable fixture so context-manager results are entered and tracked.

        Not a pure pass-through: each call invokes ``fixture`` and, if the result is
        a context manager, calls ``__enter__()`` and registers it on
        ``cls.active_context_managers`` for LIFO teardown in ``teardown_class``.
        Consequence: calling the wrapped value twice opens two resources (two
        ``__enter__`` calls, two teardowns) — invoke it once per needed instance.
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
            cls.client = None
        # Reset ALL per-run chain state so a re-run of this class after teardown
        # (e.g. a rerun plugin) actually re-executes: rebuild the client lazily
        # ("_initialized implies client is built"), clear the abort flag (else
        # every rerun stage would skip "Flow aborted"), and drop saved-context
        # layers back to the pristine base (maps[-1] is the original scenario
        # context from create_test_class; saves only ever prepend new layers).
        cls._initialized = False
        cls._init_failed = None
        cls.aborted = False
        cls.last_request = None
        cls.last_response = None
        cls.last_exchanges = []
        cls.last_iterations_attempted = 0
        cls.global_context = base_global_context(cls.global_context.maps[-1])


def _context_dump(data: Mapping[str, Any]) -> str:
    """Render a context for DEBUG logging; logging must never break a stage.

    A user-function save can put anything into the context, and ``json.dumps``
    can fail on it in several ways even with ``default=str`` — circular
    structures (ValueError), non-string dict keys (TypeError), a ``__str__``
    that itself raises — so ANY failure degrades to a placeholder."""
    try:
        return json.dumps(dict(data), indent=2, default=str)
    except Exception as e:
        return f"<unserializable context: {e}>"


def _normalize_cert(cert: Any) -> str | tuple[str, ...]:
    """Stringify SSL client-cert paths for httpx.

    The model stores ``cert`` as ``pathlib.Path`` (single) or a tuple of Paths.
    httpx builds the SSL context via ``load_cert_chain(*cert)`` for a non-tuple
    cert, so a bare ``Path`` is unpacked and raises ``TypeError``. Passing string
    paths avoids that for both the single-path and (cert, key) tuple forms.
    """
    if isinstance(cert, list | tuple):
        return tuple(str(p) for p in cert)
    return str(cert)
