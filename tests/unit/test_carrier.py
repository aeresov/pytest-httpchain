"""carrier.py: chain state, the iteration matrix, threading and reporting.

What a single request or response step means lives in request_builder and
response_steps (test_request_builder.py, test_response_steps.py); the HTTP
round trip itself is the integration suite's.
"""

import contextvars
import re
import ssl
import threading
import time
from collections import ChainMap
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import trustme
from pydantic import ValidationError
from pyrate_limiter import Duration, Limiter, Rate

import pytest_httpchain.carrier as carrier_module
from pytest_httpchain.carrier import (
    Carrier,
    IterationResult,
    _context_dump,
    _parallel_int,
    _parallel_number,
    _render_declared,
    fresh_chain_state,
    fresh_scenario_state,
)
from pytest_httpchain.errors import RequestError, SaveError, StageExecutionError, VerificationError
from pytest_httpchain.models import (
    CombinationsParameter,
    IndividualParameter,
    ParallelForeachConfig,
    ParallelRepeatConfig,
    Request,
    ResponseBody,
    Scenario,
    SSLConfig,
    Stage,
    UserFunctionName,
    VarsSubstitution,
    Verify,
)
from pytest_httpchain.redaction import DEFAULT_REDACTION, NO_REDACTION
from pytest_httpchain.templates import TemplatesError
from tests.unit.models.helpers import make_stage


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

    @pytest.fixture
    def pems(self, tmp_path):
        """Throwaway PEMs from one CA: its bundle, a client cert/key pair and
        the single-file key+chain form of the same client cert."""
        ca = trustme.CA()
        client = ca.issue_cert("client@example.com")
        pems = SimpleNamespace(dir=tmp_path, ca=tmp_path / "ca.pem", pair=(tmp_path / "client.pem", tmp_path / "client.key"), bundle=tmp_path / "bundle.pem")
        ca.cert_pem.write_to_path(pems.ca)
        client.cert_chain_pems[0].write_to_path(pems.pair[0])
        client.private_key_pem.write_to_path(pems.pair[1])
        client.private_key_and_cert_chain_pem.write_to_path(pems.bundle)
        return pems

    @pytest.mark.parametrize("verify", [True, False])
    def test_bool_verify_passed_through(self, monkeypatch, verify):
        assert self._client_kwargs_for(monkeypatch, SSLConfig(verify=verify))["verify"] is verify

    @pytest.mark.parametrize(
        "config",
        [
            pytest.param(lambda p: SSLConfig(verify=p.ca), id="ca-bundle-file"),
            pytest.param(lambda p: SSLConfig(verify=p.dir), id="ca-directory"),
            pytest.param(lambda p: SSLConfig(cert=p.pair), id="cert-pair"),
            pytest.param(lambda p: SSLConfig(cert=p.bundle), id="single-cert-file"),
        ],
    )
    def test_paths_arrive_as_ssl_context(self, monkeypatch, pems, config):
        kwargs = self._client_kwargs_for(monkeypatch, config(pems))
        assert isinstance(kwargs["verify"], ssl.SSLContext)
        assert "cert" not in kwargs

    def test_verify_false_with_cert_keeps_verification_off(self, monkeypatch, pems):
        ctx = self._client_kwargs_for(monkeypatch, SSLConfig(verify=False, cert=pems.bundle))["verify"]
        assert (ctx.verify_mode, ctx.check_hostname) == (ssl.CERT_NONE, False)

    def test_real_client_construction_emits_no_deprecation(self, pems):
        """Regression for httpx 0.28: a genuine httpx.Client built from a CA
        path plus client cert pair must construct without warnings."""
        cls = _make_carrier_subclass(
            _initialized=False,
            _context_resolved_at_collection=True,
            scenario=Scenario(ssl=SSLConfig(verify=pems.ca, cert=pems.pair)),
        )
        cls._ensure_initialized()
        try:
            assert isinstance(cls.client, httpx.Client)
        finally:
            cls.client.close()


def test_exhausted_rate_limit_blocks_for_the_delay_then_fails():
    """The limiter really blocks, and times out into a stage failure (M2)."""
    limiter = Limiter(Rate(1, Duration.SECOND))
    try:
        assert limiter.try_acquire("api", blocking=True, timeout=2)  # consume the only slot
        start = time.monotonic()
        with pytest.raises(RequestError, match="Rate limit exceeded"):
            # The limiter check precedes the HTTP request, so no client is needed.
            Carrier._execute_single_iteration(make_stage(), ChainMap(), {}, limiter=limiter, max_rate_limit_delay=0.3)
        assert time.monotonic() - start >= 0.25
    finally:
        limiter.close()


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

    def test_residual_template_fails_the_stage_cleanly(self):
        # A substitution holding '{{ 2 }}' resolves to that text, which satisfies
        # NumberOrTemplate on walk()'s re-validation; int() of it raised a bare
        # ValueError, which no stage-failure path catches.
        carrier = _make_carrier_subclass(global_context=ChainMap({"rate": "{{ 2 }}"}))
        stage = make_stage(parallel=ParallelRepeatConfig.model_validate({"repeat": 2, "max_concurrency": 2, "calls_per_sec": "{{ rate }}"}))
        with pytest.raises(pytest.fail.Exception, match="calls_per_sec"):
            carrier.execute_stage(stage, {})

    @pytest.mark.parametrize(
        ("step", "field"),
        [
            # Iterated as text, the residual template ran one iteration per character.
            (IndividualParameter(individual={"v": "{{ vals }}"}), "individual 'v'"),
            # Iterated as text, it escaped as a bare TypeError ('str' is not a mapping).
            (CombinationsParameter(combinations="{{ vals }}"), "combinations"),
        ],
        ids=["individual", "combinations"],
    )
    def test_residual_template_foreach_fails_the_stage_cleanly(self, step, field):
        # Both foreach step kinds also accept a template, so a substitution holding
        # '{{ x }}' resolves to text that passes walk()'s re-validation.
        carrier = _make_carrier_subclass(global_context=ChainMap({"vals": "{{ x }}"}))
        stage = make_stage(parallel=ParallelForeachConfig(foreach=[step]))
        with pytest.raises(pytest.fail.Exception, match=f"parallel.foreach {field} must resolve to a list"):
            carrier.execute_stage(stage, {})

    @pytest.mark.parametrize("template", ["{{ combos }}", "{{ tuple(combos) }}"])
    def test_foreach_combinations_template_over_vars(self, template):
        """``vars`` turns each object into a SimpleNamespace, and walk()'s
        re-validation of the rendered config refused them as not dicts: the
        stage failed with pydantic's report, while the same template worked in
        stage ``parametrize``. A nested object keeps its attribute access, and
        a tuple of them is taken like a list."""
        requests: list[httpx.Request] = []
        client = httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200)))
        substitution = VarsSubstitution(vars={"combos": [{"id": 1, "owner": {"name": "a"}}, {"id": 2, "owner": {"name": "b"}}]})
        cls = _make_carrier_subclass(client=client, global_context=ChainMap(substitution.vars))
        stage = Stage.model_validate({"name": "s", "parallel": {"foreach": [{"combinations": template}]}, "request": {"url": "http://mock/item/{{ id }}/{{ owner.name }}"}})
        try:
            cls.execute_stage(stage, {})
        finally:
            client.close()
        assert sorted(str(request.url) for request in requests) == ["http://mock/item/1/a", "http://mock/item/2/b"]

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
    def test_run_iterations_rejects_unusable_setting(self, field, value):
        settings = {"repeat": 2, "max_concurrency": 2, "calls_per_sec": None, "max_rate_limit_delay": 60, field: value}
        with pytest.raises(StageExecutionError, match=field):
            _make_carrier_subclass()._run_iterations(make_stage(), ChainMap(), [{}, {}], ParallelRepeatConfig.model_construct(**settings), [])


