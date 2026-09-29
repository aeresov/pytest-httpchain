"""carrier.py: chain state, the iteration matrix, threading and reporting.

What a single request or response step means lives in request_builder and
response_steps (test_request_builder.py, test_response_steps.py); the HTTP
round trip itself is the integration suite's.
"""

import contextvars
import json
import math
import re
import ssl
import threading
import time
from collections import ChainMap
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import trustme
from pydantic import ValidationError
from pydantic_core import PydanticCustomError
from pyrate_limiter import Duration, Limiter, Rate

import pytest_httpchain.carrier as carrier_module
import pytest_httpchain.templates.substitution as substitution_module
from pytest_httpchain.carrier import (
    Carrier,
    IterationResult,
    _context_dump,
    _merged_saves,
    _parallel_int,
    _parallel_number,
    _render_declared,
    fresh_chain_state,
    fresh_scenario_state,
)
from pytest_httpchain.errors import RequestError, SaveError, StageExecutionError, VerificationError
from pytest_httpchain.models import (
    AuthCredentials,
    BearerAuth,
    ClientConfig,
    CombinationsParameter,
    DigestAuth,
    FilesBody,
    FileSpec,
    IndividualParameter,
    ParallelForeachConfig,
    ParallelRepeatConfig,
    RegexSave,
    Request,
    ResponseBody,
    RetryConfig,
    Scenario,
    SSLConfig,
    Stage,
    UserFunctionKwargs,
    UserFunctionName,
    VarsSubstitution,
    Verify,
)
from pytest_httpchain.redaction import DEFAULT_REDACTION, NO_REDACTION
from pytest_httpchain.request_builder import build_client_kwargs
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


class TestClientWiring:
    """The scenario's ``client`` block -> the shared client, and the requests
    sent on it."""

    def test_templates_resolve_against_scenario_substitutions(self, monkeypatch):
        """Once per scenario, like ``ssl`` and ``auth``; the resolved block is
        kept for what each request applies itself (params, the base_url check)."""
        captured: dict = {}
        monkeypatch.setattr("pytest_httpchain.carrier.httpx.Client", lambda **kwargs: captured.update(kwargs))
        scenario = Scenario.model_validate(
            {
                "substitutions": [{"vars": {"api": "https://api.test/v1", "conns": 50, "key": "k"}}],
                "client": {"base_url": "{{ api }}", "max_connections": "{{ conns }}", "params": {"api_key": "{{ key }}"}},
            }
        )
        cls = _make_carrier_subclass(scenario=scenario, _initialized=False)
        cls._ensure_initialized()
        assert (captured["base_url"], captured["limits"].max_connections) == ("https://api.test/v1", 50)
        assert cls._client_config.params == {"api_key": "k"}

    def test_initialization_failure_does_not_quote_the_proxy(self):
        """A proxy's credentials usually come from the environment. One that
        renders malformed fails initialization without them, in the message
        every later stage repeats as its skip reason too (the model's own
        messages are pinned in tests/unit/models/test_client_config.py)."""
        scenario = Scenario.model_validate(
            {"substitutions": [{"vars": {"proxy": "http://user:pa/s3cret@proxy.internal:3128"}}], "client": {"proxy": "{{ proxy }}"}},
        )
        cls = _make_carrier_subclass(scenario=scenario, _initialized=False)
        for _ in range(2):
            with pytest.raises(StageExecutionError, match=r"(?s)^Failed to initialize scenario: .*\nproxy\..*Invalid URL") as excinfo:
                cls._ensure_initialized()
            assert "s3cret" not in str(excinfo.value)
            assert "'pa'" not in str(excinfo.value)

    def test_rendered_request_keeps_its_declared_fields(self):
        """Rendering dumped every field, defaults included, so the rendered
        request declared a timeout and a redirect setting it never had, and
        they overrode the client's."""
        rendered = _render_declared(Request.model_validate({"url": "{{ u }}", "headers": {"A": "{{ a }}"}}), {"u": "http://t/", "a": "1"}, "request")
        assert rendered.model_fields_set == {"url", "headers"}

    @pytest.mark.parametrize(("declared", "timeout"), [({}, 5), ({"timeout": "{{ t }}"}, 60)], ids=["client-default", "stage-declared"])
    def test_templated_request_takes_the_clients_defaults(self, declared, timeout):
        client_config = ClientConfig(base_url="http://mock/v1", timeout=5, follow_redirects=False)
        sent = []
        transport = httpx.MockTransport(lambda request: sent.append(request) or httpx.Response(302, headers={"Location": "/elsewhere"}))
        client = httpx.Client(**build_client_kwargs(client_config, SSLConfig(), None, None), transport=transport)
        cls = _make_carrier_subclass(client=client, _client_config=client_config)
        stage = Stage.model_validate({"name": "s", "request": {"url": "{{ path }}", **declared}})
        try:
            result = cls._execute_single_iteration(stage, ChainMap({"path": "/users/1", "t": 60}), {})
        finally:
            client.close()
        # Joined to the base URL, with the client's redirect setting: not followed.
        assert [str(request.url) for request in sent] == ["http://mock/v1/users/1"]
        assert result.response.status_code == 302
        assert sent[0].extensions["timeout"]["read"] == timeout


