"""stage_settings.py: a stage's ``parallel`` and ``retry`` blocks once
rendered — each setting coerced, or refused naming it, the iteration matrix
and its cap, the retry policy's waits.

Where the carrier applies them (a stage failing, before any request, on a
setting it cannot use) is pinned in test_carrier.py
(TestResolvedParallelSettings, TestRetry, TestCollectSaves, TestParallelStats).
"""

import math

import pytest

from pytest_httpchain.errors import StageExecutionError
from pytest_httpchain.models import IndividualParameter, ParallelForeachConfig, ParallelRepeatConfig, RetryConfig
from pytest_httpchain.stage_settings import (
    NO_RETRY,
    RateSettings,
    RetryPolicy,
    _parallel_int,
    _parallel_number,
    build_iteration_substitutions,
    rate_settings,
    retry_policy,
)


class TestResolvedParallelSettings:
    """The numeric `parallel` settings are `PositiveInt | NumberOrTemplate`.
    walk() re-validates the resolved config, but NumberOrTemplate accepts any
    complete template, so a template resolving to another template arrives as
    text. Whatever gets through, a value that cannot make a working limiter or
    pool must fail loudly — never quietly turn rate limiting off."""

    @pytest.mark.parametrize(
        "value",
        [0, 0.5, -1, pytest.param("0.5", id="fractional-string"), "abc", "{{ 2 }}", pytest.param(True, id="bool")],
    )
    def test_bad_count_rejected_naming_field_and_value(self, value):
        # A bool is rejected outright: float(True) == 1.0 would silently
        # configure one worker / one call per second.
        with pytest.raises(StageExecutionError) as excinfo:
            _parallel_int("calls_per_sec", value)
        assert "calls_per_sec" in str(excinfo.value)
        assert repr(value) in str(excinfo.value)

    @pytest.mark.parametrize("value", [2, 2.0, pytest.param("2", id="str")])
    def test_whole_number_count_accepted_as_int(self, value):
        """A template may resolve to 2.0 or "2"; both are the whole number 2."""
        assert _parallel_int("max_concurrency", value) == 2

    @pytest.mark.parametrize("value", [0, -0.5, float("inf"), float("nan"), "abc", True])
    def test_bad_delay_rejected(self, value):
        with pytest.raises(StageExecutionError, match="max_rate_limit_delay must be a positive number"):
            _parallel_number("max_rate_limit_delay", value)

    @pytest.mark.parametrize("value", [0.5, pytest.param("0.5", id="str")])
    def test_fractional_delay_is_kept(self, value):
        # Unlike the two counts, the delay is a duration: half a second is a
        # meaningful budget, so only the counts demand whole numbers.
        assert _parallel_number("max_rate_limit_delay", value) == 0.5

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            # int(0.5) == 0, which is falsy: a plain cast turned "one call
            # every two seconds" into no rate limiting at all.
            ("calls_per_sec", 0.5),
            # int(0.5) == 0 workers, and ThreadPoolExecutor(max_workers=0)
            # raises a bare ValueError rather than failing the stage.
            ("max_concurrency", 0),
            ("max_concurrency", 0.5),
            ("max_concurrency", "{{ 2 }}"),
            ("max_rate_limit_delay", "{{ 5 }}"),
        ],
    )
    def test_rate_settings_reject_unusable_setting(self, field, value):
        settings = {"repeat": 2, "max_concurrency": 2, "calls_per_sec": None, "max_rate_limit_delay": 60, field: value}
        with pytest.raises(StageExecutionError, match=field):
            rate_settings(ParallelRepeatConfig.model_construct(**settings), 2, NO_RETRY)

    @pytest.mark.parametrize(
        ("settings", "total", "attempts", "expected"),
        [
            # A single iteration's only attempt cannot block on a fresh
            # bucket: nothing is read, not even values that would fail.
            pytest.param({"calls_per_sec": "x", "max_rate_limit_delay": "x", "max_concurrency": "x"}, 1, 1, RateSettings(), id="one-iteration-one-attempt"),
            # Retried, its attempts each take a slot, so the rate settings are
            # read; one iteration runs inline, so max_concurrency is not.
            pytest.param({"calls_per_sec": 5, "max_rate_limit_delay": 2, "max_concurrency": "x"}, 1, 2, RateSettings(5, 2, None), id="one-iteration-retried"),
            # No parallel block: a retried stage has no limiter to configure.
            pytest.param(None, 1, 3, RateSettings(), id="no-parallel-retried"),
        ],
    )
    def test_rate_settings_read_only_where_they_apply(self, settings, total, attempts, expected):
        parallel = None if settings is None else ParallelRepeatConfig.model_construct(repeat=total, **settings)
        assert rate_settings(parallel, total, RetryPolicy(attempts=attempts)) == expected


