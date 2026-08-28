"""Unit tests for carrier.py - error cases and edge cases only.

Success cases for body types, verify, and save are covered by integration tests:
- tests/integration/test_body_types.py
- tests/integration/test_verify.py
- tests/integration/test_save.py
- tests/integration/test_errors.py
"""

import json
import ssl
import threading
import time
from collections import ChainMap
from contextlib import contextmanager
from datetime import UTC, datetime
from http import HTTPMethod

import httpx
import pytest
import trustme
from pyrate_limiter import Duration, Limiter, Rate

from pytest_httpchain.carrier import Carrier, IterationResult, _context_dump, fresh_scenario_state
from pytest_httpchain.errors import RequestError, SaveError, StageExecutionError, VerificationError
from pytest_httpchain.models import (
    BinaryBody,
    FilesBody,
    IndividualParameter,
    JMESPathSave,
    ParallelForeachConfig,
    ParallelRepeatConfig,
    Request,
    Scenario,
    SSLConfig,
    Stage,
    Verify,
)
from pytest_httpchain.models.entities import ResponseBody
from pytest_httpchain.request_builder import build_request_kwargs
from pytest_httpchain.response_steps import check_rendered_assertions, process_save, process_verify
from pytest_httpchain.templates import TemplatesError


class TestBuildRequestKwargsErrors:
    """Error cases not covered by integration tests."""

    def test_binary_body_file_not_found(self):
        request = Request(
            url="https://example.com/api",
            method=HTTPMethod.POST,
            body=BinaryBody(binary="/nonexistent/file.bin"),
        )

        with pytest.raises(RequestError, match="Binary file not found"):
            build_request_kwargs(request)

    def test_files_body_file_not_found(self):
        request = Request(
            url="https://example.com/api",
            method=HTTPMethod.POST,
            body=FilesBody(files={"upload": "/nonexistent/file.txt"}),
        )

        with pytest.raises(RequestError, match="File not found for upload"):
            build_request_kwargs(request)

    def test_binary_body_unreadable_path(self, tmp_path):
        # A directory raises IsADirectoryError, an OSError that is NOT a
        # FileNotFoundError, so it must be caught by the broadened handler (M2).
        request = Request(
            url="https://example.com/api",
            method=HTTPMethod.POST,
            body=BinaryBody(binary=str(tmp_path)),
        )

        with pytest.raises(RequestError, match="Cannot read binary file"):
            build_request_kwargs(request)

    def test_files_body_unreadable_path(self, tmp_path):
        request = Request(
            url="https://example.com/api",
            method=HTTPMethod.POST,
            body=FilesBody(files={"upload": str(tmp_path)}),
        )

        with pytest.raises(RequestError, match="Cannot read file for upload"):
            build_request_kwargs(request)


class TestBuildRequestKwargsParams:
    """Params handling edge cases."""

    def test_empty_params_does_not_override_url_query(self):
        """Empty params default should not strip query parameters from the URL."""
        request = Request(
            url="https://example.com/api?streamId=123",
            method=HTTPMethod.GET,
        )

        kwargs = build_request_kwargs(request)
        assert kwargs["params"] is None

    def test_non_empty_params_passed_through(self):
        request = Request(
            url="https://example.com/api",
            method=HTTPMethod.GET,
            params={"key": "value"},
        )

        kwargs = build_request_kwargs(request)
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
            process_save(save_model, response, context)


