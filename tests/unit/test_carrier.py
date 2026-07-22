"""Unit tests for carrier.py - error cases and edge cases only.

Success cases for body types, verify, and save are covered by integration tests:
- tests/integration/test_body_types.py
- tests/integration/test_verify.py
- tests/integration/test_save.py
- tests/integration/test_errors.py
"""

import json
from collections import ChainMap
from contextlib import contextmanager
from http import HTTPMethod

import httpx
import pytest
from pyrate_limiter import Duration, Limiter, Rate

from pytest_httpchain.carrier import Carrier, _normalize_cert
from pytest_httpchain.errors import RequestError, SaveError, VerificationError
from pytest_httpchain.models import (
    BinaryBody,
    FilesBody,
    JMESPathSave,
    ParallelRepeatConfig,
    Request,
    Stage,
    Verify,
)
from pytest_httpchain.models.entities import ResponseBody


class TestNormalizeCert:
    """httpx needs string cert paths; the model stores pathlib.Path. A single
    Path passed straight to httpx.Client(cert=...) crashes with TypeError."""

    def test_single_path_becomes_str(self):
        from pathlib import Path

        # Expected values built via str(Path(...)) so the assertion is
        # platform-native (Windows renders these with backslashes).
        assert _normalize_cert(Path("/p/client.pem")) == str(Path("/p/client.pem"))

    def test_tuple_of_paths_becomes_tuple_of_str(self):
        from pathlib import Path

        assert _normalize_cert((Path("/p/c.pem"), Path("/p/k.pem"))) == (str(Path("/p/c.pem")), str(Path("/p/k.pem")))


class TestBuildRequestKwargsErrors:
    """Error cases not covered by integration tests."""

    def test_binary_body_file_not_found(self):
        request = Request(
            url="https://example.com/api",
            method=HTTPMethod.POST,
            body=BinaryBody(binary="/nonexistent/file.bin"),
        )

        with pytest.raises(RequestError, match="Binary file not found"):
            Carrier._build_request_kwargs(request)

    def test_files_body_file_not_found(self):
        request = Request(
            url="https://example.com/api",
            method=HTTPMethod.POST,
            body=FilesBody(files={"upload": "/nonexistent/file.txt"}),
        )

        with pytest.raises(RequestError, match="File not found for upload"):
            Carrier._build_request_kwargs(request)

    def test_binary_body_unreadable_path(self, tmp_path):
        # A directory raises IsADirectoryError, an OSError that is NOT a
        # FileNotFoundError, so it must be caught by the broadened handler (M2).
        request = Request(
            url="https://example.com/api",
            method=HTTPMethod.POST,
            body=BinaryBody(binary=str(tmp_path)),
        )

        with pytest.raises(RequestError, match="Cannot read binary file"):
            Carrier._build_request_kwargs(request)

    def test_files_body_unreadable_path(self, tmp_path):
        request = Request(
            url="https://example.com/api",
            method=HTTPMethod.POST,
            body=FilesBody(files={"upload": str(tmp_path)}),
        )

        with pytest.raises(RequestError, match="Cannot read file for upload"):
            Carrier._build_request_kwargs(request)


class TestBuildRequestKwargsParams:
    """Params handling edge cases."""

    def test_empty_params_does_not_override_url_query(self):
        """Empty params default should not strip query parameters from the URL."""
        request = Request(
            url="https://example.com/api?streamId=123",
            method=HTTPMethod.GET,
        )

        kwargs = Carrier._build_request_kwargs(request)
        assert kwargs["params"] is None

    def test_non_empty_params_passed_through(self):
        request = Request(
            url="https://example.com/api",
            method=HTTPMethod.GET,
            params={"key": "value"},
        )

        kwargs = Carrier._build_request_kwargs(request)
        assert kwargs["params"] == {"key": "value"}


class TestProcessSaveStepErrors:
    """Error cases not covered by integration tests."""

    def test_jmespath_save_invalid_json_response(self):
        response = httpx.Response(
            200,
            content=b"not valid json",
            headers={"content-type": "text/plain"},
        )
        save_model = JMESPathSave(jmespath={"value": "key"})
        context = ChainMap()

        with pytest.raises(SaveError, match="response is not valid JSON"):
            Carrier._process_save_step(save_model, response, context)