def test_exhausted_rate_limit_blocks_for_the_delay_then_fails():
    """The limiter really blocks, and times out into a stage failure (M2).
    Its window is a minute, so the slot taken cannot free up while the test
    runs, however slowly: a one-second window did after a one-second stall."""
    limiter = Limiter(Rate(1, Duration.MINUTE))
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

    def test_rendered_away_regex_group_says_what_it_would_have_saved(self):
        """A regex save's group left out does not disable the save: it saves
        the default group (group 1, or the whole match), so the refusal says
        that, not that the None would have disabled anything."""
        with pytest.raises(StageExecutionError) as excinfo:
            _render_declared(RegexSave.model_validate({"regex": {"v": {"pattern": "(a)(b)", "group": "{{ x }}"}}}), self.RENDERS_NONE, "save")
        assert str(excinfo.value) == "'save.regex.v.group' was declared as '{{ x }}' but rendered to None, which would silently save the default group instead"

    @pytest.mark.parametrize("field", ["filename", "content_type"])
    def test_rendered_away_file_name_or_type_says_what_it_would_have_sent(self, field):
        """A multipart file's filename or content type left out is sent as
        the default one (the path's name, the guessed type), so the refusal
        says that."""
        declared = Request.model_validate({"url": "http://t/", "body": {"multipart": {"files": {"f": {"content": "c", field: "{{ x }}"}}}}})
        with pytest.raises(StageExecutionError) as excinfo:
            _render_declared(declared, self.RENDERS_NONE, "request")
        declared_as = f"'request.body.multipart.files.f.{field}' was declared as " + "'{{ x }}'"
        assert str(excinfo.value) == f"{declared_as} but rendered to None, which would silently send the default {field.replace('_', ' ')} instead"

    @pytest.mark.parametrize(
        "specs",
        [
            pytest.param([{"content": "a"}, {"content": "b", "filename": None}], id="list-of-dicts"),
            # `vars` objects in a tuple: the tuple is the list, each namespace
            # the file object it stands for.
            pytest.param((SimpleNamespace(content="a"), SimpleNamespace(content="b", filename=None)), id="tuple-of-namespaces"),
        ],
    )
    def test_file_objects_in_a_list_rendered_whole_are_refused_too(self, specs):
        """A list of file objects one template renders (``"f": "{{ specs }}"``)
        is a list of models known only once validated: each is checked as a
        model rendered whole, a key it set to None refused."""
        context = ChainMap({"specs": specs})
        declared = FilesBody.model_validate({"files": {"f": "{{ specs }}"}})
        with pytest.raises(StageExecutionError) as excinfo:
            _render_declared(declared, context, "request.body")
        assert str(excinfo.value) == "'request.body.files.f[1].filename' was declared as '{{ specs }}' but rendered to None, which would silently send the default filename instead"

    def test_other_list_rendered_whole_is_not_walked(self, monkeypatch):
        """Only a list of files holds models, one level deep: any other list
        a template renders, a JSON body of a million values say, holds none,
        and walking each of its items, and each list in them, doubled the time
        the body took to render."""
        calls = 0
        walk_whole = carrier_module._rendered_whole_away

        def counted(*args):
            nonlocal calls
            calls += 1
            return walk_whole(*args)

        monkeypatch.setattr(carrier_module, "_rendered_whole_away", counted)
        declared = Request.model_validate({"url": "http://t/", "method": "POST", "body": {"json": "{{ big }}"}})
        rendered = _render_declared(declared, ChainMap({"big": [[i, i + 1] for i in range(1000)]}), "request")
        assert rendered.body.json[999] == [999, 1000]
        # The model's fields and the body's, but none of the list's items.
        assert calls < 50

    @pytest.mark.parametrize(
        ("spec", "sent"),
        [
            pytest.param({"path": "a.txt", "content": None, "base64": None}, FileSpec(path=Path("a.txt")), id="dict"),
            pytest.param(SimpleNamespace(content="a", path=None), FileSpec(content="a"), id="namespace"),
            pytest.param([{"content": "a", "path": None}], [FileSpec(content="a")], id="in-a-list"),
        ],
    )
    def test_file_object_rendered_whole_with_null_sources_is_sent(self, spec, sent):
        """A file object counts its sources by value: a None in the ones it
        does not use (a user function, a saved object filling every key) is
        not set, as in the same object written out, and disables nothing."""
        declared = FilesBody.model_validate({"files": {"f": "{{ spec }}"}})
        assert _render_declared(declared, ChainMap({"spec": spec}), "request.body").files["f"] == sent

    @pytest.mark.parametrize(
        ("where", "declared", "path"),
        [
            # A multipart file's only source: "sets exactly one of: path,
            # content, base64", asking for what the scenario did set.
            pytest.param(
                "request",
                Request.model_validate({"url": "http://t/", "body": {"multipart": {"files": {"f": {"path": "{{ x }}"}}}}}),
                "request.body.multipart.files.f.path",
                id="multipart-file-path",
            ),
            # A matcher's only field: pydantic's report asked the scenario to
            # "set at least one of: contains, ..." — the field it did set.
            pytest.param("verify", Verify.model_validate({"headers": {"Location": {"contains": "{{ x }}"}}}), "verify.headers.Location.contains", id="single-field-matcher"),
            # A required field, which pydantic reported as "URL input should be
            # a string or URL", naming neither the template nor the None.
            pytest.param("request", Request.model_validate({"url": "{{ x }}"}), "request.url", id="required-field"),
            # A user-function name is a RootModel, dumped as its bare root value.
            pytest.param("auth", UserFunctionName("{{ x }}"), "auth", id="scenario-auth"),
            # A built-in's credential: not "no auth", which the stage would
            # have sent unauthenticated or with the scenario's credentials.
            pytest.param("request", Request.model_validate({"url": "http://t/", "auth": {"bearer": "{{ x }}"}}), "request.auth.bearer", id="request-auth-bearer"),
            pytest.param(
                "request",
                Request.model_validate({"url": "http://t/", "auth": {"basic": {"username": "u", "password": "{{ x }}"}}}),
                "request.auth.basic.password",
                id="request-auth-basic-password",
            ),
            pytest.param("auth", DigestAuth.model_validate({"digest": {"username": "{{ x }}", "password": "p"}}), "auth.digest.username", id="scenario-auth-digest-username"),
            pytest.param("verify", Verify.model_validate({"user_functions": ["{{ x }}"]}), "verify.user_functions[0]", id="function-name-in-a-list"),
            pytest.param("save", RegexSave.model_validate({"regex": {"v": {"pattern": "{{ x }}"}}}), "save.regex.v.pattern", id="regex-save-pattern"),
            pytest.param("save", RegexSave.model_validate({"regex": {"v": {"pattern": "(a)", "all": "{{ x }}"}}}), "save.regex.v.all", id="regex-save-all"),
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

    @pytest.mark.parametrize("source", ["saved", "vars"])
    def test_regex_capture_rendered_whole_is_refused_too(self, source):
        """A ``save.regex`` entry written as one template renders a capture
        object, as a header matcher can be: a key it set to None is refused,
        from a saved object or from a ``vars`` one (a namespace), saying what
        the None would have done, as for a group declared as a template."""
        capture = {"pattern": "(a)(b)", "group": None}
        context = ChainMap(VarsSubstitution(vars={"capture": capture}).vars) if source == "vars" else {"capture": capture}
        with pytest.raises(StageExecutionError) as excinfo:
            _render_declared(RegexSave.model_validate({"regex": {"v": "{{ capture }}"}}), context, "save")
        assert str(excinfo.value) == "'save.regex.v.group' was declared as '{{ capture }}' but rendered to None, which would silently save the default group instead"

    @pytest.mark.parametrize(
        ("declared", "context", "path", "invalid"),
        [
            # None is a valid auth: before, the refusal alone was reported, and
            # the url error was only in its __cause__.
            pytest.param(Request.model_validate({"url": "{{ u }}", "auth": "{{ x }}"}), {"u": "ftp://t/", "x": None}, "request.auth", "url", id="beside-a-none-that-validates"),
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
                Request.model_validate({"url": "{{ u }}", "timeout": "{{ x }}"}), {"u": "ftp://t/", "x": None}, "request.timeout", "url", id="beside-a-none-that-is-rejected"
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
        ("save", "context", "message"),
        [
            pytest.param({"regex": {"v": "{{ p }}"}}, {"p": "("}, "Invalid regular expression", id="regex-pattern"),
            pytest.param({"regex": {"v": {"pattern": "{{ p }}", "group": 2}}}, {"p": "(a)"}, r"regex '\(a\)' has no group 2", id="regex-group"),
            pytest.param({"regex": {"v": {"pattern": "(a)", "group": "{{ g }}"}}}, {"g": "b"}, "has no group named 'b'", id="regex-group-name"),
            # The same for every save kind: it failed as a bare stage error.
            pytest.param({"jmespath": {"v": "{{ p }}"}}, {"p": "[bad"}, "Invalid JMESPath expression", id="jmespath-expression"),
        ],
    )
    def test_rendered_save_that_does_not_validate_is_a_save_error(self, save, context, message):
        """A value a save step's template rendered that validation refuses is
        the step's SaveError, as a verify step's is its VerificationError,
        with pydantic's report on it as the message."""
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        cls = _make_carrier_subclass(client=client)
        try:
            with pytest.raises(StageExecutionError) as excinfo:
                cls._execute_single_iteration(Stage.model_validate({"name": "s", "request": {"url": "http://mock/ok"}, "response": [{"save": save}]}), ChainMap(context), {})
        finally:
            client.close()
        assert type(excinfo.value) is SaveError
        assert re.search(message, str(excinfo.value))
        assert isinstance(excinfo.value.__cause__, ValidationError)
        # Carried for the report and the HAR entry, as every step's failure is.
        assert excinfo.value.response is not None

    @pytest.mark.parametrize(
        ("pattern", "saved"),
        [
            # Escaped, the braces are regex: the page's own placeholder.
            pytest.param(r"\{\{\s*(\w+)\s*\}\}", "name", id="escaped-braces"),
            # A repeat count from a template, its braces inside the expression.
            pytest.param(r"\d{{ '{' + str(n) + '}' }}", "123", id="templated-count"),
            # Spaced out of the template, the braces are the text `{ 3 }`.
            pytest.param(r"\d{ {{ n }} }", "4{ 3 }", id="spaced-count-is-text"),
        ],
    )
    def test_braces_in_a_regex_pattern(self, pattern, saved):
        """A `{{` in a pattern always opens a template (responses.md, Regex
        Extraction): the forms the docs give for literal braces and for a
        templated repeat count save what they say."""
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text="<p>Hi {{ name }}: 12345, 4{ 3 }</p>")))
        cls = _make_carrier_subclass(client=client)
        try:
            result = cls._execute_single_iteration(
                Stage.model_validate({"name": "s", "request": {"url": "http://mock/ok"}, "response": [{"save": {"regex": {"v": pattern}}}]}), ChainMap({"n": 3}), {}
            )
        finally:
            client.close()
        assert result.saved_context == {"v": saved}

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
        ("value", "expected"),
        [
            pytest.param(False, False, id="false"),
            pytest.param({"bearer": "tok"}, BearerAuth(bearer="tok"), id="builtin"),
            pytest.param("mod:fn", UserFunctionName("mod:fn"), id="function-name"),
        ],
    )
    @pytest.mark.parametrize("source", ["saved", "vars"])
    def test_auth_rendered_whole_takes_any_form(self, value, expected, source):
        """A request's auth written as one template is whatever it renders to,
        ``false`` included; only None is refused (above). From ``vars``, an
        object is a namespace, which stands for the object it was written as:
        refused, the stage failed where ``validate`` had passed it."""
        context = ChainMap(VarsSubstitution(vars={"a": value}).vars) if source == "vars" else {"a": value}
        rendered = _render_declared(Request.model_validate({"url": "http://t/", "auth": "{{ a }}"}), context, "request")
        assert rendered.auth == expected

    @pytest.mark.parametrize(
        ("auth", "expected"),
        [
            # A whole call from a vars object: its kwargs as written inline,
            # dicts all the way down.
            pytest.param("{{ call }}", UserFunctionKwargs(name=UserFunctionName("mod:fn"), kwargs={"config": {"realm": "r"}}), id="call-whole"),
            # A kwarg a template rendered is handed on as it is, as any user
            # function's is.
            pytest.param(
                {"name": "mod:fn", "kwargs": {"config": "{{ config }}"}},
                UserFunctionKwargs(name=UserFunctionName("mod:fn"), kwargs={"config": SimpleNamespace(realm="r")}),
                id="kwarg-rendered",
            ),
            # Credentials from a vars object, put in place by the expression.
            pytest.param("{{ {'digest': login} }}", DigestAuth(digest=AuthCredentials(username="u", password="p")), id="credentials-rendered"),
        ],
    )
    def test_auth_from_vars_objects(self, auth, expected):
        context = ChainMap(
            VarsSubstitution(vars={"call": {"name": "mod:fn", "kwargs": {"config": {"realm": "r"}}}, "config": {"realm": "r"}, "login": {"username": "u", "password": "p"}}).vars
        )
        rendered = _render_declared(Request.model_validate({"url": "http://t/", "auth": auth}), context, "request")
        assert rendered.auth == expected

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
            pytest.param({"auth": {"bearer": "{{ x }}"}}, "'auth.bearer' was declared as '{{ x }}' but rendered to None", id="auth-bearer"),
            # Every stage's relative URL would have lost its base, or the
            # connection limit (null is "no limit") and the proxy gone quiet.
            *(
                pytest.param(
                    {"client": {field: "{{ x }}"}},
                    f"'client.{field}' was declared as " + "'{{ x }}' but rendered to None, which would silently disable it",
                    id=f"client-{field}",
                )
                for field in ("base_url", "proxy", "max_connections", "max_keepalive_connections")
            ),
        ],
    )
    def test_scenario_initialization_fails(self, declared, message):
        """The scenario-level render sites: ``ssl``, ``client`` and ``auth``
        resolve once, at initialization."""
        scenario = Scenario.model_validate({"substitutions": [{"vars": {"x": None}}], **declared})
        cls = _make_carrier_subclass(scenario=scenario, _initialized=False)
        with pytest.raises(StageExecutionError, match=f"^{re.escape(f'Failed to initialize scenario: {message}')}$"):
            cls._ensure_initialized()


class TestScenarioAuthRenderedWhole:
    """A scenario's auth written as one template is a user function's name
    until it renders, and it was validated again as one, on its own: a built-in
    scheme it rendered failed initialization, with pydantic's report quoting
    the credentials, where ``validate`` passed the file. It is validated
    against the union now, as a request's auth is within its request."""

    SUBSTITUTIONS = [
        {
            "vars": {
                "login": {"basic": {"username": "u", "password": "p"}},
                "token": "tok",
                "leaked": {"bearer": ["s3cret"]},
                "api_token": "s3cret-token",
                "creds": "json:s3cret_pass",
            }
        }
    ]

    @staticmethod
    def _authorization(cls: type[Carrier]) -> str:
        """The Authorization header the scenario's client auth sets."""
        assert cls.client is not None
        request = cls.client.build_request("GET", "http://t/")
        return next(cls.client.auth.sync_auth_flow(request)).headers["authorization"]

    @pytest.mark.parametrize(
        ("auth", "authorization"),
        [
            pytest.param("{{ login }}", "Basic dTpw", id="vars-object"),
            pytest.param("{{ {'bearer': token} }}", "Bearer tok", id="built-by-the-expression"),
        ],
    )
    def test_renders_a_builtin(self, auth, authorization):
        cls = _make_carrier_subclass(scenario=Scenario.model_validate({"substitutions": self.SUBSTITUTIONS, "auth": auth}), _initialized=False)
        cls._ensure_initialized()
        try:
            assert self._authorization(cls) == authorization
        finally:
            cls.teardown_class()

    @pytest.mark.parametrize(
        ("auth", "message"),
        [
            pytest.param("{{ leaked }}", "1 validation error for auth\nbearer.bearer\n  Input should be a valid string [type=string_type]", id="refused-credential"),
            # As when declared: the message says where false belongs.
            pytest.param("{{ False }}", "1 validation error for auth\n  Value error, false turns the scenario's auth off for one stage", id="false"),
            # A token for {"bearer": ...} written as the whole auth: a string,
            # so a user function's name, whose messages quoted it.
            pytest.param("{{ api_token }}", "1 validation error for auth\n  Value error, Not a user function's 'module:function' name (not shown", id="token-string"),
            # Basic credentials written as a string have a name's shape, and
            # fail to import as one: the module found, the function (the
            # password) not, which the failure named.
            pytest.param(
                "{{ creds }}",
                "auth's template rendered a user function name (not shown, as auth can carry a credential) that does not import: its module has no function of that name",
                id="credentials-string",
            ),
        ],
    )
    def test_refused_once_rendered_without_quoting_it(self, auth, message):
        cls = _make_carrier_subclass(scenario=Scenario.model_validate({"substitutions": self.SUBSTITUTIONS, "auth": auth}), _initialized=False)
        with pytest.raises(StageExecutionError) as excinfo:
            cls._ensure_initialized()
        assert str(excinfo.value).startswith(f"Failed to initialize scenario: {message}")
        assert "s3cret" not in str(excinfo.value)


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