class TestProcessVerifyStepErrors:
    """Error cases and edge cases not covered by integration tests."""

    def test_verify_body_schema_file_not_found(self):
        response = httpx.Response(200, json={"id": 123})
        verify = Verify(body=ResponseBody(schema="/nonexistent/schema.json"))

        with pytest.raises(VerificationError, match="Error reading body schema file"):
            process_verify(verify, response)

    def test_verify_body_schema_invalid_json_response(self):
        response = httpx.Response(
            200,
            content=b"not json",
            headers={"content-type": "text/plain"},
        )
        schema = {"type": "object"}
        verify = Verify(body=ResponseBody(schema=schema))

        with pytest.raises(VerificationError, match="response is not valid JSON"):
            process_verify(verify, response)

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
        process_verify(verify, response)

    def test_verify_status_zero_is_not_treated_as_absent(self):
        """The status gate is `is not None`, not truthiness."""
        response = httpx.Response(200, json={})
        verify = Verify.model_construct(status=0, headers={}, expressions=[], user_functions=[], body=ResponseBody())

        with pytest.raises(VerificationError, match="Status code doesn't match"):
            process_verify(verify, response)

    def test_rendered_away_status_is_rejected(self):
        """A declared status that a template rendered to None must fail loudly.

        Both models re-validate cleanly, so without this the assertion would be
        silently dropped and a 500 would pass green.
        """
        declared = Verify(status="{{ expected }}")
        rendered = Verify(status=None)

        with pytest.raises(VerificationError, match="rendered to None"):
            check_rendered_assertions(declared, rendered)

    def test_rendered_away_body_schema_is_rejected(self):
        declared = Verify(body=ResponseBody(schema="{{ schema_path }}"))
        rendered = Verify(body=ResponseBody(schema=None))

        with pytest.raises(VerificationError, match="body.schema.*rendered to None"):
            check_rendered_assertions(declared, rendered)

    def test_undeclared_assertions_are_not_flagged(self):
        """An assertion that was never declared is not a rendered-away one."""
        check_rendered_assertions(Verify(), Verify())

    def test_rendered_status_that_survives_is_not_flagged(self):
        check_rendered_assertions(Verify(status="{{ expected }}"), Verify(status=200))

    def test_verify_body_schema_non_utf8_file(self, tmp_path):
        """A non-UTF-8 schema file must fail the stage cleanly.

        UnicodeDecodeError is a ValueError, not a JSONDecodeError, so a narrower
        except let it escape past the chain-abort machinery as a raw traceback.
        """
        schema_path = tmp_path / "schema.json"
        schema_path.write_bytes(b'{"type": "\xff\xfe object"}')

        response = httpx.Response(200, json={"id": 1})
        verify = Verify(body=ResponseBody(schema=str(schema_path)))

        with pytest.raises(VerificationError, match="Error reading body schema file"):
            process_verify(verify, response)

    def test_verify_expressions_falsy_values(self):
        """Test that falsy expression values fail verification."""
        response = httpx.Response(200)
        verify = Verify(expressions=[True, False, True])

        with pytest.raises(VerificationError, match="Expression.*failed"):
            process_verify(verify, response)

    def test_verify_expressions_empty_string_fails(self):
        """Test that empty string expression fails."""
        response = httpx.Response(200)
        verify = Verify(expressions=[""])

        with pytest.raises(VerificationError, match="Expression.*failed"):
            process_verify(verify, response)

    def test_verify_body_contains_failure(self):
        response = httpx.Response(200, content=b"hello world")
        verify = Verify(body=ResponseBody(contains=["goodbye"]))
        with pytest.raises(VerificationError, match="Body doesn't contain 'goodbye'"):
            process_verify(verify, response)

    def test_verify_body_not_contains_failure(self):
        response = httpx.Response(200, content=b"hello world")
        verify = Verify(body=ResponseBody(not_contains=["hello"]))
        with pytest.raises(VerificationError, match="Body contains 'hello' while it shouldn't"):
            process_verify(verify, response)

    def test_verify_body_matches_failure(self):
        response = httpx.Response(200, content=b"hello world")
        verify = Verify(body=ResponseBody(matches=["z{3}"]))
        with pytest.raises(VerificationError, match="Body doesn't match 'z"):
            process_verify(verify, response)

    def test_verify_body_not_matches_failure(self):
        response = httpx.Response(200, content=b"hello world")
        verify = Verify(body=ResponseBody(not_matches=["wor"]))
        with pytest.raises(VerificationError, match="Body matches 'wor' while it shouldn't"):
            process_verify(verify, response)


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
        **fresh_scenario_state(),
        "global_context": ChainMap(),
        "_initialized": True,
        "max_parallel_iterations": 10_000,
    }
    defaults.update(attrs)
    return type("UnitCarrier", (Carrier,), defaults)