class TestProcessVerifyStepErrors:
    """Error cases and edge cases not covered by integration tests."""

    def test_verify_body_schema_file_not_found(self):
        response = httpx.Response(200, json={"id": 123})
        verify = Verify(body=ResponseBody(schema="/nonexistent/schema.json"))

        with pytest.raises(VerificationError, match="Error reading body schema file"):
            Carrier._process_verify_step(verify, response)

    def test_verify_body_schema_invalid_json_response(self):
        response = httpx.Response(
            200,
            content=b"not json",
            headers={"content-type": "text/plain"},
        )
        schema = {"type": "object"}
        verify = Verify(body=ResponseBody(schema=schema))

        with pytest.raises(VerificationError, match="response is not valid JSON"):
            Carrier._process_verify_step(verify, response)

    def test_verify_body_schema_from_file(self, tmp_path):
        """Test schema loaded from file path - unique to unit tests."""
        schema_path = tmp_path / "schema.json"
        schema_path.write_text(
            json.dumps(
                {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}},
                    "required": ["id"],
                }
            )
        )

        response = httpx.Response(200, json={"id": 123})
        verify = Verify(body=ResponseBody(schema=str(schema_path)))

        # Should not raise
        Carrier._process_verify_step(verify, response)

    def test_verify_expressions_falsy_values(self):
        """Test that falsy expression values fail verification."""
        response = httpx.Response(200)
        verify = Verify(expressions=[True, False, True])

        with pytest.raises(VerificationError, match="Expression.*failed"):
            Carrier._process_verify_step(verify, response)

    def test_verify_expressions_empty_string_fails(self):
        """Test that empty string expression fails."""
        response = httpx.Response(200)
        verify = Verify(expressions=[""])

        with pytest.raises(VerificationError, match="Expression.*failed"):
            Carrier._process_verify_step(verify, response)


def _make_stage(**kwargs) -> Stage:
    """Minimal valid stage pointing at a URL that is never actually requested in
    these unit tests (the code paths under test stop before the HTTP call)."""
    return Stage(
        name="s",
        request=Request(url="https://example.com/", method=HTTPMethod.GET),
        response=[],
        **kwargs,
    )


def _make_carrier_subclass(**attrs) -> type[Carrier]:
    """Fresh Carrier subclass with its own mutable state, so a test never mutates
    the shared base-class defaults. `client` is None: teardown_class tolerates it.
    `_initialized` is True: the subclass is hand-built (no `scenario` model), so
    the lazy scenario initialization in execute_stage must not run."""
    defaults = {
        "client": None,
        "aborted": False,
        "last_request": None,
        "last_response": None,
        "global_context": ChainMap(),
        "_initialized": True,
        "active_context_managers": [],
        "max_parallel_iterations": 10_000,
    }
    defaults.update(attrs)
    return type("UnitCarrier", (Carrier,), defaults)


class TestRateLimiting:
    """The rate limiter actually blocks and times out (M2)."""

    def test_limiter_timeout_raises_request_error(self):
        # A 1/sec limiter: consume the only slot, then a second acquire with a
        # tiny timeout must block for the timeout and fail (not silently pass).
        limiter = Limiter(Rate(1, Duration.SECOND))
        assert limiter.try_acquire("api", blocking=True, timeout=2)

        stage = _make_stage()
        with pytest.raises(RequestError, match="Rate limit exceeded"):
            # The limiter check happens before the HTTP request, so no client is
            # needed; an exhausted limiter forces the timeout path.
            Carrier._execute_single_iteration(stage, ChainMap(), {}, limiter=limiter, rate_limit_delay=0.2)

    def test_limiter_blocks_until_timeout_elapses(self):
        import time

        limiter = Limiter(Rate(1, Duration.SECOND))
        assert limiter.try_acquire("api", blocking=True, timeout=2)

        stage = _make_stage()
        start = time.monotonic()
        with pytest.raises(RequestError, match="Rate limit exceeded"):
            Carrier._execute_single_iteration(stage, ChainMap(), {}, limiter=limiter, rate_limit_delay=0.3)
        # It actually blocked for ~the timeout rather than failing instantly.
        assert time.monotonic() - start >= 0.25


class TestParallelIterationCap:
    """Exceeding max_parallel_iterations is rejected before any request runs."""

    def test_exceeding_cap_fails(self):
        carrier = _make_carrier_subclass(max_parallel_iterations=2)
        stage = _make_stage(parallel=ParallelRepeatConfig(repeat=5))

        # execute_stage turns the StageExecutionError into a clean pytest failure.
        with pytest.raises(pytest.fail.Exception, match=r"exceeds maximum \(2\)"):
            carrier.execute_stage(stage, {})

    def test_within_cap_does_not_trip_guard(self):
        # repeat == cap is allowed (the guard is strict '>'); this run reaches the
        # HTTP layer and fails there (client is None) — proving the cap did NOT
        # short-circuit it. The failure is a clean stage failure: the request
        # path's terminal catch-all deliberately converts ANY exception (user
        # auth flows raise arbitrary types) into RequestError so the
        # chain-abort machinery engages.
        carrier = _make_carrier_subclass(max_parallel_iterations=3)
        stage = _make_stage(parallel=ParallelRepeatConfig(repeat=3))

        with pytest.raises(pytest.fail.Exception) as excinfo:
            carrier.execute_stage(stage, {})
        assert "exceeds maximum" not in str(excinfo.value)