def _header_matcher(field: str) -> Verify:
    """A matcher whose ``field`` is templated beside a static check, so it
    still re-validates (a matcher needs one field set) once ``field`` is gone."""
    static = "not_contains" if field == "contains" else "contains"
    return Verify.model_validate({"headers": {"Location": {field: "{{ x }}", static: "/items"}}})


class TestRenderedAwayFields:
    """A field the scenario declared, whose template rendered to None, fails the
    stage. walk() re-validates the rendered model, but an optional field accepts
    None, so it arrived looking undeclared: the assertion went unchecked, the
    setting unapplied, and the stage was green. Only ``status`` and
    ``body.schema`` used to be guarded; the guard now covers every model the
    carrier renders. Where validation rejects the None as well, the same message
    replaces pydantic's report for it."""

    RENDERS_NONE = ChainMap({"x": None})

    @pytest.mark.parametrize(
        ("where", "declared", "path"),
        [
            pytest.param("verify", Verify(status="{{ x }}"), "verify.status", id="verify-status"),
            pytest.param("verify", Verify(body=ResponseBody(schema="{{ x }}")), "verify.body.schema", id="verify-body-schema"),
            *(
                pytest.param("verify", _header_matcher(field), f"verify.headers.Location.{field}", id=f"header-{field}")
                for field in ("contains", "not_contains", "matches", "not_matches")
            ),
            # No limiter at all: the stage ran unthrottled.
            pytest.param("parallel", ParallelRepeatConfig.model_validate({"repeat": 2, "calls_per_sec": "{{ x }}"}), "parallel.calls_per_sec", id="calls-per-sec"),
            # Sent unauthenticated, or with the scenario-level credentials.
            pytest.param("request", Request.model_validate({"url": "http://t/", "auth": "{{ x }}"}), "request.auth", id="request-auth"),
            # Connected without the client certificate.
            pytest.param("ssl", SSLConfig(cert="{{ x }}"), "ssl.cert", id="ssl-cert"),
        ],
    )
    def test_rendered_away_field_is_refused(self, where, declared, path):
        with pytest.raises(StageExecutionError) as excinfo:
            _render_declared(declared, self.RENDERS_NONE, where)
        assert str(excinfo.value) == f"'{path}' was declared as " + "'{{ x }}' but rendered to None, which would silently disable it"

    @pytest.mark.parametrize(
        ("where", "declared", "path"),
        [
            # A matcher's only field: pydantic's report asked the scenario to
            # "set at least one of: contains, ..." — the field it did set.
            pytest.param("verify", Verify.model_validate({"headers": {"Location": {"contains": "{{ x }}"}}}), "verify.headers.Location.contains", id="single-field-matcher"),
            # A required field, which pydantic reported as "URL input should be
            # a string or URL", naming neither the template nor the None.
            pytest.param("request", Request.model_validate({"url": "{{ x }}"}), "request.url", id="required-field"),
            # A user-function name is a RootModel, dumped as its bare root value.
            pytest.param("auth", UserFunctionName("{{ x }}"), "auth", id="scenario-auth"),
            pytest.param("verify", Verify.model_validate({"user_functions": ["{{ x }}"]}), "verify.user_functions[0]", id="function-name-in-a-list"),
        ],
    )
    def test_rendered_away_field_validation_rejects_is_named(self, where, declared, path):
        """Nothing is silently disabled here — validation rejects the None — but
        the failure is still the guard's, naming the field and the template,
        without the "silently disable" clause that would not be true."""
        with pytest.raises(StageExecutionError) as excinfo:
            _render_declared(declared, self.RENDERS_NONE, where)
        assert str(excinfo.value) == f"'{path}' was declared as " + "'{{ x }}' but rendered to None"
        assert isinstance(excinfo.value.__cause__, ValidationError)

    @pytest.mark.parametrize(
        "template",
        [
            pytest.param("{{ {'contains': x, 'not_contains': 'error'} }}", id="built-by-the-expression"),
            # Saved whole from the response: a JMESPath multiselect such as
            # `{contains: no_such_key, not_contains: 'error'}` gives the key null.
            pytest.param("{{ matcher }}", id="saved-whole"),
        ],
    )
    def test_matcher_rendered_whole_is_refused_too(self, template):
        """A header matcher written as one template is a string until it renders,
        so its fields are known only from the validated matcher: a key the
        rendered mapping set to None is refused like a declared field (the static
        sibling kept the matcher valid, and ``contains`` went unchecked)."""
        context = self.RENDERS_NONE.new_child({"matcher": {"contains": None, "not_contains": "error"}})
        with pytest.raises(StageExecutionError) as excinfo:
            _render_declared(Verify.model_validate({"headers": {"Location": template}}), context, "verify")
        assert str(excinfo.value) == f"'verify.headers.Location.contains' was declared as {template!r} but rendered to None, which would silently disable it"

    @pytest.mark.parametrize(
        ("declared", "context", "path", "invalid"),
        [
            # None is a valid auth: before, the refusal alone was reported, and
            # the url error was only in its __cause__.
            pytest.param(Request.model_validate({"url": "{{ u }}", "auth": "{{ x }}"}), {"u": "not a url", "x": None}, "request.auth", "url", id="beside-a-none-that-validates"),
            pytest.param(
                Verify.model_validate({"status": "{{ u }}", "headers": {"H": {"contains": "{{ x }}", "not_contains": "e"}}}),
                {"u": 999, "x": None},
                "verify.headers.H.contains",
                "status",
                id="beside-a-matcher-field",
            ),
            # None is invalid for timeout too, but that is the refusal's to say:
            # the report lists only what else is wrong.
            pytest.param(
                Request.model_validate({"url": "{{ u }}", "timeout": "{{ x }}"}), {"u": "not a url", "x": None}, "request.timeout", "url", id="beside-a-none-that-is-rejected"
            ),
        ],
    )
    def test_other_validation_errors_are_reported_under_the_refusal(self, declared, context, path, invalid):
        """A rendered-away field does not hide a validation error it did not
        cause, nor take the blame for it: the refusal comes first, then
        pydantic's report on the rest, found by validating again with the
        declared templates back in place."""
        with pytest.raises(StageExecutionError) as excinfo:
            _render_declared(declared, context, path.split(".")[0])
        refusal, report = str(excinfo.value).split("\n", 1)
        assert refusal == f"'{path}' was declared as " + "'{{ x }}' but rendered to None"
        assert {line.split(".")[0] for line in report.splitlines()[1:] if not line.startswith(" ")} == {invalid}

    def test_invalid_value_other_than_none_keeps_the_validation_report(self):
        """Only a rendered-away field is the guard's to name: any other value
        validation rejects surfaces as pydantic reports it."""
        with pytest.raises(ValidationError, match="timeout"):
            _render_declared(Request.model_validate({"url": "http://t/", "timeout": "{{ x }}"}), {"x": "slow"}, "request")

    @pytest.mark.parametrize(
        ("declared", "context"),
        [
            pytest.param(Verify(), RENDERS_NONE, id="undeclared"),
            pytest.param(Verify(status="{{ x }}"), {"x": 200}, id="rendered-to-a-value"),
            # A JSON body of null, which request_builder sends as such.
            pytest.param(Request.model_validate({"url": "http://t/", "body": {"json": "{{ x }}"}}), RENDERS_NONE, id="json-body"),
            pytest.param(Verify(description="{{ x }}"), RENDERS_NONE, id="description"),
            # Values handed on rather than fields left undeclared: a query
            # parameter (httpx sends `?q=`), a user-function kwarg.
            pytest.param(Request.model_validate({"url": "http://t/", "params": {"q": "{{ x }}"}}), RENDERS_NONE, id="param-value"),
            pytest.param(Request.model_validate({"url": "http://t/", "auth": {"name": "mod:fn", "kwargs": {"token": "{{ x }}"}}}), RENDERS_NONE, id="kwarg-value"),
            # The same in a call a template rendered whole: kwargs is a map.
            pytest.param(
                Request.model_validate({"url": "http://t/", "auth": "{{ call }}"}), {"call": {"name": "mod:fn", "kwargs": {"token": None}}}, id="kwarg-value-rendered-whole"
            ),
            # A matcher rendered whole checks only the keys it has: a left-out
            # key was never declared.
            pytest.param(Verify.model_validate({"headers": {"Location": "{{ matcher }}"}}), {"matcher": {"not_contains": "error"}}, id="matcher-rendered-whole"),
        ],
    )
    def test_none_that_means_something_passes(self, declared, context):
        _render_declared(declared, context, "model")

    @pytest.mark.parametrize(
        ("stage", "path", "sent"),
        [
            pytest.param({"parallel": {"repeat": 2, "calls_per_sec": "{{ x }}"}}, "parallel.calls_per_sec", 0, id="parallel"),
            pytest.param({"request": {"url": "http://mock/ok", "auth": "{{ x }}"}}, "request.auth", 0, id="request"),
            pytest.param(
                {"response": [{"verify": {"headers": {"Location": {"contains": "{{ x }}", "not_contains": "error"}}}}]}, "verify.headers.Location.contains", 1, id="verify"
            ),
        ],
    )
    def test_stage_fails_instead_of_passing_green(self, stage, path, sent):
        """Each of a stage's render sites is guarded: against a server
        answering 200, every one of these stages used to pass. A bad setting
        fails the stage before anything goes on the wire."""
        requests = []
        client = httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, headers={"Location": "/items/1"})))
        cls = _make_carrier_subclass(client=client, global_context=self.RENDERS_NONE)
        try:
            with pytest.raises(pytest.fail.Exception, match=re.escape(f"'{path}' was declared as")):
                cls.execute_stage(Stage.model_validate({"name": "s", "request": {"url": "http://mock/ok"}, **stage}), {})
        finally:
            client.close()
        assert len(requests) == sent

    @pytest.mark.parametrize(
        ("stage", "error", "message"),
        [
            pytest.param(
                {"request": {"url": "http://mock/ok", "auth": "{{ x }}"}},
                RequestError,
                "'request.auth' was declared as '{{ x }}' but rendered to None, which would silently disable it",
                id="request",
            ),
            # Nothing to disable (a function name cannot be None), but the
            # failure is still the named SaveError, not pydantic's report.
            pytest.param(
                {"response": [{"save": {"user_functions": ["{{ x }}"]}}]}, SaveError, "'save.user_functions[0]' was declared as '{{ x }}' but rendered to None", id="save"
            ),
            pytest.param(
                {"response": [{"verify": {"status": "{{ x }}"}}]},
                VerificationError,
                "'verify.status' was declared as '{{ x }}' but rendered to None, which would silently disable it",
                id="verify",
            ),
        ],
    )
    def test_each_step_refuses_with_its_own_error(self, stage, error, message):
        """Every render site of an iteration goes through the guard, and the
        refusal is that step's failure type."""
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        cls = _make_carrier_subclass(client=client)
        try:
            with pytest.raises(StageExecutionError) as excinfo:
                cls._execute_single_iteration(Stage.model_validate({"name": "s", "request": {"url": "http://mock/ok"}, **stage}), self.RENDERS_NONE, {})
        finally:
            client.close()
        assert type(excinfo.value) is error
        assert str(excinfo.value) == message

    @pytest.mark.parametrize(
        ("declared", "message"),
        [
            pytest.param({"ssl": {"cert": "{{ x }}"}}, "'ssl.cert' was declared as '{{ x }}' but rendered to None, which would silently disable it", id="ssl"),
            pytest.param({"auth": "{{ x }}"}, "'auth' was declared as '{{ x }}' but rendered to None", id="auth"),
        ],
    )
    def test_scenario_initialization_fails(self, declared, message):
        """The scenario-level render sites: ``ssl`` and ``auth`` resolve once, at
        initialization."""
        scenario = Scenario.model_validate({"substitutions": [{"vars": {"x": None}}], **declared})
        cls = _make_carrier_subclass(scenario=scenario, _initialized=False)
        with pytest.raises(StageExecutionError, match=f"^{re.escape(f'Failed to initialize scenario: {message}')}$"):
            cls._ensure_initialized()