class TestSSLClientWiring:
    """SSLConfig -> httpx.Client kwargs, the ssl branches of _ensure_initialized.

    httpx 0.28 deprecates ``verify=<str>`` and ``cert=...``: bool verify passes
    through untouched and everything else must arrive as a ready
    ``ssl.SSLContext``. trustme issues real throwaway PEMs so context
    construction actually parses certificates; httpx.Client is captured so the
    tests assert exactly what the engine hands it. The real-client test at the
    end is the deprecation regression: ``filterwarnings = error`` escalates any
    DeprecationWarning from a genuine httpx.Client construction."""

    def _client_kwargs_for(self, monkeypatch, ssl_config: SSLConfig) -> dict:
        captured: dict = {}

        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        monkeypatch.setattr("pytest_httpchain.carrier.httpx.Client", FakeClient)
        cls = _make_carrier_subclass(
            _initialized=False,
            _context_resolved_at_collection=True,
            scenario=Scenario(ssl=ssl_config),
        )
        cls._ensure_initialized()
        return captured

    def test_verify_true_passed_through(self, monkeypatch):
        assert self._client_kwargs_for(monkeypatch, SSLConfig(verify=True))["verify"] is True

    def test_verify_false_passed_through(self, monkeypatch):
        assert self._client_kwargs_for(monkeypatch, SSLConfig(verify=False))["verify"] is False

    def test_verify_ca_bundle_file_builds_context(self, monkeypatch, tmp_path):
        ca_path = tmp_path / "ca.pem"
        trustme.CA().cert_pem.write_to_path(ca_path)
        kwargs = self._client_kwargs_for(monkeypatch, SSLConfig(verify=ca_path))
        assert isinstance(kwargs["verify"], ssl.SSLContext)

    def test_verify_ca_directory_builds_context(self, monkeypatch, tmp_path):
        kwargs = self._client_kwargs_for(monkeypatch, SSLConfig(verify=tmp_path))
        assert isinstance(kwargs["verify"], ssl.SSLContext)

    def test_cert_pair_loaded_into_context(self, monkeypatch, tmp_path):
        crt, key = tmp_path / "client.pem", tmp_path / "client.key"
        client_cert = trustme.CA().issue_cert("client@example.com")
        client_cert.cert_chain_pems[0].write_to_path(crt)
        client_cert.private_key_pem.write_to_path(key)
        kwargs = self._client_kwargs_for(monkeypatch, SSLConfig(cert=(crt, key)))
        assert isinstance(kwargs["verify"], ssl.SSLContext)
        assert "cert" not in kwargs

    def test_single_cert_file_loaded_into_context(self, monkeypatch, tmp_path):
        bundle = tmp_path / "client-bundle.pem"
        client_cert = trustme.CA().issue_cert("client@example.com")
        client_cert.private_key_and_cert_chain_pem.write_to_path(bundle)
        kwargs = self._client_kwargs_for(monkeypatch, SSLConfig(cert=bundle))
        assert isinstance(kwargs["verify"], ssl.SSLContext)
        assert "cert" not in kwargs

    def test_verify_false_with_cert_keeps_verification_off(self, monkeypatch, tmp_path):
        bundle = tmp_path / "client-bundle.pem"
        trustme.CA().issue_cert("client@example.com").private_key_and_cert_chain_pem.write_to_path(bundle)
        ctx = self._client_kwargs_for(monkeypatch, SSLConfig(verify=False, cert=bundle))["verify"]
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.verify_mode == ssl.CERT_NONE
        assert ctx.check_hostname is False

    def test_real_client_construction_emits_no_deprecation(self, tmp_path):
        """Regression for httpx 0.28: a genuine httpx.Client built from a CA
        path plus client cert pair must construct without warnings."""
        ca = trustme.CA()
        ca_path = tmp_path / "ca.pem"
        ca.cert_pem.write_to_path(ca_path)
        crt, key = tmp_path / "client.pem", tmp_path / "client.key"
        client_cert = ca.issue_cert("client@example.com")
        client_cert.cert_chain_pems[0].write_to_path(crt)
        client_cert.private_key_pem.write_to_path(key)
        cls = _make_carrier_subclass(
            _initialized=False,
            _context_resolved_at_collection=True,
            scenario=Scenario(ssl=SSLConfig(verify=ca_path, cert=(crt, key))),
        )
        cls._ensure_initialized()
        try:
            assert isinstance(cls.client, httpx.Client)
        finally:
            cls.client.close()


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
            Carrier._execute_single_iteration(stage, ChainMap(), {}, limiter=limiter, max_rate_limit_delay=0.2)

    def test_limiter_blocks_until_timeout_elapses(self):

        limiter = Limiter(Rate(1, Duration.SECOND))
        assert limiter.try_acquire("api", blocking=True, timeout=2)

        stage = _make_stage()
        start = time.monotonic()
        with pytest.raises(RequestError, match="Rate limit exceeded"):
            Carrier._execute_single_iteration(stage, ChainMap(), {}, limiter=limiter, max_rate_limit_delay=0.3)
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

        assert '"a": 1' in _context_dump({"a": 1})

    def test_circular_context_degrades_to_placeholder(self):

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

        config = ParallelRepeatConfig(repeat=10**9)
        with pytest.raises(StageExecutionError, match="exceeds maximum"):
            Carrier._build_iteration_substitutions(config, max_parallel_iterations=10)

    def test_huge_foreach_product_rejected_before_expansion(self):

        config = ParallelForeachConfig(
            foreach=[
                IndividualParameter(individual={"a": list(range(5000))}),
                IndividualParameter(individual={"b": list(range(5000))}),
            ]
        )
        with pytest.raises(StageExecutionError, match="exceeds maximum"):
            Carrier._build_iteration_substitutions(config, max_parallel_iterations=10)

    def test_small_configs_still_expand(self):

        result = Carrier._build_iteration_substitutions(ParallelRepeatConfig(repeat=3), max_parallel_iterations=10)
        assert result == [{}, {}, {}]

    def test_non_parallel_single_iteration(self):
        assert Carrier._build_iteration_substitutions(None, max_parallel_iterations=10) == [{}]


