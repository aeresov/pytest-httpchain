"""parallel_stats.py: a parallel stage's stats from how its iterations ended,
the report section, the saved object, and the thresholds' checks and failure.

How the carrier times the iterations and what it does with a violated
threshold is pinned in test_carrier.py (TestParallelStats).
"""

import math

import pytest

from pytest_httpchain.models import ParallelThresholds
from pytest_httpchain.parallel_stats import MAX_LISTED_FAILURES, THRESHOLDS, Latency, failed_iterations, nearest_rank, parallel_stats, threshold_failure


class TestNearestRank:
    """The p-th percentile is the value of rank ⌈p/100 · n⌉: always one of
    the values measured, never an interpolation between two."""

    @pytest.mark.parametrize(
        ("values", "percent", "expected"),
        [
            pytest.param([7.0], 50, 7.0, id="single-p50"),
            pytest.param([7.0], 99, 7.0, id="single-p99"),
            # Odd: the middle value.
            pytest.param([1.0, 2.0, 3.0, 4.0, 5.0], 50, 3.0, id="odd-p50"),
            # Even: the lower of the two middle values, rank 2 of 4.
            pytest.param([1.0, 2.0, 3.0, 4.0], 50, 2.0, id="even-p50"),
            pytest.param([1.0, 2.0, 3.0, 4.0], 95, 4.0, id="even-p95"),
            # Rank ⌈0.95 · 10⌉ = 10: with fewer than 20 values, p95 is the maximum.
            pytest.param([float(v) for v in range(1, 11)], 95, 10.0, id="ten-p95"),
            # Rank 19 of 20, not 20: 0.95 · 20 is 19.000000000000004 in floats.
            pytest.param([float(v) for v in range(1, 21)], 95, 19.0, id="twenty-p95"),
            pytest.param([float(v) for v in range(1, 101)], 99, 99.0, id="hundred-p99"),
            pytest.param([float(v) for v in range(1, 101)], 50, 50.0, id="hundred-p50"),
            # Below rank 1: the minimum.
            pytest.param([1.0, 2.0], 0, 1.0, id="p0"),
            pytest.param([1.0, 2.0], 100, 2.0, id="p100"),
        ],
    )
    def test_rank(self, values, percent, expected):
        assert nearest_rank(values, percent) == expected


class TestLatency:
    def test_none_passed(self):
        assert Latency.of([]) is None

    def test_one_passed(self):
        assert Latency.of([0.25]) == Latency(min_ms=250.0, mean_ms=250.0, p50_ms=250.0, p95_ms=250.0, p99_ms=250.0, max_ms=250.0)

    def test_in_milliseconds_whatever_the_order(self):
        latency = Latency.of([0.004, 0.001, 0.003, 0.002])
        assert latency is not None
        assert (latency.min_ms, latency.p50_ms, latency.p95_ms, latency.max_ms) == pytest.approx((1.0, 2.0, 4.0, 4.0))
        assert latency.mean_ms == pytest.approx(2.5)