class TestVerifyObjectsFromVars:
    """``vars`` turns each object into a SimpleNamespace, and walk()'s
    re-validation of a rendered verify step refused one where the step takes an
    object: a ``body.schema`` or a header matcher written as one template over
    ``vars`` failed the stage with pydantic's report, while the same template
    over a saved value worked. Each is now the object it was declared as."""

    @staticmethod
    def _context(**values):
        return ChainMap(VarsSubstitution(vars=values).vars)

    @pytest.mark.parametrize(
        ("verify", "failure"),
        [
            pytest.param({"body": {"schema": "{{ schema }}"}}, "Body schema validation failed: '1' is not of type 'integer'", id="schema"),
            pytest.param({"headers": {"Content-Type": "{{ ct }}"}}, "Header 'Content-Type' (value: 'text/plain; charset=utf-8')", id="matcher"),
        ],
    )
    def test_checks_the_response(self, verify, failure):
        context = self._context(schema={"type": "object", "properties": {"id": {"type": "integer"}}}, ct={"contains": "json"})
        stage = Stage.model_validate({"name": "s", "request": {"url": "http://mock/ok"}, "response": [{"verify": verify}]})
        responses = iter([httpx.Response(200, json={"id": 1}), httpx.Response(200, text='{"id": "1"}')])
        client = httpx.Client(transport=httpx.MockTransport(lambda request: next(responses)))
        cls = _make_carrier_subclass(client=client)
        try:
            cls._execute_single_iteration(stage, context, {})
            with pytest.raises(VerificationError, match=f"^{re.escape(failure)}"):
                cls._execute_single_iteration(stage, context, {})
        finally:
            client.close()

    def test_matcher_key_set_to_none_is_refused(self):
        """As for a matcher saved whole from the response
        (``TestRenderedAwayFields``): a key it sets to None is refused."""
        context = self._context(matcher={"contains": None, "not_contains": "error"})
        with pytest.raises(StageExecutionError) as excinfo:
            _render_declared(Verify.model_validate({"headers": {"Location": "{{ matcher }}"}}), context, "verify")
        assert str(excinfo.value) == "'verify.headers.Location.contains' was declared as '{{ matcher }}' but rendered to None, which would silently disable it"


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (httpx.ReadTimeout("slow"), "HTTP request timed out: slow"),
        (httpx.ConnectError("refused"), "HTTP connection error: refused"),
        (httpx.RemoteProtocolError("garbled"), "HTTP request failed: garbled"),
        # User auth flows run inside request() and may raise anything.
        (RuntimeError("auth bug"), "Unexpected error during HTTP request: auth bug"),
    ],
    ids=["timeout", "connect", "other-httpx", "non-httpx"],
)
def test_transport_errors_become_request_errors(error, message):
    """Pinned here, not by the connection-refused/DNS integration tests: under
    in-process pytester a stale httpcore module can leave httpx's exception
    mapping undone, so those runs land in the catch-all whatever the cause."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    cls = _make_carrier_subclass(client=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        with pytest.raises(RequestError, match=f"^{message}$"):
            cls._execute_http_request({"method": "GET", "url": "http://t/"})
    finally:
        cls.client.close()


@pytest.mark.parametrize(
    ("redaction", "quoted"),
    [(DEFAULT_REDACTION, "b'[REDACTED]'"), (NO_REDACTION, "b'Bearer tok\\n'")],
    ids=["redacted", "disabled"],
)
def test_request_error_quoting_a_header_value_is_redacted(redaction, quoted):
    """h11 refuses a value ending in a newline (a token read from a file) as
    ``Illegal header value b'...'``, the real message pinned in
    tests/integration/test_redaction.py. The failure message prints above the
    request's report section and hides the value as that section does."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.LocalProtocolError(f"Illegal header value {request.headers.raw[-1][1]!r}")

    cls = _make_carrier_subclass(client=httpx.Client(transport=httpx.MockTransport(handler)), redaction=redaction)
    try:
        with pytest.raises(RequestError) as excinfo:
            cls._execute_http_request({"method": "GET", "url": "http://t/", "headers": {"Authorization": "Bearer tok\n"}})
    finally:
        cls.client.close()
    assert str(excinfo.value) == f"HTTP request failed: Illegal header value {quoted}"
    assert excinfo.value.request is not None


