"""Parallel HTTP request execution infrastructure using ThreadPoolExecutor."""

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class ParallelIterationResult:
    """Result of a successful parallel iteration."""

    index: int
    saved_vars: dict[str, Any]
    request: httpx.Request
    response: httpx.Response


@dataclass
class ParallelIterationError:
    """Result of a failed parallel iteration."""

    index: int
    iteration_context: dict[str, Any]
    exception: Exception


@dataclass
class ParallelExecutionResult:
    """Aggregated result of parallel execution."""

    results: list[ParallelIterationResult | ParallelIterationError | None]
    first_error: ParallelIterationError | None
    completed_count: int
    failed_count: int


def execute_parallel_requests(
    iterations: list[dict[str, Any]],
    execute_fn: Callable[[int, dict[str, Any]], ParallelIterationResult],
    max_concurrency: int,
    fail_fast: bool,
) -> ParallelExecutionResult:
    """Execute HTTP requests in parallel using ThreadPoolExecutor.

    Args:
        iterations: List of iteration context dicts (one per request).
        execute_fn: Function to execute a single iteration (index, context) -> result.
        max_concurrency: Maximum number of concurrent requests.
        fail_fast: If True, stop on first error and cancel pending requests.

    Returns:
        ParallelExecutionResult with all results and error information.
    """
    total = len(iterations)
    results: list[ParallelIterationResult | ParallelIterationError | None] = [None] * total
    first_error: ParallelIterationError | None = None
    completed_count = 0
    failed_count = 0

    workers = min(max_concurrency, total) if total > 0 else 1

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures: dict[Future[ParallelIterationResult], int] = {}
        for idx, iter_context in enumerate(iterations):
            future = executor.submit(execute_fn, idx, iter_context)
            futures[future] = idx

        for future in as_completed(futures):
            idx = futures[future]
            iter_context = iterations[idx]

            try:
                results[idx] = future.result()
                completed_count += 1
            except Exception as e:
                error = ParallelIterationError(
                    index=idx,
                    iteration_context=iter_context,
                    exception=e,
                )
                results[idx] = error
                failed_count += 1

                if fail_fast and first_error is None:
                    first_error = error
                    for f in futures:
                        if not f.done():
                            f.cancel()
                    break

    return ParallelExecutionResult(
        results=results,
        first_error=first_error,
        completed_count=completed_count,
        failed_count=failed_count,
    )