class TestVerifyRenderedValueByValue:
    """A verify step's templates render one value at a time, all before the
    first check runs, and each value that fails is one failure in its check's
    place: rendered whole, the first that failed (an expression raising
    KeyError on a missing header, an operand rendered to None) was the stage's
    one failure, and hid every other, the status's too."""

    BODY = {"id": 1, "tags": ["a"]}

    @classmethod
    def _run(cls, verify: dict, context: dict) -> None:
        stage = Stage.model_validate({"name": "s", "request": {"url": "http://mock/ok"}, "response": [{"verify": verify}]})
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=cls.BODY)))
        carrier = _make_carrier_subclass(client=client)
        try:
            carrier._execute_single_iteration(stage, ChainMap(context), {})
        finally:
            client.close()

    @classmethod
    def _failure(cls, verify: dict, context: dict) -> StageExecutionError:
        with pytest.raises(StageExecutionError) as excinfo:
            cls._run(verify, context)
        assert type(excinfo.value) is VerificationError
        return excinfo.value

    def test_template_error_is_one_failure_among_the_others(self):
        verify = {
            "status": 201,
            "jmespath": {"id": 2},
            "expressions": ["{{ response.headers['x-missing'] == 'a' }}", "{{ 1 == 2 }}"],
            "body": {"contains": ["nope"]},
        }
        assert str(self._failure(verify, {})).split("\n") == [
            "5 verification checks failed:",
            "  1. Status code doesn't match: expected 201, got 200",
            "  2. JMESPath 'id' doesn't match: expected 2, got 1",
            "  3. KeyError in expression '{{ response.headers['x-missing'] == 'a' }}': 'x-missing'",
            "  4. Expression 1 failed: evaluated to False",
            "  5. Body doesn't contain 'nope'",
        ]

    @pytest.mark.parametrize(
        ("verify", "message"),
        [
            # An item keeps its index: validated alone, where its messages say it
            # sits is where the step has it.
            pytest.param(
                {"body": {"contains": ["a", "{{ gone }}"]}},
                "1 validation error for Verify\nbody.contains.1\n  Input should be a valid string [type=string_type, input_value=None, input_type=NoneType]",
                id="body-operand",
            ),
            pytest.param(
                {"user_functions": ["tests.unit.response_steps_test_helpers:returns_true", "{{ gone }}"]},
                "'verify.user_functions[1]' was declared as '{{ gone }}' but rendered to None",
                id="function-name",
            ),
            pytest.param(
                {"headers": {"Location": {"contains": "{{ gone }}", "not_contains": "e"}}},
                "'verify.headers.Location.contains' was declared as '{{ gone }}' but rendered to None, which would silently disable it",
                id="header-matcher-field",
            ),
            pytest.param(
                {"jmespath": {"id": {"gt": "{{ gone }}"}}},
                "'verify.jmespath.id.gt' was declared as '{{ gone }}' but rendered to None",
                id="jmespath-matcher-key",
            ),
            pytest.param(
                {"body": {"schema": "{{ gone }}"}},
                "'verify.body.schema' was declared as '{{ gone }}' but rendered to None, which would silently disable it",
                id="schema",
            ),
        ],
    )
    def test_value_rendered_to_none_is_one_failure_in_its_place(self, verify, message):
        """The rendered-away guard's refusal, or pydantic's report, as it read
        when it ended the step, now listed between the checks around it: the
        status first, the body's not_matches last."""
        verify = {"status": 201, **verify, "body": {**verify.get("body", {}), "not_matches": ["id"]}}
        first, status, failure, *rest = str(self._failure(verify, {"gone": None})).split("\n")
        assert (first, status) == ("3 verification checks failed:", "  1. Status code doesn't match: expected 201, got 200")
        rendered_away, *report = message.split("\n")
        assert failure == f"  2. {rendered_away}"
        assert rest[: len(report)] == [f"     {line}" for line in report]
        assert rest[-1] == "  3. Body matches 'id' while it shouldn't"

    def test_lone_template_error_reads_as_before(self):
        """Its message, as the stage's failure had it; a VerificationError now,
        caused by the template error."""
        error = self._failure({"expressions": ["{{ missing }}"]}, {})
        assert str(error) == "Undefined variable in expression '{{ missing }}': 'missing' is not defined for expression 'missing'"
        assert isinstance(error.__cause__, TemplatesError)

    @pytest.mark.parametrize(
        ("outcome", "listed"),
        [
            pytest.param(pytest.skip, [], id="skip"),
            pytest.param(pytest.xfail, [], id="xfail"),
            pytest.param(pytest.fail, ["The template at 'verify.expressions[0]' called pytest.fail(): no"], id="fail"),
        ],
    )
    def test_outcome_a_template_raises_cannot_override_an_earlier_failure(self, outcome, listed):
        """As a user function's: rendered after a check failed, a function the
        template calls must not turn the failed stage into a skipped one."""
        error = self._failure({"status": 201, "expressions": ["{{ end() }}"], "body": {"contains": ["nope"]}}, {"end": lambda: outcome("no")})
        status_failure = "Status code doesn't match: expected 201, got 200"
        expected = [status_failure] if not listed else ["2 verification checks failed:", f"  1. {status_failure}", f"  2. {listed[0]}"]
        assert str(error).split("\n") == expected

    def test_outcome_a_template_raises_ends_a_step_that_has_not_failed(self):
        stage = Stage.model_validate({"name": "s", "request": {"url": "http://mock/ok"}, "response": [{"verify": {"expressions": ["{{ end() }}"]}}]})
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        carrier = _make_carrier_subclass(client=client)
        try:
            with pytest.raises(pytest.skip.Exception, match="^not here$"):
                carrier._execute_single_iteration(stage, ChainMap({"end": lambda: pytest.skip("not here")}), {})
        finally:
            client.close()

    def test_template_fail_after_a_function_skip_fails_the_stage(self):
        """As when the step rendered whole before any check, and the fail() was
        raised before the function ran: the skip must not hide it."""
        verify = {"user_functions": ["tests.unit.response_steps_test_helpers:skips"], "body": {"contains": ["{{ end() }}"]}}
        # Both caught, so a wrong outcome fails this test rather than skipping it.
        with pytest.raises((pytest.skip.Exception, pytest.fail.Exception)) as excinfo:
            self._run(verify, {"end": lambda: pytest.fail("template failed")})
        assert (excinfo.type, str(excinfo.value)) == (pytest.fail.Exception, "template failed")

    def test_outcome_a_template_raises_ends_the_rendering_there(self):
        """Nothing after it is rendered, as nothing after it was when the step
        rendered whole."""
        calls = []
        verify = {"expressions": ["{{ end() }}", "{{ count() }}"], "body": {"contains": ["{{ count() }}"]}}
        with pytest.raises(pytest.skip.Exception, match="^not here$"):
            self._run(verify, {"end": lambda: pytest.skip("not here"), "count": lambda: calls.append(1)})
        assert calls == []

    def test_one_evaluator_renders_the_step(self, monkeypatch):
        """Built from the whole context, which gains a layer per stage and per
        save step: once per value, that cost the step its context's size again
        for every templated value."""
        built = []
        real = substitution_module._build_evaluator
        monkeypatch.setattr(substitution_module, "_build_evaluator", lambda context: built.append(1) or real(context))
        verify = {
            "status": "{{ 200 }}",
            "headers": {"Content-Type": {"contains": "{{ 'json' }}"}},
            "jmespath": {"id": "{{ 1 }}"},
            "expressions": ["{{ missing }}", "{{ 1 == 2 }}"],
            "body": {"contains": ["{{ 'tags' }}", "{{ gone }}"]},
        }
        lines = str(self._failure(verify, {"gone": None})).split("\n")
        assert lines[0] == "3 verification checks failed:"
        assert built == [1]

    def test_each_template_renders_once(self):
        """A list item renders alone: none is rendered again for another's
        check, nor for its own failure's message."""
        calls = []
        count = lambda: calls.append(1) or len(calls)  # noqa: E731
        verify = {
            "expressions": ["{{ count() == 0 }}", "{{ count() == 0 }}", "{{ count() == 0 }}"],
            "body": {"contains": ["{{ 'x' * count() }}", "{{ [count()][5] }}", "{{ gone(count()) }}"]},
        }
        lines = str(self._failure(verify, {"count": count, "gone": lambda _: None})).split("\n")
        assert len(calls) == 6
        assert lines[4] == "  4. Body doesn't contain 'xxxx'"
        assert lines[5].startswith("  5. IndexError in expression ")
        assert lines[6:8] == ["  6. 1 validation error for Verify", "     body.contains.2"]

    def test_step_that_renders_valid_is_validated_once(self, monkeypatch):
        """Whole, as when the step rendered whole: validating each value on its
        own is what a failure pays for."""
        validated = self._spy_validation(monkeypatch)
        self._run({"status": "{{ 200 }}", "expressions": ["{{ true }}"] * 3, "body": {"contains": ["{{ 'id' }}", "{{ 'tags' }}"]}}, {})
        assert validated == [{"status": 200, "expressions": [True] * 3, "body": {"contains": ["id", "tags"]}}]

    def test_list_item_is_validated_alone(self, monkeypatch):
        """Once the step fails validation whole. Validated with its list whole,
        every item of a list of n templated ones cost the list: n² for the
        step. So did an item that fails, validated again with its list for its
        index in the message, when many did: its index is put there instead."""
        validated = self._spy_validation(monkeypatch)
        lines = str(self._failure({"expressions": ["{{ true }}"] * 3, "body": {"contains": ["{{ 'id' }}", "{{ 'tags' }}", "{{ gone }}"]}}, {"gone": None})).split("\n")
        assert lines[:2] == ["1 validation error for Verify", "body.contains.2"]
        assert validated == [
            {"expressions": [True] * 3, "body": {"contains": ["id", "tags", None]}},
            *[{"expressions": [True]}] * 3,
            {"body": {"contains": ["id"]}},
            {"body": {"contains": ["tags"]}},
            {"body": {"contains": [None]}},
        ]

    def test_failing_list_items_each_keep_their_index(self):
        """As their list whole gave it: pydantic's report, each error of its
        type, message and input (its link left out here: it names pydantic's
        version), and the guard's refusal, at the index each item has in the
        step."""
        verify = {
            "user_functions": ["{{ gone }}", "tests.unit.response_steps_test_helpers:returns_true", {"name": "{{ gone }}"}],
            "body": {"matches": ["{{ paren }}", "a", "{{ gone }}"]},
        }
        error = self._failure(verify, {"gone": None, "paren": "("})
        assert [line for line in str(error).split("\n") if "For further information visit" not in line] == [
            "4 verification checks failed:",
            "  1. 'verify.user_functions[0]' was declared as '{{ gone }}' but rendered to None",
            "  2. 'verify.user_functions[2].name' was declared as '{{ gone }}' but rendered to None",
            "  3. 1 validation error for Verify",
            "     body.matches.0",
            "       Value error, Invalid regular expression [type=value_error, input_value='(', input_type=str]",
            "  4. 1 validation error for Verify",
            "     body.matches.2",
            "       Input should be a valid string [type=string_type, input_value=None, input_type=NoneType]",
        ]

    def test_relocated_report_reads_as_pydantic_wrote_it(self):
        """Its own errors rebuilt with their context, link included; a custom
        one, whose template is gone, with its type and message."""
        custom = PydanticCustomError("custom_kind", "Custom {text}", {"text": "words"})
        error = ValidationError.from_exception_data(
            "Verify",
            [
                {"type": "value_error", "loc": ("body", "matches", 0), "input": "(", "ctx": {"error": ValueError("Invalid regular expression")}},
                {"type": custom, "loc": ("body", "matches", 0, "x"), "input": 1},
                {"type": "missing", "loc": ("status",), "input": {}},
            ],
        )
        moved = carrier_module._relocated(error, carrier_module._relocation(("body", "matches", 0), ("body", "matches", 7)))
        assert str(moved) == str(error).replace("body.matches.0", "body.matches.7")
        assert [(e["type"], e["loc"], e["msg"]) for e in moved.errors()] == [
            ("value_error", ("body", "matches", 7), "Value error, Invalid regular expression"),
            ("custom_kind", ("body", "matches", 7, "x"), "Custom words"),
            ("missing", ("status",), "Field required"),
        ]

    @staticmethod
    def _spy_validation(monkeypatch) -> list:
        """What the renderer validates, each time it does, from now on."""
        validated = []
        real = carrier_module.validate_rendered_verify

        def spy(declared, value):
            validated.append(value)
            return real(declared, value)

        monkeypatch.setattr(carrier_module, "validate_rendered_verify", spy)
        return validated

    @pytest.mark.parametrize(
        ("function", "verify", "message"),
        [
            pytest.param(
                "skips",
                {"body": {"contains": ["{{ response.headers['x-missing'] }}"]}},
                "KeyError in expression '{{ response.headers['x-missing'] }}': 'x-missing'",
                id="skip-before-template-error",
            ),
            pytest.param(
                "xfails",
                {"body": {"schema": "{{ gone }}"}},
                "'verify.body.schema' was declared as '{{ gone }}' but rendered to None, which would silently disable it",
                id="xfail-before-rendered-away-schema",
            ),
        ],
    )
    def test_function_outcome_cannot_hide_a_template_failure_after_it(self, function, verify, message):
        """Every value renders before the first check runs: a template that
        fails fails the stage wherever it is, as when the step rendered whole,
        and a function's skip or xfail ending the step before its check does
        not turn that into a skipped or xfailed stage."""
        verify = {"status": 200, "user_functions": [f"tests.unit.response_steps_test_helpers:{function}"], **verify}
        assert str(self._failure(verify, {"gone": None})) == message