def test_cancelled_iteration_sends_nothing():
    """Once another iteration has failed, a queued one must not add traffic."""
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(RequestError, match="Iteration cancelled"):
        # client is None: reaching the send would fail differently.
        _make_carrier_subclass()._execute_single_iteration(make_stage(), ChainMap(), {}, cancel=cancel)


def test_initialization_failure_is_sticky(monkeypatch):
    """Initialization runs at most once: after a failure, later stages get the
    same error without substitutions (or auth) being re-invoked."""
    calls = []

    def failing_substitutions(substitutions):
        calls.append(substitutions)
        raise RuntimeError("token service unreachable")

    monkeypatch.setattr(carrier_module, "process_substitutions", failing_substitutions)
    cls = _make_carrier_subclass(scenario=Scenario(), _initialized=False)
    for _ in range(2):
        with pytest.raises(StageExecutionError, match="Failed to initialize scenario: token service unreachable"):
            cls._ensure_initialized()
    assert len(calls) == 1


class TestParallelIterationCap:
    """Exceeding max_parallel_iterations is rejected before any request runs."""

    def test_exceeding_cap_fails(self):
        carrier = _make_carrier_subclass(max_parallel_iterations=2)
        stage = make_stage(parallel=ParallelRepeatConfig(repeat=5))

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
        stage = make_stage(parallel=ParallelRepeatConfig(repeat=3))

        with pytest.raises(pytest.fail.Exception) as excinfo:
            carrier.execute_stage(stage, {})
        assert "exceeds maximum" not in str(excinfo.value)


# What iteration 1's context manager raises on exit in the parallel stages of
# `TestContextManagerFixtureCleanup` (a straggler's, or a cancelled one's), as the stage reports it.
_ITERATION_1_EXIT_ERROR = "Iteration 1: Exiting the context manager from fixture 'a' failed: RuntimeError: commit of 1 rejected"

# How the parallel stages of `TestContextManagerFixtureCleanup` fail when iteration 0 gets a 500.
_ITERATION_0_FAILED = "Parallel execution failed at iteration 0: Status code doesn't match: expected 200, got 500"

# Both orders the stage's thread can read a straggler's outcome in: after that
# of the iteration that ended the stage, or before it, while that one's exits
# still run (`TestContextManagerFixtureCleanup._run_with_a_straggler`).
_EITHER_READ_ORDER = pytest.mark.parametrize("straggler_read_first", [False, True], ids=["straggler-read-last", "straggler-read-first"])


class _UnprintableError(Exception):
    """An exception whose ``__str__`` raises in turn."""

    def __str__(self):
        raise RuntimeError("cannot describe this error")


def _wait_until(condition: Callable[[], bool], timeout: float = 5) -> None:
    """Poll ``condition`` until it holds, giving up after ``timeout`` seconds
    so that a broken ordering fails its test's assertions instead of hanging."""
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)