class TestParallelStats:
    def test_counts_by_how_each_iteration_ended(self):
        """None is an iteration that never started: cancelled with the pool."""
        stats = parallel_stats(["passed", "failed", None, "cancelled", "skipped", "passed"], [0.01, 0.5, None, None, None, 0.03], 2.0)
        assert (stats.iterations, stats.passed, stats.failed, stats.cancelled, stats.skipped) == (6, 2, 1, 2, 1)
        assert stats.success_ratio == 2 / 6
        assert stats.wall_ms == 2000.0
        assert stats.rps == 1.0  # passed iterations per second
        # Every one that ran to its end, passed, failed or skipped: all but the cancelled.
        assert (stats.completed, stats.completed_rps) == (4, 2.0)

    def test_latency_is_over_the_passed_iterations_only(self):
        """A failure's duration (a timeout's whole wait) says nothing of how
        fast the server answers."""
        stats = parallel_stats(["passed", "failed", "passed"], [0.01, 30.0, 0.03], 1.0)
        assert stats.latency is not None
        assert (stats.latency.min_ms, stats.latency.max_ms) == pytest.approx((10.0, 30.0))

    def test_none_passed(self):
        """No passed throughput, but the failed iterations completed."""
        stats = parallel_stats(["failed", "failed"], [0.1, 0.2], 1.0)
        assert stats.latency is None
        assert (stats.success_ratio, stats.rps, stats.completed_rps) == (0.0, 0.0, 2.0)

    def test_zero_wall_time_has_no_throughput(self):
        stats = parallel_stats(["passed"], [0.0], 0.0)
        assert (stats.rps, stats.completed_rps) == (0.0, 0.0)

    def test_saved(self):
        stats = parallel_stats(["passed", "failed", "passed", "passed"], [0.01, 0.2, 0.02, 0.03], 0.5)
        assert stats.saved() == pytest.approx(
            {
                "iterations": 4,
                "passed": 3,
                "failed": 1,
                "success_ratio": 0.75,
                "wall_ms": 500.0,
                "rps": 6.0,
                "completed_rps": 8.0,
                "min_ms": 10.0,
                "mean_ms": 20.0,
                "p50_ms": 20.0,
                "p95_ms": 30.0,
                "p99_ms": 30.0,
                "max_ms": 30.0,
            }
        )

    def test_saved_latency_is_null_when_none_passed(self):
        saved = parallel_stats(["failed"], [0.1], 1.0).saved()
        assert [saved[key] for key in ("min_ms", "mean_ms", "p50_ms", "p95_ms", "p99_ms", "max_ms")] == [None] * 6

    def test_summary(self):
        stats = parallel_stats(["passed", "failed", "passed", None], [0.0125, 0.2, 0.0375, None], 0.4, {"min_success_ratio": 0.5, "max_p95_ms": 30})
        assert stats.summary() == (
            "Iterations:     4 (2 passed, 1 failed, 1 cancelled)\n"
            "Success ratio:  0.5000\n"
            "Wall time:      400.00 ms\n"
            "Throughput:     7.50 completed iterations/s, 5.00 passed iterations/s\n"
            "Latency (ms):   min 12.50, mean 25.00, p50 12.50, p95 37.50, p99 37.50, max 37.50\n"
            "Thresholds:     min_success_ratio 0.5: met, 0.5 (2 of 4 iterations passed)\n"
            "                max_p95_ms 30: not met, 37.5 ms"
        )

    def test_summary_counts_skipped_only_when_there_are(self):
        stats = parallel_stats(["skipped", "failed"], [None, 0.1], 0.1)
        assert stats.summary().splitlines()[0] == "Iterations:     2 (0 passed, 1 failed, 0 cancelled, 1 skipped)"
        assert "Latency (ms):   none, no iteration passed" in stats.summary().splitlines()


class TestThresholds:
    def test_one_per_model_field(self):
        """Every field of ``parallel.thresholds`` is checked, in the model's order."""
        assert list(THRESHOLDS) == list(ParallelThresholds.model_fields)

    def test_without_limits_nothing_is_checked(self):
        assert parallel_stats(["passed"], [0.01], 1.0).checks == ()
        assert parallel_stats(["passed"], [0.01], 1.0, {}).checks == ()

    @pytest.mark.parametrize(
        ("name", "limit", "met"),
        [
            # A limit is met at equality, from either side.
            pytest.param("min_success_ratio", 0.75, True, id="ratio-equal"),
            pytest.param("min_success_ratio", 0.76, False, id="ratio-below"),
            pytest.param("min_success_ratio", 0, True, id="ratio-zero"),
            pytest.param("max_mean_ms", 20, True, id="mean-equal"),
            pytest.param("max_mean_ms", 19.99, False, id="mean-above"),
            pytest.param("max_p50_ms", 20, True, id="p50-equal"),
            pytest.param("max_p95_ms", 29.9, False, id="p95-above"),
            pytest.param("max_p99_ms", 30, True, id="p99-equal"),
            pytest.param("min_rps", 6, True, id="rps-equal"),
            pytest.param("min_rps", 6.5, False, id="rps-below"),
        ],
    )
    def test_limit(self, name, limit, met):
        stats = parallel_stats(["passed", "failed", "passed", "passed"], [0.01, 0.2, 0.02, 0.03], 0.5, {name: limit})
        [check] = stats.checks
        assert (check.name, check.limit, check.met) == (name, limit, met)
        assert stats.violated == (() if met else (check,))

    def test_latency_with_no_passed_iteration_is_not_met(self):
        """A limit on something not measured cannot be said to hold."""
        stats = parallel_stats(["failed"], [0.1], 1.0, {"min_success_ratio": 0, "max_p95_ms": 1000})
        ratio, p95 = stats.checks
        assert ratio.met
        assert (p95.measured, p95.met) == (None, False)
        assert p95.violation() == "max_p95_ms: not measured, no iteration passed (limit 1000 ms)"
        assert p95.status() == "max_p95_ms 1000: not met, no iteration passed"

    def test_listed_in_the_models_order(self):
        stats = parallel_stats(["passed"], [0.01], 1.0, {"min_rps": 100, "max_p99_ms": 1, "min_success_ratio": 1})
        assert [check.name for check in stats.checks] == ["min_success_ratio", "max_p99_ms", "min_rps"]

    @pytest.mark.parametrize(
        ("name", "limit", "violation"),
        [
            pytest.param("min_success_ratio", 0.9, "min_success_ratio: 0.666667 (2 of 3 iterations passed), below the limit 0.9", id="ratio"),
            pytest.param("max_mean_ms", 5, "max_mean_ms: 20 ms, above the limit 5 ms", id="mean"),
            pytest.param("max_p99_ms", 12.5, "max_p99_ms: 30 ms, above the limit 12.5 ms", id="p99"),
            pytest.param("min_rps", 100, "min_rps: 20 passed iterations/s, below the limit 100", id="rps"),
        ],
    )
    def test_violation_names_the_threshold_the_value_and_the_limit(self, name, limit, violation):
        stats = parallel_stats(["passed", "failed", "passed"], [0.01, 0.1, 0.03], 0.1, {name: limit})
        [check] = stats.violated
        assert check.violation() == violation