class TestRetryPolicy:
    """A stage's ``retry`` resolved: the wait before each attempt after the
    first. The attempts the carrier makes with it are pinned in
    test_carrier.py (TestRetry)."""

    @pytest.mark.parametrize(
        ("retry", "waits"),
        [
            pytest.param({"attempts": 1}, [], id="one-attempt-no-wait"),
            pytest.param({"attempts": 4}, [1.0, 1.0, 1.0], id="defaults"),
            pytest.param({"attempts": 5, "delay": 0.5, "backoff": 2}, [0.5, 1.0, 2.0, 4.0], id="backoff"),
            pytest.param({"attempts": 5, "delay": 1, "backoff": 3, "max_delay": 5}, [1.0, 3.0, 5.0, 5.0], id="capped"),
            pytest.param({"attempts": 3, "delay": 0, "backoff": 2}, [0.0, 0.0], id="no-delay"),
            pytest.param({"attempts": 3, "delay": 2, "max_delay": 0}, [0.0, 0.0], id="capped-at-zero"),
        ],
    )
    def test_wait_schedule(self, retry, waits):
        """Each wait is the one before multiplied by ``backoff``, never more
        than ``max_delay``: one before each attempt after the first."""
        assert list(retry_policy(RetryConfig.model_validate(retry)).waits()) == waits

    def test_wait_schedule_past_floats_range(self):
        """A backoff doubling a thousand times overflows to inf, which the
        cap takes; uncapped, the wait is inf, which `carrier._wait_to_retry`
        bounds."""
        waits = list(retry_policy(RetryConfig(attempts=1100, delay=1, backoff=2, max_delay=60)).waits())
        assert (len(waits), waits[:3], waits[-1]) == (1099, [1.0, 2.0, 4.0], 60.0)
        assert list(retry_policy(RetryConfig(attempts=1100, delay=1, backoff=2)).waits())[-1] == math.inf


class TestIterationCapBeforeMaterialization:
    """The max_parallel_iterations cap exists to stop runaway template-driven
    counts; it must be checked BEFORE the iteration list is materialized, or
    the runaway values it exists to catch OOM the process first. These tests
    completing quickly (no 10^9 allocations) is the point."""

    def test_huge_repeat_rejected_before_allocation(self):
        config = ParallelRepeatConfig(repeat=10**9)
        with pytest.raises(StageExecutionError, match="exceeds maximum"):
            build_iteration_substitutions(config, max_parallel_iterations=10)

    def test_huge_foreach_product_rejected_before_expansion(self):
        config = ParallelForeachConfig(
            foreach=[
                IndividualParameter(individual={"a": list(range(5000))}),
                IndividualParameter(individual={"b": list(range(5000))}),
            ]
        )
        with pytest.raises(StageExecutionError, match="exceeds maximum"):
            build_iteration_substitutions(config, max_parallel_iterations=10)

    def test_small_configs_still_expand(self):
        result = build_iteration_substitutions(ParallelRepeatConfig(repeat=3), max_parallel_iterations=10)
        assert result == [{}, {}, {}]

    def test_non_parallel_single_iteration(self):
        assert build_iteration_substitutions(None, max_parallel_iterations=10) == [{}]