class TestContextManagerFixtureCleanup:
    """A factory fixture's context manager is entered on use and exited when
    the iteration that entered it ends, in the thread that ran it (one entered
    outside an iteration, when the stage ends), however it ends, and before the
    stage commits its saves: pytest tears the stage's fixtures down right after
    the stage, and such a context manager is typically built on them. They were
    exited only at class teardown, after pytest had already torn those fixtures
    down, and an error on exit was only logged (the stage and the run stayed
    green)."""

    @staticmethod
    def _resource(events: list[str], tag: str, exit_error: BaseException | None = None):
        """A factory fixture returning a context manager that records its
        enter and exit, raising ``exit_error`` on exit."""

        @contextmanager
        def resource():
            events.append(f"enter {tag}")
            yield tag
            events.append(f"exit {tag}")
            if exit_error is not None:
                raise exit_error

        return resource

    @staticmethod
    def _run(fixtures: dict, status: int = 200, carrier: type[Carrier] | None = None, **stage_fields) -> type[Carrier]:
        """Run one stage requesting ``/{{ a() }}/{{ b() }}`` from a mock server
        answering ``status``, on ``carrier`` (a fresh one by default); the stage verifies a 200."""
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status)))
        cls = carrier if carrier is not None else _make_carrier_subclass()
        cls.client = client
        fields = {"name": "s", "request": {"url": "http://mock/{{ a() }}/{{ b() }}"}, "response": [{"verify": {"status": 200}}], **stage_fields}
        try:
            cls.execute_stage(Stage.model_validate(fields), fixtures)
        finally:
            client.close()
        return cls

    def test_exited_last_first_when_the_stage_passes(self):
        events: list[str] = []
        cls = self._run({"a": self._resource(events, "a"), "b": self._resource(events, "b")})
        assert events == ["enter a", "enter b", "exit b", "exit a"]
        assert cls.active_context_managers == []

    def test_exited_when_the_stage_fails(self):
        events: list[str] = []
        with pytest.raises(pytest.fail.Exception, match=r"^Status code doesn't match: expected 200, got 500$"):
            self._run({"a": self._resource(events, "a"), "b": self._resource(events, "b")}, status=500)
        assert events == ["enter a", "enter b", "exit b", "exit a"]

    @pytest.mark.parametrize(
        ("status", "exit_error", "message"),
        [
            pytest.param(200, RuntimeError("rollback failed"), "Exiting the context manager from fixture 'b' failed: RuntimeError: rollback failed", id="stage-passed"),
            # The stage's own failure stays the primary error.
            pytest.param(
                500,
                RuntimeError("rollback failed"),
                "Status code doesn't match: expected 200, got 500\nExiting the context manager from fixture 'b' failed: RuntimeError: rollback failed",
                id="stage-failed",
            ),
            # An exit's pytest.fail() is a BaseException, which the class
            # teardown's cleanup did not catch: it escaped as a teardown error
            # of its own, and ``a``, entered first, was never exited.
            pytest.param(
                500,
                pytest.fail.Exception("b exit says no"),
                "Status code doesn't match: expected 200, got 500\nExiting the context manager from fixture 'b' failed: Failed: b exit says no",
                id="pytest-fail-on-exit",
            ),
            # Formatting the error raised too, and escaped the loop of exits:
            # ``a`` was never exited, and the stage ended in that raw error.
            pytest.param(
                200,
                _UnprintableError(),
                "Exiting the context manager from fixture 'b' failed: _UnprintableError: <exception str() failed>",
                id="unprintable-error-on-exit",
            ),
        ],
    )
    def test_error_on_exit_fails_the_stage(self, status, exit_error, message):
        """``b`` raises on exit, and ``a``, entered first, is exited after it
        all the same. The request went on the wire whatever the exit did, so
        the stage's failure still shows its exchange, in the report and the HAR."""
        events: list[str] = []
        cls = _make_carrier_subclass(record_all_exchanges=True)
        with pytest.raises(pytest.fail.Exception) as excinfo:
            self._run({"a": self._resource(events, "a"), "b": self._resource(events, "b", exit_error)}, status=status, carrier=cls)
        assert str(excinfo.value) == message
        assert events == ["enter a", "enter b", "exit b", "exit a"]
        assert [(str(request.url), response.status_code) for request, response, _ in cls.last_exchanges] == [("http://mock/a/b", status)]
        assert cls.last_shown_exchange_is_failed

    @pytest.mark.parametrize(
        "stage_fields",
        [
            pytest.param({"request": {"url": "http://mock/{{ a() }}"}}, id="entered-by-the-iteration"),
            pytest.param({"substitutions": [{"vars": {"t": "{{ a() }}"}}], "request": {"url": "http://mock/{{ t }}"}}, id="entered-by-the-stage"),
        ],
    )
    def test_a_stage_failed_on_exit_commits_no_saves(self, stage_fields):
        """Like any failed stage: a cleanup stage guarded by ``exists()`` must
        not find an id whose transaction was never committed. The exit came at
        class teardown, once every stage, the cleanup included, had run with
        the save committed, and its error failed nothing."""
        events: list[str] = []
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        cls = _make_carrier_subclass(client=client)
        stage = Stage.model_validate({"name": "s", "response": [{"save": {"substitutions": [{"vars": {"resource_id": 42}}]}}], **stage_fields})
        try:
            with pytest.raises(pytest.fail.Exception, match=r"^Exiting the context manager from fixture 'a' failed: RuntimeError: commit rejected$"):
                cls.execute_stage(stage, {"a": self._resource(events, "a", RuntimeError("commit rejected"))})
        finally:
            client.close()
        assert events == ["enter a", "exit a"]
        assert "resource_id" not in cls.global_context

    def test_an_iteration_exits_before_the_stage(self):
        """What the request entered is exited with its iteration, before what
        the stage's substitutions entered."""
        events: list[str] = []
        self._run(
            {"a": self._resource(events, "a"), "b": self._resource(events, "b")}, substitutions=[{"vars": {"t": "{{ a() }}"}}], request={"url": "http://mock/{{ t }}/{{ b() }}"}
        )
        assert events == ["enter a", "enter b", "exit b", "exit a"]

    def test_each_parallel_iteration_exits_its_own_when_it_ends(self):
        """Not once the whole pool is done, let alone at class teardown, where
        they were exited: holding a transaction or a pooled connection open
        that long blocks the iterations still running."""
        events: list[str] = []
        self._run({"a": self._resource(events, "a"), "b": lambda: "b"}, parallel={"repeat": 3, "max_concurrency": 1})
        assert events == ["enter a", "exit a"] * 3

    def test_a_parallel_iteration_exits_in_its_own_thread(self):
        """A thread-bound context manager (``sqlite3``'s connection) cannot be
        exited from another thread. The pool's iterations were exited at class
        teardown, on pytest's thread, where such an exit raises: the error was
        only logged."""
        threads: list[tuple[int, int]] = []

        class ThreadBound:
            def __enter__(self):
                self.owner = threading.get_ident()
                return "t"

            def __exit__(self, *exc_info):
                threads.append((self.owner, threading.get_ident()))
                if threading.get_ident() != self.owner:
                    raise RuntimeError("exited from another thread")

        self._run({"a": lambda: ThreadBound(), "b": lambda: "b"}, parallel={"repeat": 4, "max_concurrency": 4})
        assert len(threads) == 4
        assert all(owner == exited != threading.get_ident() for owner, exited in threads)

    def _run_with_a_straggler(
        self,
        cls: type[Carrier],
        events: list[str],
        status: int = 200,
        end=None,
        straggler_status: int = 200,
        straggler_end=None,
        straggler_commit_fails: bool = True,
        straggler_read_first: bool = False,
        stage_exit_error: BaseException | None = None,
    ) -> None:
        """Run a parallel stage of two iterations, both in flight at once.
        Iteration 0 gets ``status``, then ends with ``end`` in its response
        steps while iteration 1, the straggler, still waits for its response.
        The straggler then gets ``straggler_status`` and ends with
        ``straggler_end``, and its ``a`` raises on exit if
        ``straggler_commit_fails``. Given ``stage_exit_error``, the stage's
        substitutions enter a context manager raising it on exit.

        The straggler is answered once iteration 0 has ended and begun its
        exits. The stage's thread then reads iteration 0's outcome first, or,
        with ``straggler_read_first``, the straggler's: iteration 0's exits
        wait for it to end, as a slow one (a rollback) would."""
        both_sent = threading.Barrier(2)

        def answer(request):
            both_sent.wait(timeout=5)
            if request.url.path == "/0":
                return httpx.Response(status)
            _wait_until(lambda: "exit 0" in events)
            if not straggler_read_first:
                # Long enough for the stage's thread to take iteration 0's outcome first.
                time.sleep(0.2)
            return httpx.Response(straggler_status)

        @contextmanager
        def a(i):
            events.append(f"enter {i}")
            yield i
            events.append(f"exit {i}")
            if i == 0 and straggler_read_first:
                # Until the straggler has ended, and then long enough for the
                # stage's thread to take its outcome first.
                _wait_until(lambda: "exit 1" in events)
                time.sleep(0.1)
            if i == 1 and straggler_commit_fails:
                raise RuntimeError(f"commit of {i} rejected")

        def b(i):
            ending = end if i == 0 else straggler_end
            if ending is not None:
                ending("given up")
            return True

        fixtures = {"a": a, "b": b}
        stage_fields: dict = {}
        if stage_exit_error is not None:
            fixtures["s"] = self._resource(events, "s", stage_exit_error)
            stage_fields["substitutions"] = [{"vars": {"t": "{{ s() }}"}}]
        cls.client = httpx.Client(transport=httpx.MockTransport(answer))
        stage = Stage.model_validate(
            {
                "name": "s",
                "parallel": {"foreach": [{"individual": {"i": [0, 1]}}], "max_concurrency": 2},
                "request": {"url": "http://mock/{{ i }}?t={{ a(i) }}"},
                "response": [{"verify": {"status": 200}}, {"verify": {"expressions": ["{{ b(i) }}"]}}],
                **stage_fields,
            }
        )
        try:
            cls.execute_stage(stage, fixtures)
        finally:
            cls.client.close()

    @pytest.mark.parametrize(
        ("status", "end", "straggler_end", "message", "exchanges"),
        [
            # The straggler's request went on the wire, so its exchange is in
            # the HAR, before the stage's failing one, which is recorded last
            # for the report to show.
            pytest.param(
                500,
                None,
                None,
                f"Parallel execution failed at iteration 0: Status code doesn't match: expected 200, got 500\n{_ITERATION_1_EXIT_ERROR}",
                ["/1?t=1", "/0?t=0"],
                id="stage-failed",
            ),
            # The straggler's own skip, xfail or pytest.fail() is secondary to
            # the stage's failure, like any failure of its own. Re-raised, it
            # turned the failed stage into a skipped or xfailed one, or
            # replaced its failure.
            *(
                pytest.param(
                    500,
                    None,
                    straggler_end,
                    f"Parallel execution failed at iteration 0: Status code doesn't match: expected 200, got 500\n{_ITERATION_1_EXIT_ERROR}",
                    ["/0?t=0"],
                    id=f"stage-failed-straggler-{name}",
                )
                for straggler_end, name in ((pytest.skip, "skipped"), (pytest.xfail, "xfailed"), (pytest.fail, "failed"))
            ),
            # A skip is no failure, so the straggler's error on exit fails the
            # stage, as it does a skipped stage's own.
            pytest.param(200, pytest.skip, None, _ITERATION_1_EXIT_ERROR, [], id="stage-skipped"),
        ],
    )
    @_EITHER_READ_ORDER
    def test_error_on_exit_of_a_straggler_is_reported(self, status, end, straggler_end, message, exchanges, straggler_read_first):
        """An iteration still running when another one ends the stage exits its
        own when it ends, and what its exit raises is listed after the stage's
        own failure, labelled with the iteration: a commit that failed is a
        side effect to hear of. Both iterations are exited, the straggler last.

        Read before the stage's failure, while that iteration's exits still
        ran, the straggler failed on exit was taken for the stage's failure,
        and the failure that ended the stage was dropped."""
        events: list[str] = []
        cls = _make_carrier_subclass(record_all_exchanges=True)
        # Both caught, so a wrong outcome fails this test rather than skipping it.
        with pytest.raises((pytest.skip.Exception, pytest.fail.Exception)) as excinfo:
            self._run_with_a_straggler(cls, events, status=status, end=end, straggler_end=straggler_end, straggler_read_first=straggler_read_first)
        assert excinfo.type is pytest.fail.Exception
        assert str(excinfo.value) == message
        assert sorted(events[:2]) == ["enter 0", "enter 1"]
        assert events[2:] == ["exit 0", "exit 1"]
        assert [str(request.url).removeprefix("http://mock") for request, _, _ in cls.last_exchanges] == exchanges

    @pytest.mark.parametrize(
        ("straggler_status", "straggler_end"),
        [
            pytest.param(200, pytest.skip, id="skipped"),
            pytest.param(200, pytest.xfail, id="xfailed"),
            pytest.param(200, pytest.fail, id="failed"),
            pytest.param(404, None, id="failed-verification"),
        ],
    )
    @_EITHER_READ_ORDER
    def test_a_stragglers_own_outcome_is_secondary(self, straggler_status, straggler_end, straggler_read_first):
        """A straggler exiting cleanly, then failing its verification, or
        skipping, xfailing or failing from a user function, leaves the stage's
        failure as it is. Reading it re-raised a skip, an xfail or a
        pytest.fail(), which turned the failed stage into a skipped or xfailed
        one, or replaced its failure message. And read before the stage's
        failure, while the exits of the iteration that failed it still ran
        (a rollback), any such outcome was taken for the stage's, the
        straggler's failed verification included."""
        events: list[str] = []
        # Both caught, so a wrong outcome fails this test rather than skipping it.
        with pytest.raises((pytest.skip.Exception, pytest.fail.Exception)) as excinfo:
            self._run_with_a_straggler(
                _make_carrier_subclass(),
                events,
                status=500,
                straggler_status=straggler_status,
                straggler_end=straggler_end,
                straggler_commit_fails=False,
                straggler_read_first=straggler_read_first,
            )
        assert excinfo.type is pytest.fail.Exception
        assert str(excinfo.value) == _ITERATION_0_FAILED
        assert events[2:] == ["exit 0", "exit 1"]

    @pytest.mark.parametrize(
        ("status", "exit_error", "end", "raised", "cancelled_on_exit"),
        [
            pytest.param(200, None, None, None, False, id="passed"),
            pytest.param(500, None, None, VerificationError, True, id="failed"),
            pytest.param(200, None, pytest.skip, pytest.skip.Exception, True, id="skipped"),
            pytest.param(200, None, KeyboardInterrupt, KeyboardInterrupt, True, id="interrupted"),
            # Failed by its exit, which is only known once the exits are done.
            pytest.param(200, RuntimeError("commit rejected"), None, StageExecutionError, False, id="error-on-exit"),
        ],
    )
    def test_an_iteration_that_does_not_succeed_cancels_the_pool_itself(self, status, exit_error, end, raised, cancelled_on_exit):
        """Before it ends, and so before the stage's thread learns of it, and,
        when it failed on its own, before its exits: the rest of the pool must
        not go on sending while they run. It claims the pool with its index,
        which makes its outcome the stage's however late it is read."""
        cancel = carrier_module._PoolCancel()
        cancel_on_exit: list[bool] = []

        @contextmanager
        def transaction():
            yield "t"
            cancel_on_exit.append(cancel.is_set())
            if exit_error is not None:
                raise exit_error

        def b():
            if end is KeyboardInterrupt:
                raise KeyboardInterrupt
            if end is not None:
                end("given up")
            return True

        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status)))
        cls = _make_carrier_subclass(client=client)
        stage = Stage.model_validate(
            {"name": "s", "request": {"url": "http://mock/{{ a() }}"}, "response": [{"verify": {"status": 200}}, {"verify": {"expressions": ["{{ b() }}"]}}]}
        )
        local_context = ChainMap(cls._build_stage_fixtures({"a": transaction, "b": b}))
        try:
            if raised is None:
                cls._run_iteration(stage, local_context, {}, None, 60, cancel, 3)
            else:
                with pytest.raises(raised):
                    cls._run_iteration(stage, local_context, {}, None, 60, cancel, 3)
        finally:
            client.close()
        assert cancel_on_exit == [cancelled_on_exit]
        assert cancel.is_set() is (raised is not None)
        # Another iteration ending now is not the first to have.
        assert cancel.claim(4) is (raised is None)

    def test_a_failing_iteration_cancels_the_others_before_its_exits(self):
        """Its worker cancels the pool as soon as it fails, and the iterations
        cancelled are not the failure reported, though read first. The stage's
        thread cancelled it only once the failure was read, which is when the
        exits are done: while a slow one ran (a rollback), the queued
        iterations went on sending."""
        events: list[str] = []
        sent: list[str] = []
        ended: list[int] = []
        both_sent = threading.Barrier(2)

        def answer(request):
            sent.append(request.url.path)
            both_sent.wait(timeout=5)
            if request.url.path == "/0":
                return httpx.Response(500)
            # Held until iteration 0 has failed and begun its exits: only then
            # does this worker take the queued iterations.
            _wait_until(lambda: "exit 0" in events)
            return httpx.Response(200)

        @contextmanager
        def rollback(i):
            yield i
            events.append(f"exit {i}")
            if i == 0:
                # Slow: until every other iteration has ended, and then long
                # enough for the stage's thread to read their ends first.
                _wait_until(lambda: len(ended) == 9)
                time.sleep(0.1)

        def run_iteration(klass, stage, local_context, iter_vars, *args):
            try:
                return Carrier._run_iteration.__func__(klass, stage, local_context, iter_vars, *args)
            finally:
                if iter_vars["i"] != 0:
                    ended.append(iter_vars["i"])

        cls = _make_carrier_subclass(client=httpx.Client(transport=httpx.MockTransport(answer)), _run_iteration=classmethod(run_iteration))
        stage = Stage.model_validate(
            {
                "name": "s",
                "parallel": {"foreach": [{"individual": {"i": list(range(10))}}], "max_concurrency": 2},
                "request": {"url": "http://mock/{{ i }}?t={{ rollback(i) }}"},
                "response": [{"verify": {"status": 200}}],
            }
        )
        try:
            with pytest.raises(pytest.fail.Exception) as excinfo:
                cls.execute_stage(stage, {"rollback": rollback})
        finally:
            cls.client.close()
        assert str(excinfo.value) == _ITERATION_0_FAILED
        assert sorted(sent) == ["/0", "/1"]

    def test_error_on_exit_of_a_cancelled_iteration_is_reported(self):
        """One cancelled once its request was rendered has entered what the
        render called, and exits it: what that exit raises is listed like a
        straggler's. The cancellation itself is no failure to report, even
        with an exit error, though read before the failure that caused it."""
        events: list[str] = []
        sent: list[str] = []

        def answer(request):
            sent.append(request.url.path)
            # Iteration 1 is rendering: it got past the cancellation check
            # before its request is rendered.
            _wait_until(lambda: "render 1" in events)
            return httpx.Response(500)

        @contextmanager
        def transaction(i):
            events.append(f"enter {i}")
            yield i
            events.append(f"exit {i}")
            if i == 0:
                # Long enough for the stage's thread to read iteration 1 first.
                _wait_until(lambda: "exit 1" in events)
                time.sleep(0.1)
            else:
                raise RuntimeError(f"commit of {i} rejected")

        def a(i):
            if i == 1:
                events.append("render 1")
                # Until iteration 0 has failed, cancelling the pool, and begun its exits.
                _wait_until(lambda: "exit 0" in events)
            return transaction(i)

        cls = _make_carrier_subclass(client=httpx.Client(transport=httpx.MockTransport(answer)))
        stage = Stage.model_validate(
            {
                "name": "s",
                "parallel": {"foreach": [{"individual": {"i": [0, 1]}}], "max_concurrency": 2},
                "request": {"url": "http://mock/{{ i }}?t={{ a(i) }}"},
                "response": [{"verify": {"status": 200}}],
            }
        )
        try:
            with pytest.raises(pytest.fail.Exception) as excinfo:
                cls.execute_stage(stage, {"a": a})
        finally:
            cls.client.close()
        assert str(excinfo.value) == f"{_ITERATION_0_FAILED}\n{_ITERATION_1_EXIT_ERROR}"
        assert sent == ["/0"]
        assert events[-2:] == ["enter 1", "exit 1"]

    @pytest.mark.parametrize(
        ("status", "message"),
        [
            pytest.param(
                500,
                [_ITERATION_0_FAILED, "Iteration 0: Exiting the context manager from fixture 'b' failed: RuntimeError: commit of b0 rejected"],
                id="iteration-failed",
            ),
            # Its exits are all it has to report, and the first one opens the message.
            pytest.param(
                200,
                [
                    "Parallel execution failed at iteration 0: Exiting the context manager from fixture 'b' failed: RuntimeError: commit of b0 rejected",
                ],
                id="iteration-passed",
            ),
        ],
    )
    def test_the_failing_iterations_exit_errors_are_labelled(self, status, message):
        """Like a straggler's, with the iteration, so they read apart from the
        stage's own, from what its substitutions entered, which are not."""
        events: list[str] = []

        def answer(request):
            return httpx.Response(status if request.url.path.startswith("/0/") else 200)

        def per_iteration(tag):
            return lambda i: self._resource(events, f"{tag}{i}", RuntimeError(f"commit of {tag}{i} rejected"))()

        cls = _make_carrier_subclass(client=httpx.Client(transport=httpx.MockTransport(answer)))
        stage = Stage.model_validate(
            {
                "name": "s",
                "substitutions": [{"vars": {"t": "{{ s() }}"}}],
                # One at a time: iteration 0 fails the stage, and 1 is cancelled before it starts.
                "parallel": {"foreach": [{"individual": {"i": [0, 1]}}], "max_concurrency": 1},
                "request": {"url": "http://mock/{{ i }}/{{ t }}/{{ a(i) }}/{{ b(i) }}"},
                "response": [{"verify": {"status": 200}}],
            }
        )
        fixtures = {"s": self._resource(events, "s", RuntimeError("commit of s rejected")), "a": per_iteration("a"), "b": per_iteration("b")}
        try:
            with pytest.raises(pytest.fail.Exception) as excinfo:
                cls.execute_stage(stage, fixtures)
        finally:
            cls.client.close()
        assert str(excinfo.value).split("\n") == [
            *message,
            "Iteration 0: Exiting the context manager from fixture 'a' failed: RuntimeError: commit of a0 rejected",
            "Exiting the context manager from fixture 's' failed: RuntimeError: commit of s rejected",
        ]
        assert events == ["enter s", "enter a0", "enter b0", "exit b0", "exit a0", "exit s"]

    @pytest.mark.parametrize(
        "by_the_stages_exit",
        [
            pytest.param(False, id="interrupted-by-an-iteration"),
            # What the stage entered outside its iterations calls pytest.exit()
            # on exit. That exit logged its own errors alone, and the
            # straggler's, collected before, were dropped.
            pytest.param(True, id="interrupted-by-the-stages-exit"),
        ],
    )
    @_EITHER_READ_ORDER
    def test_error_on_exit_of_a_straggler_is_logged_on_an_interrupt(self, caplog, by_the_stages_exit, straggler_read_first):
        """No stage failure will carry it then."""
        events: list[str] = []

        def interrupt(reason):
            raise KeyboardInterrupt(reason)

        with pytest.raises(pytest.exit.Exception if by_the_stages_exit else KeyboardInterrupt):
            self._run_with_a_straggler(
                _make_carrier_subclass(),
                events,
                status=500 if by_the_stages_exit else 200,
                end=None if by_the_stages_exit else interrupt,
                straggler_read_first=straggler_read_first,
                stage_exit_error=pytest.exit.Exception("rollback of s failed") if by_the_stages_exit else None,
            )
        assert [event for event in events if event.startswith("exit")] == ["exit 0", "exit 1", *(["exit s"] if by_the_stages_exit else [])]
        assert _ITERATION_1_EXIT_ERROR in caplog.text

    @pytest.mark.parametrize("interrupt", [pytest.exit.Exception, KeyboardInterrupt], ids=["pytest-exit", "keyboard-interrupt"])
    def test_an_interrupt_on_exit_of_a_straggler_stops_the_run(self, interrupt):
        """It is raised once the other stragglers are taken in, a success for
        the HAR, an exit error for the report. pytest.exit()'s is an Exception,
        which was taken for a failure of the straggler's own and dropped: the
        run went on. A KeyboardInterrupt left the stragglers after it untaken."""
        result = IterationResult(saved_context={}, request=httpx.Request("GET", "http://mock/0"), response=httpx.Response(200), started=datetime.now(UTC))
        interrupted: Future[IterationResult] = Future()
        interrupted.set_exception(interrupt("rollback of 1 failed"))
        passed: Future[IterationResult] = Future()
        passed.set_result(result)
        failed_on_exit: Future[IterationResult] = Future()
        failed_on_exit.set_exception(carrier_module._IterationExitError(["Exiting the context manager from fixture 'a' failed: RuntimeError: commit of 2 rejected"]))
        interrupted_too: Future[IterationResult] = Future()
        interrupted_too.set_exception(interrupt("rollback of 3 failed"))
        results: list[IterationResult | None] = [None, None, None, None]
        exit_errors: list[str] = []
        with pytest.raises(interrupt, match="^rollback of 1 failed$"):
            Carrier._fold_in_unread({interrupted: 1, passed: 0, failed_on_exit: 2, interrupted_too: 3}, set(), results, exit_errors)
        assert results == [result, None, None, None]
        assert exit_errors == ["Iteration 2: Exiting the context manager from fixture 'a' failed: RuntimeError: commit of 2 rejected"]

    @pytest.mark.parametrize(
        ("exit_error", "outcome", "message"),
        [
            pytest.param(None, pytest.skip.Exception, "Flow aborted", id="skipped"),
            # A skipped stage has not failed, so the error on exit fails it.
            pytest.param(
                RuntimeError("rollback failed"),
                pytest.fail.Exception,
                "Exiting the context manager from fixture 'a' failed: RuntimeError: rollback failed",
                id="error-on-exit",
            ),
        ],
    )
    def test_exited_when_the_stage_skips(self, exit_error, outcome, message):
        """An ``always_run`` template can call a factory fixture and still skip the stage."""
        events: list[str] = []
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        cls = _make_carrier_subclass(client=client, aborted=True)
        stage = Stage.model_validate({"name": "s", "always_run": "{{ a() == 'never' }}", "request": {"url": "http://mock/"}})
        try:
            # Both caught, so a wrong outcome fails this test rather than skipping it.
            with pytest.raises((pytest.skip.Exception, pytest.fail.Exception)) as excinfo:
                cls.execute_stage(stage, {"a": self._resource(events, "a", exit_error)})
        finally:
            client.close()
        assert excinfo.type is outcome
        assert str(excinfo.value) == message
        assert events == ["enter a", "exit a"]

    @pytest.mark.parametrize(
        "stage_fields",
        [
            pytest.param({}, id="in-the-request"),
            pytest.param({"substitutions": [{"vars": {"t": "{{ a() }}/{{ b() }}"}}], "request": {"url": "http://mock/{{ t }}"}}, id="in-substitutions"),
        ],
    )
    @pytest.mark.parametrize(
        ("end", "exit_error", "outcome", "message"),
        [
            pytest.param(pytest.skip, None, pytest.skip.Exception, "given up", id="skip"),
            pytest.param(pytest.xfail, None, pytest.xfail.Exception, "given up", id="xfail"),
            # An xfail is no failure either, so the error on exit fails the
            # stage. It was only logged, which pytest does not show for an xfail.
            pytest.param(
                pytest.xfail,
                RuntimeError("rollback failed"),
                pytest.fail.Exception,
                "Exiting the context manager from fixture 'a' failed: RuntimeError: rollback failed",
                id="xfail-error-on-exit",
            ),
            pytest.param(
                pytest.fail,
                RuntimeError("rollback failed"),
                pytest.fail.Exception,
                "given up\nExiting the context manager from fixture 'a' failed: RuntimeError: rollback failed",
                id="fail-error-on-exit",
            ),
        ],
    )
    def test_a_user_function_ending_the_stage(self, stage_fields, end, exit_error, outcome, message):
        """``b`` ends the stage with ``pytest.skip/xfail/fail()``, which passes
        through unchanged unless ``a`` raises on exit."""
        events: list[str] = []

        def b():
            end("given up")

        with pytest.raises((pytest.skip.Exception, pytest.fail.Exception)) as excinfo:
            self._run({"a": self._resource(events, "a", exit_error), "b": b}, **stage_fields)
        assert excinfo.type is outcome
        assert str(excinfo.value) == message
        assert events == ["enter a", "exit a"]

    def test_exited_when_the_stage_is_interrupted(self, caplog):
        """An interrupt or a plugin bug propagates as it is; an error on exit
        can then only be logged."""
        events: list[str] = []

        def b():
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            self._run({"a": self._resource(events, "a", RuntimeError("rollback failed")), "b": b})
        assert events == ["enter a", "exit a"]
        assert "Exiting the context manager from fixture 'a' failed: RuntimeError: rollback failed" in caplog.text

    def test_an_interrupt_on_exit_is_raised_once_all_are_exited(self, caplog):
        """``a``, entered first, is still exited; its error, which no stage
        failure will carry now, is logged."""
        events: list[str] = []
        with pytest.raises(KeyboardInterrupt):
            self._run({"a": self._resource(events, "a", RuntimeError("rollback failed")), "b": self._resource(events, "b", KeyboardInterrupt())})
        assert events == ["enter a", "enter b", "exit b", "exit a"]
        assert "Exiting the context manager from fixture 'a' failed: RuntimeError: rollback failed" in caplog.text

    @pytest.mark.parametrize("in_the_iterations_context", [False, True], ids=["empty-context", "iteration-context"])
    def test_one_entered_after_its_stage_ended_is_exited_with_the_chain(self, caplog, in_the_iterations_context):
        """By a thread a user function started, which outlived the stage. It
        goes on the stage's list, whose leftovers the chain's end exits, and
        what that exit raises is logged, as no stage is left to fail for it.
        The chain's end only reset the list, and the context manager was never
        exited. From a thread running in a copy of the iteration's context
        (``asyncio.to_thread``, or any thread on a free-threaded build), it
        went on the iteration's list instead, already exited, and was lost."""
        events: list[str] = []
        release = threading.Event()
        threads: list[threading.Thread] = []

        def later(factory):
            def enter():
                release.wait(timeout=5)
                factory()

            thread = threading.Thread(target=contextvars.copy_context().run, args=(enter,)) if in_the_iterations_context else threading.Thread(target=enter)
            thread.start()
            threads.append(thread)
            return "later"

        cls = self._run({"later": later, "t": self._resource(events, "t", RuntimeError("rollback failed"))}, request={"url": "http://mock/{{ later(t) }}"})
        release.set()
        for thread in threads:
            thread.join(timeout=5)
        assert events == ["enter t"]
        cls.teardown_class()
        assert events == ["enter t", "exit t"]
        assert "Exiting the context manager from fixture 't' failed: RuntimeError: rollback failed" in caplog.text