class TestVerifyStatusRendered:
    """``verify.status`` written as one template renders to any of its forms (a
    code, a class, a list of them), and a list entry written as one to a code
    or a class: the rendered value is checked as if it had been written so. The
    whole field rendering to None is the rendered-away guard's
    (`TestRenderedAwayFields`)."""

    @staticmethod
    def _run(status, context, *codes: int) -> None:
        """Run a stage verifying ``status`` once per answer in ``codes``."""
        stage = Stage.model_validate({"name": "s", "request": {"url": "http://mock/ok"}, "response": [{"verify": {"status": status}}]})
        responses = iter(httpx.Response(code) for code in codes)
        client = httpx.Client(transport=httpx.MockTransport(lambda request: next(responses)))
        cls = _make_carrier_subclass(client=client)
        try:
            for _ in codes:
                cls._execute_single_iteration(stage, context, {})
        finally:
            client.close()

    @pytest.mark.parametrize(
        ("status", "value", "passes", "message"),
        [
            pytest.param("{{ s }}", 201, 201, "expected 201, got 500", id="code"),
            # Text, as a value saved from a header is.
            pytest.param("{{ s }}", "201", 201, "expected 201, got 500", id="stringified-code"),
            pytest.param("{{ s }}", "2XX", 204, "expected 2xx, got 500", id="class"),
            pytest.param("{{ s }}", [200, 201], 201, "expected one of [200, 201], got 500", id="list"),
            pytest.param("{{ s }}", ["2xx", 304], 304, "expected one of [2xx, 304], got 500", id="list-mixed"),
            pytest.param(["{{ s }}", 304], 201, 201, "expected one of [201, 304], got 500", id="entry-code"),
            pytest.param(["{{ s }}", 304], "2xx", 299, "expected one of [2xx, 304], got 500", id="entry-class"),
        ],
    )
    @pytest.mark.parametrize("source", ["saved", "vars"])
    def test_rendered_form_is_checked(self, status, value, passes, message, source):
        context = ChainMap(VarsSubstitution(vars={"s": value}).vars) if source == "vars" else ChainMap({"s": value})
        with pytest.raises(VerificationError) as excinfo:
            self._run(status, context, passes, 500)
        assert str(excinfo.value) == f"Status code doesn't match: {message}"

    @pytest.mark.parametrize(
        ("status", "value"),
        [
            # A list entry is a value in the list, as an exact header string is
            # one in the `headers` map: its None fails re-validation, which is
            # pydantic's to report. Dropping it would have passed the 304.
            pytest.param(["{{ s }}", 304], None, id="entry-none"),
            pytest.param("{{ s }}", [None, 304], id="list-with-none"),
            # Would match nothing, or anything.
            pytest.param("{{ s }}", [], id="empty-list"),
            pytest.param("{{ s }}", "6xx", id="not-a-class"),
        ],
    )
    def test_invalid_rendered_value_fails_the_stage(self, status, value):
        with pytest.raises(StageExecutionError) as excinfo:
            self._run(status, ChainMap({"s": value}), 304)
        assert isinstance(excinfo.value.__cause__, ValidationError)
        assert re.match(r"\d+ validation errors? for Verify\nstatus", str(excinfo.value))
        assert excinfo.value.response is not None


class TestVerifyJmespathRendered:
    """``verify.jmespath`` values are rendered with the response step's context,
    as every verify field is, and then checked as if written so, each as the
    kind it was declared as: a value stays a value, whatever it renders. The
    None rule splits on where the template sits: a value is compared with null,
    a matcher key is a declared field the rendered-away guard refuses."""

    BODY = {"data": {"id": 42, "owner": 42, "meta": {"page": 1}}, "deleted_at": None, "code": 200}

    @classmethod
    def _run(cls, response: list, context: ChainMap, body: object = BODY) -> None:
        """Run one stage against a 200 whose JSON body is ``body``."""
        stage = Stage.model_validate({"name": "s", "request": {"url": "http://mock/ok"}, "response": response})
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
        carrier = _make_carrier_subclass(client=client)
        try:
            carrier._execute_single_iteration(stage, context, {})
        finally:
            client.close()

    @pytest.mark.parametrize(
        "response",
        [
            pytest.param([{"verify": {"jmespath": {"data.id": "{{ uid }}", "data.meta": {"eq": "{{ meta }}"}}}}], id="context"),
            # The response namespace and a prior step's save are in scope, as
            # for any response step.
            pytest.param([{"verify": {"jmespath": {"code": "{{ response.status }}"}}}], id="response-namespace"),
            pytest.param([{"save": {"jmespath": {"owner": "data.owner"}}}, {"verify": {"jmespath": {"data.id": "{{ owner }}"}}}], id="prior-save"),
            pytest.param([{"verify": {"jmespath": {"data.id": {"gt": "{{ uid - 1 }}", "type": "{{ kind }}"}, "data.meta": {"length": "{{ one }}"}}}}], id="matcher-keys"),
        ],
    )
    @pytest.mark.parametrize("source", ["saved", "vars"])
    def test_rendered_value_is_checked(self, response, source):
        values = {"uid": 42, "meta": {"page": 1}, "kind": "integer", "one": 1}
        self._run(response, ChainMap(VarsSubstitution(vars=values).vars) if source == "vars" else ChainMap(values))

    def test_rendered_value_mismatch_fails(self):
        with pytest.raises(VerificationError, match=r"^JMESPath 'data.id' doesn't match: expected 41, got 42$"):
            self._run([{"verify": {"jmespath": {"data.id": "{{ uid }}"}}}], ChainMap({"uid": 41}))

    def test_value_rendered_to_none_is_compared_with_null(self):
        """A value is not a field: its template rendering to None stands for
        null, which the path must then hold."""
        response = [{"verify": {"jmespath": {"deleted_at": "{{ gone }}"}}}]
        self._run(response, ChainMap({"gone": None}))
        with pytest.raises(VerificationError, match=r"^JMESPath 'deleted_at' doesn't match: expected null, got 1$"):
            self._run(response, ChainMap({"gone": None}), {"deleted_at": 1})

    @pytest.mark.parametrize("key", ["eq", "ne", "contains", "not_contains"])
    def test_operand_rendered_to_none_is_refused(self, key):
        """null is an operand here, so the None validates and would disable
        nothing, but it would compare with null in place of the value the
        template was written for (a save of a missing key): refused, saying how
        to compare with null. Quoted, since the expression is not a plain name."""
        with pytest.raises(VerificationError) as excinfo:
            self._run([{"verify": {"jmespath": {"data.id": {key: "{{ x }}"}}}}], ChainMap({"x": None}))
        assert str(excinfo.value) == f"'verify.jmespath[\"data.id\"].{key}' was declared as " + "'{{ x }}' but rendered to None; to compare with null, write null"

    @pytest.mark.parametrize(
        "matcher",
        [
            pytest.param({"gt": "{{ x }}"}, id="only-key"),
            # Beside a static key the matcher would still validate without it.
            pytest.param({"gt": "{{ x }}", "lt": 100}, id="beside-a-static-key"),
        ],
    )
    def test_key_rendered_to_none_is_refused(self, matcher):
        """null is no operand of gt: validation refuses it, and the guard names
        the template instead."""
        with pytest.raises(VerificationError) as excinfo:
            self._run([{"verify": {"jmespath": {"code": matcher}}}], ChainMap({"x": None}))
        assert str(excinfo.value) == "'verify.jmespath.code.gt' was declared as '{{ x }}' but rendered to None"

    @pytest.mark.parametrize(
        ("rendered", "actual"),
        [
            pytest.param({"page": 1}, "data.meta", id="object"),
            # Keys a matcher also has make it no matcher: a saved JSON Schema
            # fragment, compared as one, passed against any object.
            pytest.param({"type": "object"}, "shape", id="matcher-keys"),
            pytest.param({"length": 1}, "sized", id="length-key"),
            pytest.param({"eq": None, "gt": 0}, "operands", id="null-operand"),
        ],
    )
    @pytest.mark.parametrize("source", ["saved", "vars"])
    def test_object_rendered_at_a_value_is_compared_as_one(self, rendered, actual, source):
        """What was declared decides, not what rendered: a template where a
        value is written renders a value, an object included, compared by JSON
        equality. Only an object the scenario writes is a matcher, so one whose
        keys happen to be a matcher's is never checked as that matcher, and an
        explicit null member is an operand of nothing."""
        body = {"data": {"meta": {"page": 1}}, "shape": {"type": "object"}, "sized": {"length": 1}, "operands": {"eq": None, "gt": 0}}
        context = ChainMap(VarsSubstitution(vars={"expected": rendered}).vars) if source == "vars" else ChainMap({"expected": rendered})
        self._run([{"verify": {"jmespath": {actual: "{{ expected }}"}}}], context, body)
        with pytest.raises(VerificationError) as excinfo:
            self._run([{"verify": {"jmespath": {"code": "{{ expected }}"}}}], context, {"code": {"type": "array"}})
        assert str(excinfo.value) == f"JMESPath 'code' doesn't match: expected {json.dumps(rendered)}, got " + '{"type": "array"}'

    def test_rendered_object_in_an_array_is_compared(self):
        response = [{"verify": {"jmespath": {"items": ["{{ first }}", 2]}}}]
        self._run(response, ChainMap(VarsSubstitution(vars={"first": {"length": 1}}).vars), {"items": [{"length": 1}, 2.0]})


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


class TestSkipIf:
    """``skip_if`` decides once, when the stage is about to run: after the abort
    gate and the stage's own substitutions, before the parallel config and any
    request. A skip sends nothing and commits no saves; that it leaves the
    chain healthy is the report hook's, pinned by the integration suite."""

    @staticmethod
    def _run(stage_fields: dict, fixtures: dict | None = None, carrier: type[Carrier] | None = None) -> tuple[type[Carrier], list[httpx.Request]]:
        """Run one stage saving ``v`` from a mock server answering 200, on
        ``carrier`` (a fresh one by default); what it sent comes back too."""
        sent: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(200, json={"v": 1})

        cls = carrier if carrier is not None else _make_carrier_subclass()
        cls.client = httpx.Client(transport=httpx.MockTransport(respond))
        stage = Stage.model_validate({"name": "s", "request": {"url": "http://mock/"}, "response": [{"save": {"jmespath": {"v": "v"}}}], **stage_fields})
        try:
            cls.execute_stage(stage, fixtures or {})
        finally:
            cls.client.close()
        return cls, sent

    @pytest.mark.parametrize(
        ("stage_fields", "fixtures", "global_context"),
        [
            # pytest hands a stage its fixtures and parametrize parameters alike.
            pytest.param({}, {"flag": True}, {}, id="fixture-or-parameter"),
            pytest.param({}, {}, {"flag": True}, id="scenario-substitution-or-earlier-save"),
            # Evaluated after the stage's substitutions, which shadow a save.
            pytest.param({"substitutions": [{"vars": {"flag": True}}]}, {}, {"flag": False}, id="stage-substitution"),
        ],
    )
    def test_skips_when_it_holds(self, stage_fields, fixtures, global_context):
        cls = _make_carrier_subclass(global_context=ChainMap(global_context))
        with pytest.raises(pytest.skip.Exception, match=r"^skip_if: \{\{ flag \}\}$"):
            self._run({"skip_if": " {{ flag }} ", **stage_fields}, fixtures, carrier=cls)
        assert "v" not in cls.global_context
        assert (cls.aborted, cls.last_request, cls.last_exchanges) == (False, None, [])

    def test_runs_when_it_does_not_hold(self):
        cls, sent = self._run({"skip_if": "{{ flag }}"}, {"flag": False})
        assert len(sent) == 1
        assert cls.global_context["v"] == 1

    def test_literal_true_skips_before_anything_runs(self, monkeypatch):
        """A literal reads no context, so neither the scenario's initialization
        nor the stage's substitutions run for a stage that skips anyway."""

        def not_called(*args, **kwargs):
            raise AssertionError("resolved for a stage that skips")

        monkeypatch.setattr(carrier_module, "process_substitutions", not_called)
        cls = _make_carrier_subclass(scenario=Scenario(), _initialized=False)
        with pytest.raises(pytest.skip.Exception, match=r"^skip_if: true$"):
            self._run({"skip_if": True, "substitutions": [{"vars": {"x": 1}}]}, carrier=cls)
        assert not cls._initialized

    @pytest.mark.parametrize(
        ("value", "got"),
        [
            # Truthy as a string: by truthiness, the stage would skip.
            pytest.param("false", "str", id="string"),
            # A credential, as `{{ token }}` for "skip when there is a token"
            # renders: the message names its type, never the value.
            pytest.param("s3cret-token", "str", id="credential"),
            pytest.param(1, "int", id="number"),
            # A JMESPath save of a missing key: read as false, the stage would run.
            pytest.param(None, "None", id="none"),
        ],
    )
    def test_value_that_is_no_bool_fails_the_stage(self, value, got):
        cls = _make_carrier_subclass()
        with pytest.raises(pytest.fail.Exception) as excinfo:
            self._run({"skip_if": "{{ answer }}"}, {"answer": value}, carrier=cls)
        assert str(excinfo.value) == f"skip_if must evaluate to bool, got {got} from '{{{{ answer }}}}'"
        assert "v" not in cls.global_context

    @pytest.mark.parametrize(
        ("stage_fields", "name"),
        [
            # One decision per stage, before any iteration exists.
            pytest.param({"skip_if": "{{ item == 1 }}", "parallel": {"foreach": [{"individual": {"item": [1, 2]}}]}}, "item", id="foreach-parameter"),
            pytest.param({"skip_if": "{{ response.status == 500 }}"}, "response", id="response"),
        ],
    )
    def test_out_of_scope_name_fails_the_stage(self, stage_fields, name):
        with pytest.raises(pytest.fail.Exception, match=rf"^Failed to evaluate skip_if template: Undefined variable in expression .*'{name}' is not defined"):
            self._run(stage_fields)

    @pytest.mark.parametrize(
        ("always_run", "skip_if", "outcome", "reason"),
        [
            # The abort gate comes first: this skip_if is never evaluated.
            pytest.param(False, "{{ undefined_name }}", pytest.skip.Exception, "Flow aborted", id="aborted"),
            # always_run lets the stage past the gate, and skip_if still counts.
            pytest.param(True, "{{ true }}", pytest.skip.Exception, "skip_if: {{ true }}", id="always-run-skips"),
            pytest.param(True, "{{ false }}", None, None, id="always-run-runs"),
        ],
    )
    def test_after_an_abort(self, always_run, skip_if, outcome, reason):
        cls = _make_carrier_subclass(aborted=True)
        if outcome is None:
            _, sent = self._run({"always_run": always_run, "skip_if": skip_if}, carrier=cls)
            assert len(sent) == 1
        else:
            with pytest.raises(outcome) as excinfo:
                self._run({"always_run": always_run, "skip_if": skip_if}, carrier=cls)
            assert str(excinfo.value) == reason


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


