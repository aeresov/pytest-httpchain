"""A parallel stage's statistics, and the thresholds they are held to.

Pure: the carrier times the iterations and says how each ended, and this
turns that into what the stage's ``Parallel Summary`` report section shows
(`ParallelStats.summary`), what ``parallel.stats_as`` saves
(`ParallelStats.saved`), and whether ``parallel.thresholds`` are met
(`ParallelStats.violated`, `threshold_failure`).

The measures, as docs/advanced/parallel.md states them:

- An iteration's **duration** is the time its requests spent in the HTTP
  client: from handing one to it to having its whole response, redirects and
  an auth flow's round trips included, summed over the attempts a stage's
  ``retry`` makes. The client's own waits are in it: for a pooled connection
  (``client.max_connections`` below ``max_concurrency``), for an HTTP/2
  stream (past 100 at a time), and opening a connection. Not the wait for a
  ``calls_per_sec`` slot, nor the wait between attempts, nor rendering the
  request or running the response steps: a rate limit the stage sets itself
  must not read as a slow server.
- The **wall time** runs from the first iteration's start to the last one's
  end, every wait included.
- **Latency** is over the iterations that passed: a failure's duration (a
  timeout's whole wait, a refused connection's next to none) says nothing of
  how fast the server answers. Its percentiles are nearest-rank
  (`nearest_rank`): always a duration measured, never an interpolation.
- **Throughput** is the completed iterations per second of wall time,
  every one that ran to its end (all but the cancelled), and the passed ones
  per second (``rps``), which ``min_rps`` holds: a failed iteration adds no
  latency, and no passed throughput either.
- The **success ratio** is the passed iterations' share of all the stage's
  iterations, cancelled ones included.
"""

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

# How an iteration ended: its response steps all passed; it failed; it never
# sent its request, or stopped waiting to, because another iteration had
# ended the stage; a user function skipped or xfailed it.
type IterationEnd = Literal["passed", "failed", "cancelled", "skipped"]

# The latency percentiles reported, saved and limited (`max_p<N>_ms`).
PERCENTILES = (50, 95, 99)

# Each threshold of ``parallel.thresholds``, in the model's order, which is
# the order a failure lists them in: whether the stat must be at least
# (``min``) or at most (``max``) its limit, and which stat it holds.
THRESHOLDS: dict[str, tuple[Literal["min", "max"], str]] = {
    "min_success_ratio": ("min", "success_ratio"),
    "max_mean_ms": ("max", "mean_ms"),
    "max_p50_ms": ("max", "p50_ms"),
    "max_p95_ms": ("max", "p95_ms"),
    "max_p99_ms": ("max", "p99_ms"),
    "min_rps": ("min", "rps"),
}

# How many failed iterations a threshold failure lists, the lowest first.
MAX_LISTED_FAILURES = 5