class TestContextManagerFixtureCleanup:
    """Context-manager / @contextmanager-generator fixtures are entered on use and
    their finalizers run during teardown_class."""

    def test_context_manager_fixture_exit_runs(self):
        carrier = _make_carrier_subclass()
        events: list[str] = []

        class Resource:
            def __enter__(self):
                events.append("enter")
                return "resource-value"

            def __exit__(self, *exc):
                events.append("exit")
                return False

        # A factory fixture returning a context manager: wrapping enters it,
        # records it for cleanup, and yields the entered value.
        wrapped = carrier._build_stage_fixtures({"res": lambda: Resource()})
        value = wrapped["res"]()

        assert value == "resource-value"
        assert events == ["enter"]
        assert len(carrier.active_context_managers) == 1

        carrier.teardown_class()

        assert events == ["enter", "exit"]
        assert carrier.active_context_managers == []

    def test_generator_contextmanager_fixture_cleanup_runs(self):
        carrier = _make_carrier_subclass()
        events: list[str] = []

        @contextmanager
        def resource():
            events.append("setup")
            try:
                yield "gen-value"
            finally:
                events.append("teardown")

        wrapped = carrier._build_stage_fixtures({"res": resource})
        value = wrapped["res"]()

        assert value == "gen-value"
        assert events == ["setup"]

        carrier.teardown_class()

        assert events == ["setup", "teardown"]

    def test_teardown_continues_when_a_finalizer_raises(self):
        carrier = _make_carrier_subclass()
        exited: list[str] = []

        class Bad:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                raise RuntimeError("cleanup boom")

        class Good:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                exited.append("good")
                return False

        wrapped = carrier._build_stage_fixtures({"bad": lambda: Bad(), "good": lambda: Good()})
        wrapped["bad"]()
        wrapped["good"]()

        # teardown_class swallows finalizer errors (logged) so a failing context
        # manager does not prevent the others from being cleaned up.
        carrier.teardown_class()

        assert exited == ["good"]
        assert carrier.active_context_managers == []


class TestContextDump:
    """Context dumps feed DEBUG logging only; they must never break a stage."""

    def test_serializes_plain_context(self):
        from pytest_httpchain.carrier import _context_dump

        assert '"a": 1' in _context_dump({"a": 1})

    def test_circular_context_degrades_to_placeholder(self):
        from pytest_httpchain.carrier import _context_dump

        circular: dict = {}
        circular["self"] = circular
        out = _context_dump(circular)
        assert "unserializable" in out


class TestIterationCapBeforeMaterialization:
    """The max_parallel_iterations cap exists to stop runaway template-driven
    counts; it must be checked BEFORE the iteration list is materialized, or
    the runaway values it exists to catch OOM the process first. These tests
    completing quickly (no 10^9 allocations) is the point."""

    def test_huge_repeat_rejected_before_allocation(self):
        from pytest_httpchain.errors import StageExecutionError
        from pytest_httpchain.models import ParallelRepeatConfig

        config = ParallelRepeatConfig(repeat=10**9)
        with pytest.raises(StageExecutionError, match="exceeds maximum"):
            Carrier._build_iteration_substitutions(config, max_parallel_iterations=10)

    def test_huge_foreach_product_rejected_before_expansion(self):
        from pytest_httpchain.errors import StageExecutionError
        from pytest_httpchain.models import IndividualParameter, ParallelForeachConfig

        config = ParallelForeachConfig(
            foreach=[
                IndividualParameter(individual={"a": list(range(5000))}),
                IndividualParameter(individual={"b": list(range(5000))}),
            ]
        )
        with pytest.raises(StageExecutionError, match="exceeds maximum"):
            Carrier._build_iteration_substitutions(config, max_parallel_iterations=10)

    def test_small_configs_still_expand(self):
        from pytest_httpchain.models import ParallelRepeatConfig

        result = Carrier._build_iteration_substitutions(ParallelRepeatConfig(repeat=3), max_parallel_iterations=10)
        assert result == [{}, {}, {}]

    def test_non_parallel_single_iteration(self):
        assert Carrier._build_iteration_substitutions(None, max_parallel_iterations=10) == [{}]


class TestContextDumpNeverRaises:
    """The helper's contract is absolute: logging must never break a stage,
    whatever a user-function save put into the context."""

    def test_tuple_keyed_dict_degrades(self):
        from pytest_httpchain.carrier import _context_dump

        out = _context_dump({"a": {(1, 2): 3}})
        assert "unserializable" in out

    def test_poison_str_degrades(self):
        from pytest_httpchain.carrier import _context_dump

        class Poison:
            def __str__(self):
                raise RuntimeError("boom")

        out = _context_dump({"a": Poison()})
        assert "unserializable" in out