class TestCollectSaves:
    """What a parallel stage commits: its iterations' saves in iteration order,
    merged (the highest index wins a name) or, with ``collect_saves``, each
    name a list of one entry per iteration. Only a stage whose iterations all
    succeeded commits anything."""

    @pytest.mark.parametrize(
        ("saved", "collect", "merged"),
        [
            pytest.param([{"a": 1, "b": 1}, {"a": 2}, {"b": 3}], False, {"a": 2, "b": 3}, id="merged-highest-index-wins"),
            # A name an iteration did not save is None in its place, whichever
            # iteration saved it first.
            pytest.param([{"a": 1, "b": 1}, {"a": 2}, {"c": 3}], True, {"a": [1, 2, None], "b": [1, None, None], "c": [None, None, 3]}, id="collected-missing-names"),
            # Indistinguishable from not saving it, as documented.
            pytest.param([{"a": None}, {}], True, {"a": [None, None]}, id="collected-saved-none"),
            pytest.param([{"a": 1}], True, {"a": [1]}, id="collected-one-iteration"),
            pytest.param([{}, {}], True, {}, id="collected-nothing-saved"),
        ],
    )
    def test_merge(self, saved, collect, merged):
        assert _merged_saves(saved, collect) == merged

    # Each iteration saves the id the server gave it, and its own foreach parameter.
    _SAVES = ({"save": {"jmespath": {"ids": "id"}}}, {"save": {"substitutions": [{"vars": {"ns": "{{ n }}"}}]}})

    @staticmethod
    def _item(request: httpx.Request) -> httpx.Response:
        """The mock server's answer to ``/item/<n>``: ``{"id": "id-<n>"}``."""
        return httpx.Response(200, json={"id": f"id-{request.url.path.rsplit('/', 1)[-1]}"})

    @classmethod
    def _run(
        cls, parallel: dict, response: tuple | list = _SAVES, carrier: type[Carrier] | None = None, respond: Callable[[httpx.Request], httpx.Response] | None = None
    ) -> type[Carrier]:
        """Run a stage fetching ``/item/{{ n }}`` in parallel from ``respond``
        (`_item` by default), on ``carrier`` (a fresh one by default)."""
        carrier = carrier if carrier is not None else _make_carrier_subclass()
        carrier.client = httpx.Client(transport=httpx.MockTransport(respond or cls._item))
        stage = Stage.model_validate({"name": "s", "parallel": parallel, "request": {"url": "http://mock/item/{{ n }}"}, "response": list(response)})
        try:
            carrier.execute_stage(stage, {})
        finally:
            carrier.client.close()
        return carrier

    @pytest.mark.parametrize(
        ("collect", "ids", "ns"),
        [
            # Iteration 0 completes last, yet the last writer is iteration 2.
            pytest.param(False, "id-2", 2, id="merged"),
            pytest.param(True, ["id-0", "id-1", "id-2"], [0, 1, 2], id="collected"),
        ],
    )
    def test_iteration_order_not_completion_order(self, collect, ids, ns):
        """The server answers iteration 0 only once it has answered the
        others, so it completes last."""
        answered: list[str] = []
        lock = threading.Lock()

        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/item/0":
                _wait_until(lambda: len(answered) == 2)
            with lock:
                answered.append(request.url.path)
            return self._item(request)

        cls = self._run({"foreach": [{"individual": {"n": [0, 1, 2]}}], "max_concurrency": 3, "collect_saves": collect}, respond=respond)
        assert answered[-1] == "/item/0"
        assert (cls.global_context["ids"], cls.global_context["ns"]) == (ids, ns)

    def test_foreach_steps_in_iteration_order(self):
        """Entry i is the iteration with the foreach parameters of index i: the
        first step's values go fastest."""
        cls = self._run(
            {"foreach": [{"individual": {"n": [1, 2]}}, {"individual": {"tag": ["x", "y"]}}], "collect_saves": True},
            response=[{"save": {"substitutions": [{"vars": {"pair": "{{ [n, tag] }}"}}]}}],
        )
        assert cls.global_context["pair"] == [[1, "x"], [2, "x"], [1, "y"], [2, "y"]]

    def test_repeat(self):
        cls = self._run({"repeat": 3, "collect_saves": True}, carrier=_make_carrier_subclass(global_context=ChainMap({"n": 7})))
        assert cls.global_context["ids"] == ["id-7", "id-7", "id-7"]

    def test_an_iteration_sees_its_own_value(self):
        """The lists are what the stage commits: inside it, an iteration's
        later steps read the value it saved itself."""
        cls = self._run(
            {"foreach": [{"individual": {"n": [0, 1]}}], "collect_saves": True},
            response=[{"save": {"jmespath": {"ids": "id"}}}, {"verify": {"expressions": ["{{ ids == 'id-' + str(n) }}"]}}],
        )
        assert cls.global_context["ids"] == ["id-0", "id-1"]

    def test_a_failed_iteration_commits_nothing(self):
        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500) if request.url.path == "/item/1" else self._item(request)

        cls = _make_carrier_subclass()
        with pytest.raises(pytest.fail.Exception, match=r"^Parallel execution failed at iteration 1: Status code doesn't match: expected 200, got 500$"):
            self._run(
                {"foreach": [{"individual": {"n": [0, 1, 2]}}], "collect_saves": True},
                response=[{"verify": {"status": 200}}, *self._SAVES],
                carrier=cls,
                respond=respond,
            )
        assert "ids" not in cls.global_context

    @pytest.mark.parametrize(
        ("value", "ids"),
        [
            pytest.param(True, ["id-0", "id-1"], id="true"),
            pytest.param(False, "id-1", id="false"),
            # The numbers 1 and 0 are true and false, as in every bool
            # setting the model re-validates once rendered (client.http2).
            pytest.param(1, ["id-0", "id-1"], id="one"),
            pytest.param(0, "id-1", id="zero"),
        ],
    )
    def test_templated(self, value, ids):
        """Rendered with the rest of the parallel config, against the stage-local context."""
        carrier = _make_carrier_subclass(global_context=ChainMap({"keep_all": value}))
        cls = self._run({"foreach": [{"individual": {"n": [0, 1]}}], "collect_saves": "{{ keep_all }}"}, carrier=carrier)
        assert cls.global_context["ids"] == ids

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            # Truthy as text: read as it is, it would collect.
            pytest.param("{{ x }}", r"^parallel\.collect_saves must resolve to true or false, got '\{\{ x \}\}'$", id="template-text"),
            # Invalid for the field, as any value but a bool is: refused
            # with the template named, not pydantic's literal error.
            pytest.param(None, r"^'parallel\.collect_saves' was declared as '\{\{ flag \}\}' but rendered to None$", id="none"),
            # Text is no bool, even the text of one.
            pytest.param("true", r"(?s)^\d+ validation errors for ParallelRepeatConfig\ncollect_saves\.literal\[True,False\]\n  Input should be True or False", id="text"),
            # Only 1 and 0 of the numbers count as a bool.
            pytest.param(2, r"(?s)^\d+ validation errors for ParallelRepeatConfig\ncollect_saves\.literal\[True,False\]\n  Input should be True or False", id="other-number"),
        ],
    )
    def test_unusable_value_fails_before_any_request(self, value, message):
        sent: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return self._item(request)

        with pytest.raises(pytest.fail.Exception, match=message):
            self._run({"repeat": 2, "collect_saves": "{{ flag }}"}, carrier=_make_carrier_subclass(global_context=ChainMap({"flag": value})), respond=respond)
        assert sent == []

    def test_foreach_parameter_is_out_of_scope(self):
        """One list shape for the whole stage, decided before its iterations
        exist, as the validator's HTTPCHAIN003 at ``stages[i].parallel`` says."""
        with pytest.raises(pytest.fail.Exception, match=r"Undefined variable in expression .*'n' is not defined"):
            self._run({"foreach": [{"individual": {"n": [0, 1]}}], "collect_saves": "{{ n == 0 }}"})


# A response of the mock job the retry tests poll: pending until its
# `done_at`-th request, done from then on.
def _job(done_at: int) -> Callable[[int, httpx.Request], httpx.Response]:
    def respond(n: int, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "done" if n >= done_at else "pending", "n": n})

    return respond


# What waits for a job to be done: a check that the attempt starts from a
# context without the saves of the attempts before it, the save of the
# response's number, the check that fails while the job is pending.
_POLL = [
    {"verify": {"expressions": ["{{ not exists('n') }}"]}},
    {"save": {"jmespath": {"n": "n"}}},
    {"verify": {"jmespath": {"status": "done"}}},
]


