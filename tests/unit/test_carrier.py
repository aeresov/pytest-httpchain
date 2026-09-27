"""carrier.py: chain state, the iteration matrix, threading and reporting.

What a single request or response step means lives in request_builder and
response_steps (test_request_builder.py, test_response_steps.py); the HTTP
round trip itself is the integration suite's.
"""

import re
import ssl
import threading
import time
from collections import ChainMap
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
            _make_carrier_subclass()._run_iterations(make_stage(), ChainMap(), [{}, {}], ParallelRepeatConfig.model_construct(**settings))


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