class TestContextDumpNeverRaises:
    """The helper's contract is absolute: logging must never break a stage,
    whatever a user-function save put into the context."""

    def test_tuple_keyed_dict_degrades(self):

        out = _context_dump({"a": {(1, 2): 3}})
        assert "unserializable" in out

    def test_poison_str_degrades(self):

        class Poison:
            def __str__(self):
                raise RuntimeError("boom")

        out = _context_dump({"a": Poison()})
        assert "unserializable" in out


class TestRedirectExchangeRecording:
    """Redirect hops from response.history become their own exchanges: with
    HAR recording on, every wire exchange lands in last_exchanges instead of
    only the post-redirect one."""

    @staticmethod
    def _redirect_chain():
        hop_req = httpx.Request("GET", "http://t/a")
        hop = httpx.Response(302, request=hop_req, headers={"location": "http://t/b"})
        final_req = httpx.Request("GET", "http://t/b")
        final = httpx.Response(200, request=final_req, history=[hop])
        return hop_req, hop, final_req, final

    def test_hops_expanded_when_recording_all(self):

        hop_req, hop, final_req, final = self._redirect_chain()
        started = datetime.now(UTC)
        result = IterationResult(saved_context={}, request=final_req, response=final, started=started)

        cls = _make_carrier_subclass(record_all_exchanges=True)
        cls._record_exchanges([result], None, 1)

        # Hops carry the iteration's start (the first hop IS the request sent
        # then) rather than None, which the HAR writer would replace with
        # export time — placing the hop after the response that followed it.
        assert cls.last_exchanges == [(hop_req, hop, started), (final_req, final, started)]
        # The report still shows the final exchange.
        assert cls.last_request is final_req
        assert cls.last_response is final

    def test_only_final_exchange_kept_without_har(self):

        _, _, final_req, final = self._redirect_chain()
        started = datetime.now(UTC)
        result = IterationResult(saved_context={}, request=final_req, response=final, started=started)

        cls = _make_carrier_subclass()
        cls._record_exchanges([result], None, 1)

        assert cls.last_exchanges == [(final_req, final, started)]


class TestRedirectKwargsMapping:
    """The model's allow_redirects must reach httpx as follow_redirects: httpx
    defaults the kwarg to False, so silently dropping the mapping would flip
    the plugin's documented follow-by-default behavior with the suite green."""

    def test_default_follows(self):
        model = Request.model_validate({"url": "http://t/"})
        assert build_request_kwargs(model, None)["follow_redirects"] is True

    def test_disabled_passes_false(self):
        model = Request.model_validate({"url": "http://t/", "allow_redirects": False})
        assert build_request_kwargs(model, None)["follow_redirects"] is False