class _Poison:
    def __str__(self):
        raise RuntimeError("boom")


def _circular() -> dict:
    circular: dict = {}
    circular["self"] = circular
    return circular


def test_context_dump_serializes_plain_context():
    assert '"a": 1' in _context_dump({"a": 1})


@pytest.mark.parametrize(
    "context",
    [
        pytest.param(_circular(), id="circular"),
        pytest.param({"a": {(1, 2): 3}}, id="tuple-key"),
        pytest.param({"a": _Poison()}, id="poison-str"),
    ],
)
def test_context_dump_never_raises(context):
    """Dumps feed DEBUG logging only; whatever a user-function save put into
    the context, they must degrade rather than break the stage."""
    assert "unserializable" in _context_dump(context)


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
            cls._run_iterations(None, ChainMap(), [{} for _ in range(40)], config, [])

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
    pristine base context, so a rerun plugin's second pass actually re-executes
    the chain instead of replaying stale saves or skipping. A scenario's next
    chain resets the same way, short of its initialization."""

    @pytest.fixture
    def executed(self, monkeypatch):
        """A one-stage scenario class after its first pass: it saved `v`."""
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"v": 1}))
        monkeypatch.setattr("pytest_httpchain.carrier.build_client_kwargs", lambda *args, **kwargs: {"transport": transport})
        scenario = Scenario.model_validate(
            {
                "substitutions": [{"vars": {"base": 1}}],
                "stages": [{"name": "s", "request": {"url": "http://mock/ok"}, "response": [{"save": {"jmespath": {"v": "v"}}}]}],
            }
        )
        cls = _make_carrier_subclass(scenario=scenario, _initialized=False)
        cls.execute_stage(scenario.stages[0], {})
        assert cls.global_context["v"] == 1
        yield cls, scenario.stages[0]
        cls.teardown_class()  # closes whichever client the test left open

    def test_teardown_restores_fresh_state(self, executed):
        cls, _ = executed
        client = cls.client
        cls.teardown_class()
        assert client.is_closed
        assert {name: getattr(cls, name) for name in fresh_scenario_state()} == fresh_scenario_state()
        # Saves are gone; the pristine scenario context survives.
        assert dict(cls.global_context) == {"base": 1}

    def test_changing_chain_resets_and_staying_in_it_does_not(self, executed):
        """A parametrized fixture splits a scenario into chains that share one
        class, so class teardown alone would hand the next chain the previous
        one's saves, abort flag and client. Entering a different chain resets
        them; re-entering the current one keeps its state."""
        cls, _ = executed
        client = cls.client
        cls.aborted = True
        cls.begin_chain((("tenant", 0),))  # the chain the first pass ran in
        cls.begin_chain((("tenant", 0),))
        assert (cls.aborted, cls.global_context["v"], client.is_closed) == (True, 1, False)

        cls.begin_chain((("tenant", 1),))
        assert client.is_closed
        assert {name: getattr(cls, name) for name in fresh_chain_state()} == fresh_chain_state()
        assert dict(cls.global_context) == {"base": 1}

    def test_next_chain_keeps_the_scenario_initialization(self, executed, monkeypatch):
        """Entering a chain does not repeat the scenario's initialization: it
        cannot see the param, and its user functions (substitutions, auth) run
        at most once per scenario. The chain gets a client of its own, built
        from the arguments the first chain resolved."""
        cls, stage = executed
        first_client = cls.client
        # Only the first chain's resolution holds this; a second one would not.
        cls.global_context.maps[-1]["resolved_once"] = True

        def re_resolved(*args, **kwargs):
            raise AssertionError("scenario auth and ssl resolved again")

        monkeypatch.setattr("pytest_httpchain.carrier.build_client_kwargs", re_resolved)
        cls.begin_chain((("tenant", 0),))
        cls.begin_chain((("tenant", 1),))
        cls.execute_stage(stage, {})
        assert (cls.global_context["resolved_once"], cls.global_context["v"]) == (True, 1)
        assert cls.client is not first_client
        assert not cls.client.is_closed

    def test_second_pass_reinitializes_and_replays(self, executed):
        cls, stage = executed
        first_client = cls.client
        cls.teardown_class()
        cls.execute_stage(stage, {})
        assert cls.client is not first_client
        assert cls.global_context["v"] == 1