def nearest_rank(ordered: Sequence[float], percent: int) -> float:
    """The ``percent``-th percentile of ``ordered`` (ascending, not empty) by
    the nearest-rank method: the value of rank ⌈percent/100 · n⌉, counting
    from 1, the smallest that at least ``percent`` % of the values are at most.

    In integers: 0.95 · 20 is 19.000000000000004 in floats, whose ceiling
    would take the 20th value for the 19th.
    """
    rank = -(-percent * len(ordered) // 100)
    return ordered[max(rank, 1) - 1]


@dataclass(frozen=True, slots=True)
class Latency:
    """The durations of the iterations that passed, in milliseconds."""

    min_ms: float
    mean_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float

    @classmethod
    def of(cls, seconds: Sequence[float]) -> "Latency | None":
        """The latency of these durations, or None for none: no iteration passed."""
        if not seconds:
            return None
        ordered = sorted(duration * 1000 for duration in seconds)
        p50, p95, p99 = (nearest_rank(ordered, percent) for percent in PERCENTILES)
        return cls(min_ms=ordered[0], mean_ms=math.fsum(ordered) / len(ordered), p50_ms=p50, p95_ms=p95, p99_ms=p99, max_ms=ordered[-1])


def _limit(value: float) -> str:
    """A limit as the scenario writes it: 500, not 500.0."""
    return str(int(value)) if value.is_integer() else repr(value)


def _measured(value: float) -> str:
    """A measured value, to six significant digits."""
    return f"{value:.6g}"


@dataclass(frozen=True, slots=True)
class ThresholdCheck:
    """One threshold held against the stats: its ``limit``, the value
    ``measured`` (None: a latency with no passed iteration to measure it
    over, which does not meet any limit), whether it is ``met``, and how a
    message shows the value (``shown``)."""

    name: str
    limit: float
    measured: float | None
    met: bool
    shown: str

    def _limit_text(self) -> str:
        return f"{_limit(self.limit)} ms" if self.name.endswith("_ms") else _limit(self.limit)

    def violation(self) -> str:
        """What a failure says of the threshold not met: its name, the value measured, the limit."""
        if self.measured is None:
            return f"{self.name}: not measured, no iteration passed (limit {self._limit_text()})"
        side = "below" if THRESHOLDS[self.name][0] == "min" else "above"
        return f"{self.name}: {self.shown}, {side} the limit {self._limit_text()}"

    def status(self) -> str:
        """What the summary says of the threshold: met or not, and the value measured."""
        return f"{self.name} {_limit(self.limit)}: {'met' if self.met else 'not met'}, {self.shown}"


@dataclass(frozen=True, slots=True)
class ParallelStats:
    """A parallel stage's stats: how many of its iterations ended how, the
    stage's wall time, the passed iterations' latency (None when none
    passed), and the thresholds checked against them, when they were: only
    once every iteration had ended without failing the stage by itself."""

    iterations: int
    passed: int
    failed: int
    cancelled: int
    skipped: int
    wall_ms: float
    latency: Latency | None
    checks: tuple[ThresholdCheck, ...] = ()

    @property
    def success_ratio(self) -> float:
        return self.passed / self.iterations

    @property
    def completed(self) -> int:
        """The iterations that ran to their end: passed, failed or skipped, all but the cancelled."""
        return self.iterations - self.cancelled

    def _per_second(self, count: int) -> float:
        return count * 1000 / self.wall_ms if self.wall_ms > 0 else 0.0

    @property
    def rps(self) -> float:
        """Passed iterations per second of wall time, what ``min_rps`` holds."""
        return self._per_second(self.passed)

    @property
    def completed_rps(self) -> float:
        """Completed iterations per second of wall time."""
        return self._per_second(self.completed)

    @property
    def violated(self) -> tuple[ThresholdCheck, ...]:
        return tuple(check for check in self.checks if not check.met)

    def stat(self, name: str) -> float | None:
        """The stat a threshold holds, by name (`THRESHOLDS`); a latency's is None when no iteration passed."""
        if name in ("success_ratio", "rps"):
            return getattr(self, name)
        return getattr(self.latency, name) if self.latency is not None else None

    def saved(self) -> dict[str, Any]:
        """The object ``parallel.stats_as`` saves. A stage commits it only
        when it passes, so no iteration of it was cancelled or skipped, and
        those counts are left out; a latency is null when none passed."""
        return {
            "iterations": self.iterations,
            "passed": self.passed,
            "failed": self.failed,
            "success_ratio": self.success_ratio,
            "wall_ms": self.wall_ms,
            "rps": self.rps,
            "completed_rps": self.completed_rps,
            **{f"{stat}_ms": self.stat(f"{stat}_ms") for stat in ("min", "mean", *(f"p{percent}" for percent in PERCENTILES), "max")},
        }

    def summary(self) -> str:
        """The ``Parallel Summary`` report section."""
        counts = f"{self.passed} passed, {self.failed} failed, {self.cancelled} cancelled"
        if self.skipped:
            counts += f", {self.skipped} skipped"
        lines = [
            f"Iterations:     {self.iterations} ({counts})",
            f"Success ratio:  {self.success_ratio:.4f}",
            f"Wall time:      {self.wall_ms:.2f} ms",
            f"Throughput:     {self.completed_rps:.2f} completed iterations/s, {self.rps:.2f} passed iterations/s",
        ]
        if (latency := self.latency) is None:
            lines.append("Latency (ms):   none, no iteration passed")
        else:
            lines.append(
                f"Latency (ms):   min {latency.min_ms:.2f}, mean {latency.mean_ms:.2f}, p50 {latency.p50_ms:.2f}, "
                f"p95 {latency.p95_ms:.2f}, p99 {latency.p99_ms:.2f}, max {latency.max_ms:.2f}"
            )
        lines.extend(f"{'Thresholds:' if i == 0 else '':<16}{check.status()}" for i, check in enumerate(self.checks))
        return "\n".join(lines)


def _check(stats: ParallelStats, name: str, limit: float) -> ThresholdCheck:
    bound, stat = THRESHOLDS[name]
    measured = stats.stat(stat)
    if measured is None:
        return ThresholdCheck(name, limit, None, False, "no iteration passed")
    met = measured >= limit if bound == "min" else measured <= limit
    if stat == "success_ratio":
        shown = f"{_measured(measured)} ({stats.passed} of {stats.iterations} iterations passed)"
    elif stat == "rps":
        shown = f"{_measured(measured)} passed iterations/s"
    else:
        shown = f"{_measured(measured)} ms"
    return ThresholdCheck(name, limit, measured, met, shown)


def parallel_stats(ends: Sequence[IterationEnd | None], durations: Sequence[float | None], wall_seconds: float, limits: Mapping[str, float] | None = None) -> ParallelStats:
    """The stats of a stage whose iteration i ended as ``ends[i]`` (None: it
    never started, cancelled first) after its exchanges took ``durations[i]``
    seconds (read for the passed ones), the whole stage ``wall_seconds``,
    checked against ``limits`` (by threshold name, `THRESHOLDS`) when given."""
    counts = Counter(end or "cancelled" for end in ends)
    stats = ParallelStats(
        iterations=len(ends),
        passed=counts["passed"],
        failed=counts["failed"],
        cancelled=counts["cancelled"],
        skipped=counts["skipped"],
        wall_ms=wall_seconds * 1000,
        latency=Latency.of([duration for end, duration in zip(ends, durations, strict=True) if end == "passed" and duration is not None]),
    )
    if not limits:
        return stats
    return replace(stats, checks=tuple(_check(stats, name, limits[name]) for name in THRESHOLDS if name in limits))


def failed_iterations(failures: Sequence[tuple[int, str]], qualifier: str = "") -> list[str]:
    """The lines listing the first of ``failures`` (iteration index and
    message, lowest index first), the failed iterations a ``min_success_ratio``
    below 1 tolerated, under a heading ``qualifier`` ends: none for none."""
    if not failures:
        return []
    listed = failures[:MAX_LISTED_FAILURES]
    if len(listed) < len(failures):
        lines = [f"First {len(listed)} of {len(failures)} failed iterations{qualifier}:"]
    else:
        lines = [f"Failed iteration{qualifier}:" if len(failures) == 1 else f"{len(failures)} failed iterations{qualifier}:"]
    for idx, message in listed:
        first, *rest = message.split("\n")
        marker = f"  iteration {idx}: "
        lines.append(f"{marker}{first}")
        lines.extend(f"{' ' * len(marker)}{line}" if line else "" for line in rest)
    return lines


def threshold_failure(violated: Sequence[ThresholdCheck], failures: Sequence[tuple[int, str]] = ()) -> str:
    """The failure of a stage that did not meet the ``violated`` thresholds,
    every one listed as a verify step lists its failed checks, then the first
    of its ``failures`` (`failed_iterations`)."""
    if len(violated) == 1:
        lines = [f"Parallel threshold not met: {violated[0].violation()}"]
    else:
        lines = [f"{len(violated)} parallel thresholds not met:", *(f"  {number}. {check.violation()}" for number, check in enumerate(violated, start=1))]
    return "\n".join([*lines, *failed_iterations(failures)])