class TestParallelCancellation:
    """The iteration pool must be cancellable: without it, an escaping
    exception (KeyboardInterrupt, a plugin bug) reaches the executor exit,
    which runs every queued iteration to completion — an unstoppable load test."""

    def test_unexpected_error_cancels_queued_iterations(self):
        calls: list[int] = []

        def fake_iteration(cls, stage, local_context, iter_vars, limiter=None, max_rate_limit_delay=60, cancel=None):
            calls.append(1)
            raise RuntimeError("plugin bug")

        cls = _make_carrier_subclass(_execute_single_iteration=classmethod(fake_iteration))
        config = ParallelRepeatConfig.model_validate({"repeat": 40, "max_concurrency": 1})

        with pytest.raises(RuntimeError, match="plugin bug"):
            cls._run_iterations(None, ChainMap(), [{} for _ in range(40)], config)

        # The queued iterations were cancelled, not drained. A worker may have
        # started one or two before the cancel landed; 40 means no cancellation.
        assert len(calls) < 20, f"{len(calls)} iterations ran after the failure"

    def test_rate_slot_wait_interrupted_by_cancellation(self):

        limiter = Limiter(Rate(1, Duration.SECOND))
        try:
            assert limiter.try_acquire("api", blocking=False)  # drain the bucket
            cancel = threading.Event()
            cancel.set()
            start = time.monotonic()
            assert Carrier._acquire_rate_slot(limiter, timeout=30, cancel=cancel) is False
            # Far below the 30s timeout: the cancellation interrupted the wait.
            assert time.monotonic() - start < 5
        finally:
            limiter.close()


class TestFailedExchangeShownFlag:
    """A failure that never recorded a request (template error, rate-limit
    timeout) falls back to showing the last COMPLETED exchange, which the
    report must not label as the failing one."""

    @staticmethod
    def _completed_result():

        req = httpx.Request("GET", "http://t/x")
        resp = httpx.Response(200, request=req)
        return IterationResult(saved_context={}, request=req, response=resp, started=datetime.now(UTC)), req

    def test_failure_without_request_info_not_marked_failing(self):

        result, req = self._completed_result()
        cls = _make_carrier_subclass()
        cls._record_exchanges([result], failed=TemplatesError("undefined variable"), attempted=3)

        assert cls.last_shown_exchange_is_failed is False
        assert cls.last_request is req

    def test_failure_with_request_info_marked_failing(self):
        result, _ = self._completed_result()
        failed_req = httpx.Request("GET", "http://t/failed")
        failed_resp = httpx.Response(400, request=failed_req)
        cls = _make_carrier_subclass()
        cls._record_exchanges([result], failed=RequestError("bad", request=failed_req, response=failed_resp), attempted=3)

        assert cls.last_shown_exchange_is_failed is True
        assert cls.last_request is failed_req


class TestScenarioRerunReset:
    """teardown_class must return the class to fresh_scenario_state and the
    pristine base context, so a rerun plugin's second pass actually
    re-executes the chain instead of replaying stale saves or skipping."""

    def test_execute_teardown_execute_replays_cleanly(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"v": 1})

        monkeypatch.setattr(
            "pytest_httpchain.carrier.build_client_kwargs",
            lambda *args, **kwargs: {"transport": httpx.MockTransport(handler)},
        )

        scenario = Scenario.model_validate(
            {
                "substitutions": [{"vars": {"base": 1}}],
                "stages": [
                    {
                        "name": "s",
                        "request": {"url": "http://mock/ok"},
                        "response": [{"save": {"jmespath": {"v": "v"}}}, {"verify": {"status": 200}}],
                    }
                ],
            }
        )
        cls = _make_carrier_subclass(scenario=scenario, _initialized=False)
        stage = scenario.stages[0]

        cls.execute_stage(stage, {})
        first_client = cls.client
        assert cls._initialized is True
        assert cls.global_context["v"] == 1

        cls.teardown_class()
        assert first_client is not None
        assert first_client.is_closed
        for name, value in fresh_scenario_state().items():
            assert getattr(cls, name) == value, f"{name} not reset"
        # Saves are gone; the pristine scenario context survives.
        assert dict(cls.global_context) == {"base": 1}

        # The second pass re-initializes (fresh client) and replays the chain.
        cls.execute_stage(stage, {})
        assert cls.client is not first_client
        assert cls.global_context["v"] == 1