class TestThresholdFailure:
    @staticmethod
    def _violated(limits):
        return parallel_stats(["passed", "failed", "passed"], [0.01, 0.1, 0.03], 0.1, limits).violated

    def test_one(self):
        assert threshold_failure(self._violated({"max_p95_ms": 25})) == "Parallel threshold not met: max_p95_ms: 30 ms, above the limit 25 ms"

    def test_every_one_is_listed(self):
        assert threshold_failure(self._violated({"min_success_ratio": 0.9, "max_p95_ms": 25, "min_rps": 50})) == (
            "3 parallel thresholds not met:\n"
            "  1. min_success_ratio: 0.666667 (2 of 3 iterations passed), below the limit 0.9\n"
            "  2. max_p95_ms: 30 ms, above the limit 25 ms\n"
            "  3. min_rps: 20 passed iterations/s, below the limit 50"
        )

    def test_the_failed_iterations_follow(self):
        """A failure of several lines keeps its later ones, under its first."""
        failures = [(1, "Status code doesn't match: expected 200, got 500"), (4, "2 verification checks failed:\n  1. one\n\n  2. two")]
        assert threshold_failure(self._violated({"min_success_ratio": 0.9}), failures) == (
            "Parallel threshold not met: min_success_ratio: 0.666667 (2 of 3 iterations passed), below the limit 0.9\n"
            "2 failed iterations:\n"
            "  iteration 1: Status code doesn't match: expected 200, got 500\n"
            "  iteration 4: 2 verification checks failed:\n"
            "                 1. one\n"
            "\n"
            "                 2. two"
        )

    def test_one_failed_iteration(self):
        message = threshold_failure(self._violated({"min_success_ratio": 0.9}), [(1, "boom")])
        assert message.splitlines()[1:] == ["Failed iteration:", "  iteration 1: boom"]

    def test_only_the_first_failed_iterations_are_listed(self):
        failures = [(idx, f"failure {idx}") for idx in range(MAX_LISTED_FAILURES + 2)]
        lines = threshold_failure(self._violated({"min_success_ratio": 0.9}), failures).splitlines()
        assert lines[1] == f"First {MAX_LISTED_FAILURES} of {MAX_LISTED_FAILURES + 2} failed iterations:"
        assert lines[2:] == [f"  iteration {idx}: failure {idx}" for idx in range(MAX_LISTED_FAILURES)]


class TestFailedIterations:
    """The listing a threshold failure ends with, and an error on exit's
    failure too, under a heading of its own (`qualifier`)."""

    @pytest.mark.parametrize(
        ("count", "heading"),
        [
            pytest.param(1, "Failed iteration tolerated by min_success_ratio:", id="one"),
            pytest.param(2, "2 failed iterations tolerated by min_success_ratio:", id="some"),
            pytest.param(
                MAX_LISTED_FAILURES + 1,
                f"First {MAX_LISTED_FAILURES} of {MAX_LISTED_FAILURES + 1} failed iterations tolerated by min_success_ratio:",
                id="more-than-listed",
            ),
        ],
    )
    def test_heading_ends_with_the_qualifier(self, count, heading):
        lines = failed_iterations([(idx, "boom") for idx in range(count)], " tolerated by min_success_ratio")
        assert lines[0] == heading
        assert len(lines) == 1 + min(count, MAX_LISTED_FAILURES)

    def test_none_lists_nothing(self):
        assert failed_iterations([], " tolerated by min_success_ratio") == []


def test_limits_shown_as_written():
    """500, not 500.0; a fraction in full."""
    [check] = parallel_stats(["passed"], [1.0], 1.0, {"max_p95_ms": 500.0}).violated
    assert check.violation() == "max_p95_ms: 1000 ms, above the limit 500 ms"
    [check] = parallel_stats(["passed", "failed", "failed"], [0.01, 0.1, 0.1], 1.0, {"min_success_ratio": 1 / 3 + 1e-9}).violated
    assert check.violation().endswith(f"below the limit {1 / 3 + 1e-9!r}")
    assert math.isclose(check.limit, 1 / 3 + 1e-9)