class TestRetry:
    """A stage's ``retry``: after an attempt that fails in a way its ``on``
    names and another attempt may change, a wait of the backoff schedule and
    another attempt, which renders the request anew and runs every response
    step in a fresh context, until one passes or none is left. The waits are
    recorded, not waited (`_waits`), but where cancellation is the subject."""

    @staticmethod
    def _waits(monkeypatch) -> list[float]:
        """The waits before each retry, recorded instead of waited."""
        waited: list[float] = []

        def wait(seconds: float, cancel: threading.Event | None) -> bool:
            waited.append(seconds)
            return True

        monkeypatch.setattr(carrier_module, "_wait_to_retry", wait)
        return waited

    @staticmethod
    def _run(
        stage_fields: dict, respond: Callable[[int, httpx.Request], httpx.Response], carrier: type[Carrier] | None = None, fixtures: dict | None = None
    ) -> tuple[type[Carrier], list[httpx.Request]]:
        """Run a stage GETting ``http://mock/job`` unless ``stage_fields`` say
        otherwise, answered by ``respond(n, request)`` for the n-th request,
        on ``carrier`` (a fresh one by default). The requests sent come back
        with the carrier, whether the stage passed or not: read them in a
        ``finally``, or from a stage that passed."""
        sent: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return respond(len(sent), request)

        carrier = carrier if carrier is not None else _make_carrier_subclass()
        carrier.client = httpx.Client(transport=httpx.MockTransport(handler))
        stage = Stage.model_validate({"name": "s", "request": {"url": "http://mock/job"}, **stage_fields})
        try:
            carrier.execute_stage(stage, fixtures or {})
        finally:
            carrier.client.close()
        return carrier, sent

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
        assert list(carrier_module._retry_policy(RetryConfig.model_validate(retry)).waits()) == waits

    def test_wait_schedule_past_floats_range(self):
        """A backoff doubling a thousand times overflows to inf, which the
        cap takes; uncapped, the wait is inf, which `_wait_to_retry` bounds."""
        waits = list(carrier_module._retry_policy(RetryConfig(attempts=1100, delay=1, backoff=2, max_delay=60)).waits())
        assert (len(waits), waits[:3], waits[-1]) == (1099, [1.0, 2.0, 4.0], 60.0)
        assert list(carrier_module._retry_policy(RetryConfig(attempts=1100, delay=1, backoff=2)).waits())[-1] == math.inf

    def test_polls_until_the_response_steps_pass(self, monkeypatch):
        """Each attempt renders its request anew, a uuid4() included, and runs
        every response step in a fresh context: the failed attempts' saves are
        gone, and the stage commits the one that passed."""
        waited = self._waits(monkeypatch)
        cls, sent = self._run(
            {"retry": {"attempts": 5, "delay": 0.5, "backoff": 2}, "request": {"url": "http://mock/job?attempt={{ uuid4() }}"}, "response": _POLL},
            _job(done_at=3),
        )
        assert len(sent) == 3
        assert len({request.url.params["attempt"] for request in sent}) == 3
        assert waited == [0.5, 1.0]
        assert cls.global_context["n"] == 3
        # The report shows the attempt that passed, and says which it is.
        assert (cls.last_response.json(), cls.last_shown_attempt) == ({"status": "done", "n": 3}, (3, 5))

    def test_first_attempt_passing_waits_for_nothing(self, monkeypatch):
        waited = self._waits(monkeypatch)
        cls, sent = self._run({"retry": {"attempts": 5}, "response": _POLL}, _job(done_at=1))
        assert (len(sent), waited, cls.last_shown_attempt) == (1, [], None)

    def test_last_attempt_failing_fails_the_stage(self, monkeypatch):
        """With its failure, counting the attempts, and committing nothing."""
        self._waits(monkeypatch)
        cls = _make_carrier_subclass()
        with pytest.raises(pytest.fail.Exception, match=r"""^JMESPath 'status' doesn't match: expected "done", got "pending" \(after 3 attempts\)$"""):
            self._run({"retry": {"attempts": 3}, "response": _POLL}, _job(done_at=4), carrier=cls)
        assert "n" not in cls.global_context
        assert (cls.last_response.json()["n"], cls.last_shown_attempt, cls.last_shown_exchange_is_failed) == (3, (3, 3), True)

    @staticmethod
    def _raise(error: Exception) -> Callable[[int, httpx.Request], httpx.Response]:
        """Raise ``error`` for the first request, and answer the next with a done job."""

        def respond(n: int, request: httpx.Request) -> httpx.Response:
            if n == 1:
                raise error
            return _job(done_at=1)(n, request)

        return respond

    @staticmethod
    def _answer(first: httpx.Response) -> Callable[[int, httpx.Request], httpx.Response]:
        """Answer the first request with ``first``, and the next with a done job."""
        return lambda n, request: first if n == 1 else _job(done_at=1)(n, request)

    # The response steps each row fails on its first attempt.
    _STATUS = [{"verify": {"status": 200}}]
    _SAVE = [{"save": {"jmespath": {"status": "status"}}}]
    _TEXT = httpx.Response(200, text="<html>Queued</html>")
    _PENDING = httpx.Response(200, json={"status": "pending"})
    _HELPERS = "tests.unit.response_steps_test_helpers"

    @pytest.mark.parametrize(
        ("on", "respond", "response"),
        [
            pytest.param("verify", _answer(httpx.Response(503)), _STATUS, id="verify"),
            pytest.param(["save"], _answer(_TEXT), _SAVE, id="save-body-not-json"),
            # One kind of several.
            pytest.param(["verify", "save"], _answer(_TEXT), _SAVE, id="save-of-several"),
            pytest.param("request", _raise(httpx.ConnectError("refused")), _STATUS, id="connection-refused"),
            pytest.param("request", _raise(httpx.ReadTimeout("slow")), _STATUS, id="timeout"),
            pytest.param("request", _raise(httpx.ReadError("reset")), _STATUS, id="connection-broken-off"),
            pytest.param("request", _raise(httpx.RemoteProtocolError("Server disconnected without sending a response.")), _STATUS, id="server-disconnected"),
            # A function saying "not yet" with the step's own failure.
            pytest.param("verify", _answer(_PENDING), [{"verify": {"user_functions": [f"{_HELPERS}:done_or_not_yet"]}}], id="verify-function-raised-verification-error"),
            pytest.param("save", _answer(_PENDING), [{"save": {"user_functions": [f"{_HELPERS}:result_or_not_yet"]}}], id="save-function-raised-save-error"),
        ],
    )
    def test_retried(self, monkeypatch, on, respond, response):
        """A failure of a kind ``on`` names: the second attempt passes."""
        self._waits(monkeypatch)
        _cls, sent = self._run({"retry": {"attempts": 3, "on": on}, "response": response}, respond)
        assert len(sent) == 2

    @pytest.mark.parametrize(
        ("on", "respond", "response", "message"),
        [
            # A kind `on` does not name.
            pytest.param("save", _answer(httpx.Response(503)), _STATUS, r"^Status code doesn't match: expected 200, got 503$", id="verify-not-named"),
            pytest.param("verify", _answer(_TEXT), _SAVE, r"^Cannot extract variables, response is not valid JSON", id="save-not-named"),
            pytest.param(["verify", "save"], _raise(httpx.ConnectError("refused")), _STATUS, r"^HTTP connection error: refused$", id="request-not-named"),
            # A request error the next attempt would repeat, `on: request` or not.
            pytest.param(
                "request", _raise(httpx.LocalProtocolError("Illegal header value")), _STATUS, r"^HTTP request failed: Illegal header value$", id="request-refused-by-httpx"
            ),
            pytest.param("request", _raise(httpx.TooManyRedirects("Exceeded maximum allowed redirects.")), _STATUS, r"^HTTP request failed: Exceeded", id="too-many-redirects"),
            # What auth code raised, httpx running it inside the request.
            pytest.param("request", _raise(RuntimeError("token service down")), _STATUS, r"^Unexpected error during HTTP request: token service down$", id="auth-code-raised"),
        ],
    )
    def test_not_retried(self, monkeypatch, on, respond, response, message):
        waited = self._waits(monkeypatch)
        sent: list[httpx.Request] = []

        def counted(n: int, request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return respond(n, request)

        with pytest.raises(pytest.fail.Exception, match=message):
            self._run({"retry": {"attempts": 3, "on": on}, "response": response}, counted)
        assert (len(sent), waited) == (1, [])

    @pytest.mark.parametrize(
        ("stage_fields", "requests", "message"),
        [
            # The scenario's own templates fail every attempt alike.
            pytest.param({"request": {"url": "http://mock/{{ missing }}"}}, 0, "'missing' is not defined", id="request-template"),
            pytest.param({"request": {"url": "http://mock/job", "timeout": "{{ gone }}"}}, 0, r"^'request\.timeout' was declared as", id="request-rendered-to-none"),
            pytest.param(
                {"response": [{"verify": {"status": 200, "expressions": ["{{ missing > 1 }}"]}}]},
                1,
                r"^2 verification checks failed:\n  1\. Status code doesn't match: expected 200, got 503\n  2\. .*'missing' is not defined",
                id="verify-value-among-check-failures",
            ),
            pytest.param({"response": [{"verify": {"status": "{{ gone }}"}}]}, 1, r"^'verify\.status' was declared as", id="verify-rendered-to-none"),
            pytest.param({"response": [{"save": {"jmespath": {"x": "{{ gone }}"}}}]}, 1, r"^2 validation errors for JMESPathSave\njmespath\.x", id="save-rendered-invalid"),
            pytest.param(
                {"response": [{"save": {"regex": {"x": {"pattern": "(a)", "group": "{{ gone }}"}}}}]}, 1, r"^'save\.regex\.x\.group' was declared as", id="save-rendered-to-none"
            ),
            pytest.param({"response": [{"save": {"jmespath": {"x": "{{ missing }}"}}}]}, 1, "'missing' is not defined", id="save-template"),
            pytest.param(
                {"response": [{"save": {"substitutions": [{"vars": {"x": "{{ missing }}"}}]}}]},
                1,
                "^Error processing substitutions: .*'missing' is not defined",
                id="save-substitution",
            ),
            # Rendered to template text, which its check cannot take.
            pytest.param({"response": [{"verify": {"status": "{{ template_text }}"}}]}, 1, r"^verify\.status must resolve to a status code", id="verify-rendered-template-text"),
            pytest.param(
                {"response": [{"save": {"regex": {"x": "{{ template_text }}"}}}]},
                1,
                r"^Error saving variable x: pattern must resolve to a regular expression",
                id="save-rendered-template-text",
            ),
            # A user function that crashed, or cannot be called, fails every
            # attempt alike: `validate --deep` reports the latter (HTTPCHAIN022).
            pytest.param({"response": [{"verify": {"user_functions": [f"{_HELPERS}:raises"]}}]}, 1, r"^Error calling user function .*: boom$", id="verify-function-crashed"),
            pytest.param({"response": [{"save": {"user_functions": [f"{_HELPERS}:raises"]}}]}, 1, r"^Error calling user function .*: boom$", id="save-function-crashed"),
            pytest.param(
                {"response": [{"verify": {"user_functions": [f"{_HELPERS}:is_done_typo"]}}]},
                1,
                r"^Error calling user function .*: Function 'is_done_typo' not found",
                id="verify-function-not-found",
            ),
            pytest.param(
                {"response": [{"save": {"user_functions": ["no_such_module_xyz:extract"]}}]},
                1,
                r"^Error calling user function .*: Failed to import module 'no_such_module_xyz'",
                id="save-module-not-importable",
            ),
            # The body schema cannot be read: `validate --deep` reports it (HTTPCHAIN020).
            pytest.param(
                {"response": [{"verify": {"body": {"schema": "no_such_schema.json"}}}]}, 1, r"^Error reading body schema file '.*no_such_schema\.json'", id="schema-file-missing"
            ),
            # A pytest outcome ends the stage wherever it is raised.
            pytest.param({"response": [{"verify": {"user_functions": [f"{_HELPERS}:fails"]}}]}, 1, "^custom check failed$", id="function-called-fail"),
        ],
    )
    def test_never_retried(self, monkeypatch, stage_fields, requests, message):
        waited = self._waits(monkeypatch)
        sent: list[httpx.Request] = []

        def respond(n: int, request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(503, json={})

        carrier = _make_carrier_subclass(global_context=ChainMap({"gone": None, "template_text": "{{ ( }}"}))
        with pytest.raises(pytest.fail.Exception, match=message) as excinfo:
            self._run({"retry": {"attempts": 3}, **stage_fields}, respond, carrier=carrier)
        assert "attempts)" not in str(excinfo.value)
        assert (len(sent), waited) == (requests, [])

    def test_skip_is_never_retried(self, monkeypatch):
        waited = self._waits(monkeypatch)
        with pytest.raises(pytest.skip.Exception):
            self._run({"retry": {"attempts": 3}, "response": [{"verify": {"user_functions": ["tests.unit.response_steps_test_helpers:skips"]}}]}, _job(done_at=1))
        assert waited == []

    def test_rate_limit_exhausted_is_not_retryable(self):
        """Another attempt would wait as long again for a slot. A minute's
        window: the slot taken stays taken, however slowly the test runs."""
        limiter = Limiter(Rate(1, Duration.MINUTE))
        try:
            assert limiter.try_acquire("api", blocking=False)
            with pytest.raises(RequestError, match="Rate limit exceeded") as excinfo:
                Carrier._execute_single_iteration(make_stage(), ChainMap(), {}, limiter=limiter, max_rate_limit_delay=0.05)
            assert excinfo.value.retryable is False
        finally:
            limiter.close()

    @pytest.mark.parametrize(
        ("function", "outcome", "message"),
        [
            pytest.param("fails_once_done", pytest.fail.Exception, r"^job vanished \(after 2 attempts\)$", id="fail"),
            # No failure: the reason is the function's, as it wrote it.
            pytest.param("skips_once_done", pytest.skip.Exception, r"^job moved elsewhere$", id="skip"),
            pytest.param("xfails_once_done", pytest.xfail.Exception, r"^known bug$", id="xfail"),
        ],
    )
    @pytest.mark.parametrize(("record_all", "recorded"), [pytest.param(True, [1, 2], id="har"), pytest.param(False, [2], id="no-har")])
    def test_user_function_ending_a_later_attempt(self, monkeypatch, function, outcome, message, record_all, recorded):
        """A function's pytest.fail(), skip() or xfail() on a later attempt
        ends the stage there, never retried, as on the first attempt. Every
        attempt's request went on the wire, and the HAR has them all: they
        were dropped, with the stage's only exchanges on the outcome's way out."""
        waited = self._waits(monkeypatch)
        cls = _make_carrier_subclass(record_all_exchanges=record_all)
        with pytest.raises((pytest.skip.Exception, pytest.fail.Exception), match=message) as excinfo:
            self._run({"retry": {"attempts": 5}, "response": [{"verify": {"user_functions": [f"{self._HELPERS}:{function}"]}}]}, _job(done_at=2), carrier=cls)
        assert excinfo.type is outcome
        assert len(waited) == 1
        assert [response.json()["n"] for _request, response, _started in cls.last_exchanges] == recorded

    def test_user_function_ending_a_later_attempt_then_an_exit_error(self, monkeypatch):
        """What the iteration's attempts entered raising on exit fails it
        after the outcome, which keeps its attempts' exchanges and count."""
        self._waits(monkeypatch)

        @contextmanager
        def lease():
            yield "l"
            raise RuntimeError("release failed")

        cls = _make_carrier_subclass(record_all_exchanges=True)
        exit_error = "Exiting the context manager from fixture 'lease' failed: RuntimeError: release failed"
        with pytest.raises(pytest.fail.Exception, match=rf"^job vanished \(after 2 attempts\)\n{re.escape(exit_error)}\n{re.escape(exit_error)}$"):
            self._run(
                {
                    "retry": {"attempts": 5},
                    "request": {"url": "http://mock/job?lease={{ lease() }}"},
                    "response": [{"verify": {"user_functions": [f"{self._HELPERS}:fails_once_done"]}}],
                },
                _job(done_at=2),
                carrier=cls,
                fixtures={"lease": lease},
            )
        assert [response.json()["n"] for _request, response, _started in cls.last_exchanges] == [1, 2]

    def test_user_function_ending_the_only_attempt_is_recorded(self):
        """Without retry too: the request the outcome answered is in the HAR."""
        cls = _make_carrier_subclass(record_all_exchanges=True)
        with pytest.raises(pytest.skip.Exception, match="^not on this server$"):
            self._run({"response": [{"verify": {"user_functions": [f"{self._HELPERS}:skips"]}}]}, _job(done_at=1), carrier=cls)
        assert [response.json()["n"] for _request, response, _started in cls.last_exchanges] == [1]

    def test_later_attempt_failing_otherwise_ends_the_iteration(self, monkeypatch):
        """A template failing on the second attempt ends the iteration there,
        a stage failure that counts the attempts made."""
        self._waits(monkeypatch)
        calls: list[int] = []

        def token() -> str:
            calls.append(1)
            if len(calls) > 1:
                raise RuntimeError("token expired")
            return "t"

        with pytest.raises(pytest.fail.Exception, match=r"token expired.* \(after 2 attempts\)$"):
            self._run(
                {"retry": {"attempts": 5}, "request": {"url": "http://mock/job?token={{ token() }}"}, "response": self._STATUS},
                lambda n, request: httpx.Response(503),
                fixtures={"token": token},
            )
        assert len(calls) == 2

    @pytest.mark.parametrize(
        ("message", "noted"),
        [
            pytest.param("Status code doesn't match: expected 200, got 503", "Status code doesn't match: expected 200, got 503 (after 3 attempts)", id="one-line"),
            # Before the colon of a count of failures, on the line a parallel stage's failure quotes.
            pytest.param("2 verification checks failed:\n  1. a\n  2. b", "2 verification checks failed (after 3 attempts):\n  1. a\n  2. b", id="several-failures"),
            pytest.param(
                "Body does not match schema: 'id' is required\n\nFailed validating",
                "Body does not match schema: 'id' is required (after 3 attempts)\n\nFailed validating",
                id="multi-line",
            ),
        ],
    )
    def test_after_attempts_message(self, message, noted):
        error = VerificationError(message)
        earlier = ((httpx.Request("GET", "http://mock/job"), None, None),)
        assert carrier_module._after_attempts(error, 3, earlier) is error
        assert (str(error), error.attempt, error.earlier_exchanges) == (noted, 3, earlier)

    def test_after_attempts_of_a_template_error(self):
        """Only a stage failure carries the attempt and the earlier exchanges."""
        cause = TemplatesError("'token' is not defined")
        error = carrier_module._after_attempts(cause, 2, ())
        assert (type(error), str(error), error.__cause__, error.attempt) == (StageExecutionError, "'token' is not defined (after 2 attempts)", cause, 2)

    def test_parallel_iterations_retry_each_on_its_own(self, monkeypatch):
        """Iteration i's job is done at its i-th attempt; a job never done
        fails its iteration once its attempts are spent, and the stage."""
        self._waits(monkeypatch)
        attempts: dict[str, int] = {}
        lock = threading.Lock()

        def respond(n: int, request: httpx.Request) -> httpx.Response:
            job = request.url.path.rsplit("/", 1)[-1]
            with lock:
                attempts[job] = attempts.get(job, 0) + 1
                done = job != "never" and attempts[job] >= int(job)
            return httpx.Response(200, json={"status": "done" if done else "pending"})

        stage_fields = {"retry": {"attempts": 3}, "request": {"url": "http://mock/job/{{ job }}"}, "response": [{"verify": {"jmespath": {"status": "done"}}}]}
        self._run({**stage_fields, "parallel": {"foreach": [{"individual": {"job": ["1", "2", "3"]}}], "max_concurrency": 3}}, respond)
        assert attempts == {"1": 1, "2": 2, "3": 3}

        attempts.clear()
        cls = _make_carrier_subclass()
        failed = r"""^Parallel execution failed at iteration 1: JMESPath 'status' doesn't match: expected "done", got "pending" \(after 3 attempts\)$"""
        with pytest.raises(pytest.fail.Exception, match=failed):
            self._run({**stage_fields, "parallel": {"foreach": [{"individual": {"job": ["1", "never"]}}], "max_concurrency": 2}}, respond, carrier=cls)
        assert attempts == {"1": 1, "never": 3}
        assert cls.last_shown_attempt == (3, 3)

    def test_wait_ends_when_cancelled(self):
        cancel = threading.Event()
        threading.Timer(0.05, cancel.set).start()
        start = time.monotonic()
        assert carrier_module._wait_to_retry(30, cancel) is False
        assert time.monotonic() - start < 5
        # Not cancelled: the wait runs its course.
        assert carrier_module._wait_to_retry(0.01, threading.Event()) is True
        assert carrier_module._wait_to_retry(0.01, None) is True

    @pytest.mark.parametrize(
        ("record_all", "har_paths"),
        [
            # Iteration 1's attempt went on the wire before it was cancelled,
            # so the HAR has it, ahead of the one that failed the stage.
            pytest.param(True, ["/job/1", "/job/0"], id="har"),
            pytest.param(False, ["/job/0"], id="no-har"),
        ],
    )
    def test_another_iteration_failing_interrupts_the_wait(self, monkeypatch, record_all, har_paths):
        """Iteration 1 waits 30 seconds to retry; iteration 0, answered only
        once that wait began, fails the stage, which cancels the wait: the
        stage fails at once with iteration 0's failure, not 30 seconds later.
        What iteration 1 sent before it was cancelled was dropped from the
        HAR with its failure."""
        waiting = threading.Event()
        interrupted: list[bool] = []
        wait = carrier_module._wait_to_retry

        def recorded(seconds: float, cancel: threading.Event | None) -> bool:
            waiting.set()
            done = wait(seconds, cancel)
            interrupted.append(not done)
            return done

        monkeypatch.setattr(carrier_module, "_wait_to_retry", recorded)

        def respond(n: int, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/job/0":
                assert waiting.wait(timeout=5)
                return httpx.Response(500, json={})
            return httpx.Response(200, text="<html>Queued</html>")

        cls = _make_carrier_subclass(record_all_exchanges=record_all)
        start = time.monotonic()
        with pytest.raises(pytest.fail.Exception, match=r"^Parallel execution failed at iteration 0: Status code doesn't match: expected 200, got 500$"):
            self._run(
                {
                    "parallel": {"foreach": [{"individual": {"n": [0, 1]}}], "max_concurrency": 2},
                    # A 500 is not retried, a body that is no JSON is.
                    "retry": {"attempts": 3, "delay": 30, "on": "save"},
                    "request": {"url": "http://mock/job/{{ n }}"},
                    "response": [{"verify": {"status": 200}}, {"save": {"jmespath": {"status": "status"}}}],
                },
                respond,
                carrier=cls,
            )
        assert time.monotonic() - start < 5
        assert interrupted == [True]
        assert [request.url.path for request, _response, _started in cls.last_exchanges] == har_paths

    @pytest.mark.parametrize(
        ("parallel", "retry", "slots"),
        [
            # A single iteration's only attempt needs no limiter...
            pytest.param({"repeat": 1, "calls_per_sec": 100}, None, 0, id="one-attempt"),
            # ...but when retried, each attempt takes a slot, the first too.
            pytest.param({"repeat": 1, "calls_per_sec": 100}, {"attempts": 3}, 3, id="one-iteration-retried"),
            pytest.param({"foreach": [{"individual": {"n": [0, 1]}}], "calls_per_sec": 100}, {"attempts": 3}, 6, id="each-iteration-retried"),
        ],
    )
    def test_each_attempt_takes_a_rate_limit_slot(self, monkeypatch, parallel, retry, slots):
        """Each iteration's job is done at its third attempt."""
        self._waits(monkeypatch)
        taken: list[int] = []
        attempts: dict[str, int] = {}
        lock = threading.Lock()

        def acquire(limiter: Limiter, timeout: float, cancel: threading.Event | None) -> bool:
            taken.append(1)
            return True

        def respond(n: int, request: httpx.Request) -> httpx.Response:
            with lock:
                attempts[request.url.path] = attempts.get(request.url.path, 0) + 1
                return httpx.Response(200 if attempts[request.url.path] == 3 else 503)

        cls = _make_carrier_subclass(_acquire_rate_slot=staticmethod(acquire))
        stage_fields = {"parallel": parallel, "request": {"url": "http://mock/job/{{ get('n', 0) }}"}, "response": self._STATUS}
        if retry is None:
            with pytest.raises(pytest.fail.Exception):
                self._run(stage_fields, respond, carrier=cls)
        else:
            self._run({**stage_fields, "retry": retry}, respond, carrier=cls)
        assert len(taken) == slots

    def test_context_managers_are_exited_when_the_iteration_ends(self, monkeypatch):
        """Each attempt that calls a factory fixture enters what it returns;
        all are exited once the last attempt is done, last entered first, not
        attempt by attempt."""
        self._waits(monkeypatch)
        events: list[str] = []

        @contextmanager
        def lease(n):
            events.append(f"enter {n}")
            yield n
            events.append(f"exit {n}")

        def respond(n: int, request: httpx.Request) -> httpx.Response:
            events.append(f"send {n}")
            return httpx.Response(200 if n == 3 else 503)

        leases = iter(range(1, 10))
        self._run(
            {"retry": {"attempts": 3}, "request": {"url": "http://mock/job?lease={{ lease(next_lease()) }}"}, "response": self._STATUS},
            respond,
            fixtures={"lease": lease, "next_lease": lambda: next(leases)},
        )
        assert events == ["enter 1", "send 1", "enter 2", "send 2", "enter 3", "send 3", "exit 3", "exit 2", "exit 1"]

    @pytest.mark.parametrize(("record_all", "recorded"), [(True, [1, 2, 3]), (False, [3])])
    def test_every_attempt_is_recorded_for_the_har(self, monkeypatch, record_all, recorded):
        """Real traffic, each attempt's: kept only when the HAR records every
        exchange, the last attempt's alone otherwise, as for any stage."""
        self._waits(monkeypatch)
        cls, _sent = self._run({"retry": {"attempts": 5}, "response": _POLL}, _job(done_at=3), carrier=_make_carrier_subclass(record_all_exchanges=record_all))
        assert [response.json()["n"] for _request, response, _started in cls.last_exchanges] == recorded

    @pytest.mark.parametrize(("record_all", "recorded"), [(True, [1, 2]), (False, [2])])
    def test_every_attempt_of_a_failed_stage_is_recorded(self, monkeypatch, record_all, recorded):
        self._waits(monkeypatch)
        cls = _make_carrier_subclass(record_all_exchanges=record_all)
        with pytest.raises(pytest.fail.Exception):
            self._run({"retry": {"attempts": 2}, "response": _POLL}, _job(done_at=3), carrier=cls)
        assert [response.json()["n"] for _request, response, _started in cls.last_exchanges] == recorded

    def test_settings_render_in_the_stage_scope(self, monkeypatch):
        """Against the stage's own substitutions, as the parallel config is."""
        waited = self._waits(monkeypatch)
        with pytest.raises(pytest.fail.Exception, match=r"\(after 3 attempts\)$"):
            self._run(
                {"substitutions": [{"vars": {"tries": 3, "pause": 0.25}}], "retry": {"attempts": "{{ tries }}", "delay": "{{ pause }}"}, "response": self._STATUS},
                lambda n, request: httpx.Response(503),
            )
        assert waited == [0.25, 0.25]

    @pytest.mark.parametrize(
        ("retry", "message"),
        [
            # Template text, which the template branch takes as it is.
            pytest.param({"attempts": "{{ value }}"}, r"^retry\.attempts must be a positive number, got '\{\{ x \}\}'$", id="attempts-template-text"),
            pytest.param({"attempts": 2, "delay": "{{ value }}"}, r"^retry\.delay must be a number of seconds, 0 or more, got '\{\{ x \}\}'$", id="delay-template-text"),
            pytest.param({"attempts": 2, "backoff": "{{ value }}"}, r"^retry\.backoff must be a number, 1 or more, got '\{\{ x \}\}'$", id="backoff-template-text"),
            pytest.param(
                {"attempts": 2, "max_delay": "{{ value }}"}, r"^retry\.max_delay must be a number of seconds, 0 or more, got '\{\{ x \}\}'$", id="max-delay-template-text"
            ),
        ],
    )
    def test_unusable_setting_fails_before_any_request(self, retry, message):
        sent: list[httpx.Request] = []
        carrier = _make_carrier_subclass(global_context=ChainMap({"value": "{{ x }}"}))
        with pytest.raises(pytest.fail.Exception, match=message):
            self._run({"retry": retry}, lambda n, request: sent.append(request) or httpx.Response(200), carrier=carrier)
        assert sent == []

    @pytest.mark.parametrize(
        ("retry", "value", "message"),
        [
            # An optional setting read as never declared: no cap at all.
            pytest.param(
                {"attempts": 2, "max_delay": "{{ value }}"},
                None,
                r"^'retry\.max_delay' was declared as '\{\{ value \}\}' but rendered to None, which would silently disable it$",
                id="max-delay-none",
            ),
            # Invalid there anyway: refused with the template named.
            pytest.param({"attempts": "{{ value }}"}, None, r"^'retry\.attempts' was declared as '\{\{ value \}\}' but rendered to None$", id="attempts-none"),
            pytest.param({"attempts": "{{ value }}"}, 0, r"validation errors? for RetryConfig\nattempts", id="attempts-zero"),
            # A flag where a number belongs: read as 1, it would attempt once
            # and fail on the first failure as if retry were not there.
            pytest.param(
                {"attempts": "{{ value }}"},
                True,
                r"validation error for RetryConfig\nattempts\n  Value error, A retry setting is a number or a template, got true",
                id="attempts-bool",
            ),
            pytest.param(
                {"attempts": 2, "delay": "{{ value }}"},
                False,
                r"validation error for RetryConfig\ndelay\n  Value error, A retry setting is a number or a template, got false",
                id="delay-bool",
            ),
            pytest.param({"attempts": 2, "backoff": "{{ value }}"}, 0.5, r"validation errors? for RetryConfig\nbackoff", id="backoff-below-one"),
            pytest.param(
                {"attempts": 2, "max_delay": "{{ value }}"},
                math.inf,
                r"validation errors? for RetryConfig\nmax_delay\.constrained-float\n  Input should be a finite number",
                id="max-delay-infinite",
            ),
        ],
    )
    def test_rendered_setting_the_stage_cannot_use(self, retry, value, message):
        sent: list[httpx.Request] = []
        with pytest.raises(pytest.fail.Exception, match=message):
            self._run({"retry": retry}, lambda n, request: sent.append(request) or httpx.Response(200), carrier=_make_carrier_subclass(global_context=ChainMap({"value": value})))
        assert sent == []

    def test_foreach_parameter_is_out_of_scope(self):
        """One schedule for the whole stage, decided before its iterations
        exist, as the validator's HTTPCHAIN003 at ``stages[i].retry`` says."""
        with pytest.raises(pytest.fail.Exception, match=r"'n' is not defined"):
            self._run({"parallel": {"foreach": [{"individual": {"n": [1, 2]}}]}, "retry": {"attempts": "{{ n }}"}}, lambda n, request: httpx.Response(200))


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
        ("status", "end", "straggler_end", "message"),
        [
            pytest.param(
                500,
                None,
                None,
                f"Parallel execution failed at iteration 0: Status code doesn't match: expected 200, got 500\n{_ITERATION_1_EXIT_ERROR}",
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
                    id=f"stage-failed-straggler-{name}",
                )
                for straggler_end, name in ((pytest.skip, "skipped"), (pytest.xfail, "xfailed"), (pytest.fail, "failed"))
            ),
            # A skip is no failure, so the straggler's error on exit fails the
            # stage, as it does a skipped stage's own.
            pytest.param(200, pytest.skip, None, _ITERATION_1_EXIT_ERROR, id="stage-skipped"),
        ],
    )
    @_EITHER_READ_ORDER
    def test_error_on_exit_of_a_straggler_is_reported(self, status, end, straggler_end, message, straggler_read_first):
        """An iteration still running when another one ends the stage exits its
        own when it ends, and what its exit raises is listed after the stage's
        own failure, labelled with the iteration: a commit that failed is a
        side effect to hear of. Both iterations are exited, the straggler last.

        Read before the stage's failure, while that iteration's exits still
        ran, the straggler failed on exit was taken for the stage's failure,
        and the failure that ended the stage was dropped.

        Both requests went on the wire, so the HAR has both, however the
        straggler ended and whether a failure or a skip ended the stage: the
        straggler's first, the one of the iteration that ended the stage
        last, for the report to show. A straggler that failed, or any
        iteration but the one a skip ended the stage with, was left out."""
        events: list[str] = []
        cls = _make_carrier_subclass(record_all_exchanges=True)
        # Both caught, so a wrong outcome fails this test rather than skipping it.
        with pytest.raises((pytest.skip.Exception, pytest.fail.Exception)) as excinfo:
            self._run_with_a_straggler(cls, events, status=status, end=end, straggler_end=straggler_end, straggler_read_first=straggler_read_first)
        assert excinfo.type is pytest.fail.Exception
        assert str(excinfo.value) == message
        assert sorted(events[:2]) == ["enter 0", "enter 1"]
        assert events[2:] == ["exit 0", "exit 1"]
        assert [str(request.url).removeprefix("http://mock") for request, _, _ in cls.last_exchanges] == ["/1?t=1", "/0?t=0"]

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
        straggler's failed verification included. Its request went on the
        wire, so the HAR has it, where it was left out with the failure."""
        events: list[str] = []
        cls = _make_carrier_subclass(record_all_exchanges=True)
        # Both caught, so a wrong outcome fails this test rather than skipping it.
        with pytest.raises((pytest.skip.Exception, pytest.fail.Exception)) as excinfo:
            self._run_with_a_straggler(
                cls,
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
        assert [str(request.url).removeprefix("http://mock") for request, _, _ in cls.last_exchanges] == ["/1?t=1", "/0?t=0"]

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
        ("aborted", "stage_fields", "reason"),
        [
            pytest.param(True, {"always_run": "{{ a() == 'never' }}"}, "Flow aborted", id="always-run"),
            # Evaluated after the stage's substitutions, which enter one too.
            pytest.param(False, {"substitutions": [{"vars": {"t": "{{ b() }}"}}], "skip_if": "{{ a() == 'a' }}"}, "skip_if: {{ a() == 'a' }}", id="skip-if"),
        ],
    )
    @pytest.mark.parametrize(
        ("exit_error", "outcome", "message"),
        [
            pytest.param(None, pytest.skip.Exception, None, id="skipped"),
            # A skipped stage has not failed, so the error on exit fails it.
            pytest.param(
                RuntimeError("rollback failed"),
                pytest.fail.Exception,
                "Exiting the context manager from fixture 'a' failed: RuntimeError: rollback failed",
                id="error-on-exit",
            ),
        ],
    )
    def test_exited_when_the_stage_skips(self, aborted, stage_fields, reason, exit_error, outcome, message):
        """An ``always_run`` or ``skip_if`` template can call a factory fixture
        and still skip the stage, which commits no saves either way."""
        events: list[str] = []
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        cls = _make_carrier_subclass(client=client, aborted=aborted)
        stage = Stage.model_validate({"name": "s", "request": {"url": "http://mock/"}, **stage_fields})
        try:
            # Both caught, so a wrong outcome fails this test rather than skipping it.
            with pytest.raises((pytest.skip.Exception, pytest.fail.Exception)) as excinfo:
                cls.execute_stage(stage, {"a": self._resource(events, "a", exit_error), "b": self._resource(events, "b")})
        finally:
            client.close()
        assert excinfo.type is outcome
        assert str(excinfo.value) == (message or reason)
        entered = ["enter b", "enter a", "exit a", "exit b"] if "skip_if" in stage_fields else ["enter a", "exit a"]
        assert events == entered
        assert cls.active_context_managers == []

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

    def test_unexpected_error_cancels_queued_iterations(self, monkeypatch):
        """One worker: iteration 0 fails with a plugin bug, and the iteration
        the worker takes next, if it gets to one before the stage's thread
        reacts, is held until that thread has shut the pool down. So exactly
        the iterations queued behind it are what the shutdown found, and none
        of them ran: drained instead of cancelled, all 40 would have.

        The hold is released by the shutdown itself, never by a clock: this
        asserted fewer than 20 of the 40 ran, and on a loaded machine the
        worker got through that many before the stage's thread was scheduled."""
        started: list[int] = []
        released: list[bool] = []
        shut_down = threading.Event()

        class Executor(carrier_module.ThreadPoolExecutor):
            def shutdown(self, wait=True, *, cancel_futures=False):
                if wait:
                    # About to join the worker: let the held iteration end,
                    # or a pool drained after all would hang, not fail.
                    shut_down.set()
                super().shutdown(wait=wait, cancel_futures=cancel_futures)
                # After cancelling: released earlier, the worker could take
                # another queued iteration before the shutdown dropped it.
                shut_down.set()

        def fake_iteration(cls, stage, local_context, iter_vars, limiter=None, max_rate_limit_delay=60, cancel=None):
            started.append(iter_vars["i"])
            if iter_vars["i"] == 0:
                raise RuntimeError("plugin bug")
            released.append(shut_down.wait(timeout=5))
            return IterationResult(saved_context={}, request=httpx.Request("GET", "http://mock/"), response=httpx.Response(200), started=datetime.now(UTC))

        monkeypatch.setattr(carrier_module, "ThreadPoolExecutor", Executor)
        cls = _make_carrier_subclass(_execute_single_iteration=classmethod(fake_iteration))
        config = ParallelRepeatConfig.model_validate({"repeat": 40, "max_concurrency": 1})

        with pytest.raises(RuntimeError, match="plugin bug"):
            cls._run_iterations(None, ChainMap(), [{"i": i} for i in range(40)], config, [])

        # Iteration 1 ran only where the worker took it before the shutdown.
        assert started in ([0], [0, 1])
        assert released == [True] * (len(started) - 1)

    def test_rate_slot_wait_interrupted_by_cancellation(self):
        # A minute's window: the drained bucket stays empty, however slowly
        # the test runs, so only the cancellation can end the wait.
        limiter = Limiter(Rate(1, Duration.MINUTE))
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
