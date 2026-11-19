"""A stage's ``parallel`` and ``retry`` blocks once rendered, as its
iterations use them: each setting coerced, or refused as a stage failure
naming it (`setting_number`), the iteration matrix
(`build_iteration_substitutions`), the retry policy (`retry_policy`), and
how the iterations are paced (`rate_settings`).

Pure: the carrier renders each block (`rendering.render_declared`) and hands
it here before any request, so a setting the stage cannot use fails it
before its iterations send anything, not once they all have.
"""

import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, TypeGuard

from pytest_httpchain.errors import RequestError, SaveError, StageExecutionError, VerificationError
from pytest_httpchain.models import (
    CombinationsParameter,
    IndividualParameter,
    ParallelConfig,
    ParallelForeachConfig,
    ParallelRepeatConfig,
    ParallelThresholds,
    RetryConfig,
)


def setting_number(setting: str, value: Any, must_be: str = "a positive number", accepts: Callable[[float], bool] = lambda number: number > 0) -> float:
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
    """`setting_number` for a positive whole-number setting. Truncating
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
    number = setting_number(setting, value)
    if not number.is_integer():
        raise StageExecutionError(f"{setting} must be a positive whole number, got {value!r}")
    return int(number)


def _parallel_number(field: str, value: Any) -> float:
    """A walk()-resolved ``parallel`` setting as a positive float, or a stage failure."""
    return setting_number(f"parallel.{field}", value)


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


def parallel_flag(field: str, value: Any) -> bool:
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


def threshold_limits(thresholds: ParallelThresholds | None) -> dict[str, float]:
    """A walk()-resolved ``parallel.thresholds`` as the limits the stage's
    stats are held to, by threshold, or a stage failure naming one it cannot
    use: resolved before any request, as the rest of the ``parallel`` config
    is, so a limit a template rendered to template text fails the stage then,
    not once every iteration has run. None is "never declared" here:
    `rendering.render_declared` already refused a template that rendered to
    None."""
    if thresholds is None:
        return {}
    limits: dict[str, float] = {}
    for name in type(thresholds).model_fields:
        value = getattr(thresholds, name)
        if value is None:
            continue
        setting = f"parallel.thresholds.{name}"
        if name == "min_success_ratio":
            limits[name] = setting_number(setting, value, "a number from 0 to 1", lambda number: 0 <= number <= 1)
        else:
            limits[name] = setting_number(setting, value)
    return limits


# The error each kind a stage's ``retry.on`` names is a failure of: a verify
# step's, a save step's, the request's.
_RETRY_ON_ERRORS: dict[str, type[StageExecutionError]] = {"verify": VerificationError, "save": SaveError, "request": RequestError}


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """A stage's ``retry`` resolved (`retry_policy`): how many attempts it
    makes at most, how long it waits before each after the first, and after
    which failures. `NO_RETRY` is a stage's without one: one attempt."""

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
            # own, `carrier._wait_to_retry`) takes.
            wait *= self.backoff


NO_RETRY = RetryPolicy()


def retry_policy(config: RetryConfig) -> RetryPolicy:
    """A walk()-resolved ``retry`` as the policy the iterations follow, or a
    stage failure naming a setting it cannot use. Resolved before any request,
    as the ``parallel`` config is: a setting a template rendered to template
    text fails the stage then, not once the first attempt has failed."""
    return RetryPolicy(
        attempts=_setting_int("retry.attempts", config.attempts),
        delay=setting_number("retry.delay", config.delay, "a number of seconds, 0 or more", lambda number: number >= 0),
        backoff=setting_number("retry.backoff", config.backoff, "a number, 1 or more", lambda number: number >= 1),
        # None is "never declared" here: `rendering.render_declared` already
        # refused a template that rendered to None.
        max_delay=setting_number("retry.max_delay", config.max_delay, "a number of seconds, 0 or more", lambda number: number >= 0) if config.max_delay is not None else None,
        on=tuple(_RETRY_ON_ERRORS[kind] for kind in ([config.on] if isinstance(config.on, str) else config.on)),
    )


def build_iteration_substitutions(parallel_config: ParallelConfig | None, max_parallel_iterations: int) -> list[dict[str, Any]]:
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


@dataclass(frozen=True, slots=True)
class RateSettings:
    """How a stage's iterations are paced (`rate_settings`): the requests per
    second a limiter admits, None for no limiter; how long an attempt waits
    for a slot; and how many iterations run at once, None for a stage that
    runs one, inline."""

    calls_per_sec: int | None = None
    max_rate_limit_delay: float = 60
    max_concurrency: int | None = None


def rate_settings(parallel_config: ParallelConfig | None, total: int, retry: RetryPolicy) -> RateSettings:
    """A walk()-resolved ``parallel`` config's rate and concurrency settings
    for ``total`` iterations, each making the attempts ``retry`` allows, or a
    stage failure naming one it cannot use: ``calls_per_sec``, then
    ``max_rate_limit_delay``, then ``max_concurrency``.

    Each is read only where it applies: a single iteration's only attempt
    cannot block on a fresh bucket, so without retries the rate-limiting
    settings have nothing to do, and only more than one iteration runs in a
    pool.
    """
    if parallel_config is None or not (total > 1 or retry.attempts > 1):
        return RateSettings()
    # Guarded rather than cast: the config arrives walk()-resolved, and a
    # resolved value can still be unusable (see `setting_number`). None is
    # "never declared" here: `rendering.render_declared` already refused a
    # template that rendered to None.
    calls_per_sec = _parallel_int("calls_per_sec", parallel_config.calls_per_sec) if parallel_config.calls_per_sec is not None else None
    max_rate_limit_delay = _parallel_number("max_rate_limit_delay", parallel_config.max_rate_limit_delay)
    max_concurrency = _parallel_int("max_concurrency", parallel_config.max_concurrency) if total > 1 else None
    return RateSettings(calls_per_sec, max_rate_limit_delay, max_concurrency)
