import logging
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from pytest_httpchain.carrier import Carrier
from pytest_httpchain.constants import ConfigOptions
from pytest_httpchain.models import Scenario
from pytest_httpchain.plugin import (
    _CHAIN_KEY,
    _HAR_REDACTION,
    _HTTPX_LOG_FILTER,
    _REDACTION,
    _apply_xdist_group_nodeids,
    _chain_args,
    _earlier_hops,
    _format_section,
    _regroup_carrier_items,
    _sections_will_be_shown,
    _warn_on_params_varying_across_stages,
    pytest_collect_file,
    pytest_collection_modifyitems,
    pytest_runtest_makereport,
)
from pytest_httpchain.redaction import DEFAULT_REDACTION, NO_REDACTION


class TestPytestConfigure:
    """Configuration validation through REAL pytest machinery.

    ``pytester.parseconfigure`` builds a genuine Config from an ini file, so
    option registration, pytest's type="int" coercion (whose bare ValueError
    the plugin must wrap into a clean UsageError), and the range checks are
    exercised end to end instead of emulated on a mock."""

    def test_defaults_configure_cleanly(self, pytester):
        pytester.parseconfigure()

    @pytest.mark.parametrize(
        ("option", "value"),
        [
            pytest.param(ConfigOptions.SUFFIX, "mytest123", id="suffix-alphanumeric"),
            pytest.param(ConfigOptions.SUFFIX, "my_test", id="suffix-underscore"),
            pytest.param(ConfigOptions.SUFFIX, "my-test", id="suffix-hyphen"),
            pytest.param(ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH, 0, id="ref-depth-zero"),
            pytest.param(ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH, 10, id="ref-depth-positive"),
            pytest.param(ConfigOptions.MAX_COMPREHENSION_LENGTH, 1, id="max-comp-minimum"),
            pytest.param(ConfigOptions.MAX_COMPREHENSION_LENGTH, 1000000, id="max-comp-maximum"),
            pytest.param(ConfigOptions.MAX_PARALLEL_ITERATIONS, 1, id="max-parallel-minimum"),
            pytest.param(ConfigOptions.MAX_PARALLEL_ITERATIONS, 1000000, id="max-parallel-maximum"),
        ],
    )
    def test_valid_config(self, pytester, option, value):
        pytester.makeini(f"[pytest]\n{option} = {value}\n")
        assert pytester.parseconfigure().getini(option) == value

    def test_comprehension_cap_restored_on_unconfigure(self, pytester):
        """The cap is a process-wide simpleeval global: an in-process pytester
        run (or any nested session) must put back what it found, or its value
        leaks into the enclosing process's template engine (M-review)."""
        import simpleeval

        before = simpleeval.MAX_COMPREHENSION_LENGTH
        pytester.makeini(f"[pytest]\n{ConfigOptions.MAX_COMPREHENSION_LENGTH} = 7\n")
        config = pytester.parseconfigure()
        assert simpleeval.MAX_COMPREHENSION_LENGTH == 7

        config._ensure_unconfigure()
        assert simpleeval.MAX_COMPREHENSION_LENGTH == before

    @pytest.mark.parametrize(
        ("option", "value", "match"),
        [
            pytest.param(ConfigOptions.SUFFIX, "test.http", "suffix must contain only alphanumeric", id="suffix-special-chars"),
            pytest.param(ConfigOptions.SUFFIX, "test http", "suffix must contain only alphanumeric", id="suffix-spaces"),
            pytest.param(ConfigOptions.SUFFIX, "a" * 33, "suffix must contain only alphanumeric", id="suffix-too-long"),
            pytest.param(ConfigOptions.SUFFIX, "", "suffix must contain only alphanumeric", id="suffix-empty"),
            pytest.param(ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH, -1, "must be non-negative", id="ref-depth-negative"),
            pytest.param(ConfigOptions.MAX_COMPREHENSION_LENGTH, 0, "must be a positive integer", id="max-comp-zero"),
            pytest.param(ConfigOptions.MAX_COMPREHENSION_LENGTH, -1, "must be a positive integer", id="max-comp-negative"),
            pytest.param(ConfigOptions.MAX_COMPREHENSION_LENGTH, 1000001, "must not exceed 1,000,000", id="max-comp-too-large"),
            pytest.param(ConfigOptions.MAX_PARALLEL_ITERATIONS, 0, "must be a positive integer", id="max-parallel-zero"),
            pytest.param(ConfigOptions.MAX_PARALLEL_ITERATIONS, -1, "must be a positive integer", id="max-parallel-negative"),
            pytest.param(ConfigOptions.MAX_PARALLEL_ITERATIONS, 1000001, "must not exceed 1,000,000", id="max-parallel-too-large"),
            # A non-integer must surface as a clean UsageError rather than an
            # INTERNALERROR traceback, but the wording depends on who wraps it.
            # Through pytest 9 the coercion is a bare int(value) whose ValueError
            # the plugin catches and re-raises as "<option> must be an integer:
            # <ValueError>"; pytest 10 raises the UsageError itself and the
            # plugin's handler never runs, leaving the bare ValueError text. Both
            # embed int()'s own message, so match on that and let either wrapper
            # win — the type assertion below is what actually pins the behavior.
            pytest.param(ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH, "notanumber", "invalid literal for int", id="ref-depth-non-integer"),
            pytest.param(ConfigOptions.MAX_COMPREHENSION_LENGTH, "notanumber", "invalid literal for int", id="max-comp-non-integer"),
            pytest.param(ConfigOptions.MAX_PARALLEL_ITERATIONS, "notanumber", "invalid literal for int", id="max-parallel-non-integer"),
            # A header entry that can never match would leave that credential in
            # the report without a word.
            pytest.param(ConfigOptions.REDACT_HEADERS, "Authorization:", "'Authorization:' is not a header name", id="redact-headers-not-a-name"),
            pytest.param(ConfigOptions.REDACT_HEADERS, '"Authorization', "No closing quotation", id="redact-headers-unbalanced-quote"),
            pytest.param(ConfigOptions.REDACT_QUERY_PARAMS, "'token", "No closing quotation", id="redact-query-unbalanced-quote"),
            pytest.param(ConfigOptions.HAR_REDACT, "maybe", "httpchain_har_redact must be a boolean", id="har-redact-not-a-boolean"),
        ],
    )
    def test_invalid_config(self, pytester, option, value, match):
        pytester.makeini(f"[pytest]\n{option} = {value}\n")
        with pytest.raises(pytest.UsageError, match=match):
            pytester.parseconfigure()

    @pytest.mark.parametrize(
        ("option", "value", "match"),
        [
            pytest.param(ConfigOptions.REDACT_HEADERS, '["Authorization", 1]', "item at index 1 is int: 1", id="redact-headers-non-string-item"),
            pytest.param(ConfigOptions.REDACT_QUERY_PARAMS, '[["token"]]', r"item at index 0 is list: \['token'\]", id="redact-query-nested-list"),
        ],
    )
    def test_invalid_toml_list_item(self, pytester, option, value, match):
        """[tool.pytest.ini_options] hands a TOML list over unchecked (the
        native [tool.pytest] table has pytest check its items): a non-string
        item is a UsageError, not an INTERNALERROR from splitting it."""
        pytester.makepyprojecttoml(f"[tool.pytest.ini_options]\n{option} = {value}\n")
        with pytest.raises(pytest.UsageError, match=f"{option}: expects a list of strings, but {match}"):
            pytester.parseconfigure()


_DEFAULT_HEADERS = DEFAULT_REDACTION.headers
_DEFAULT_QUERY_PARAMS = DEFAULT_REDACTION.query_params


@pytest.mark.parametrize(
    ("ini", "headers", "query_params", "har_redacts"),
    [
        pytest.param("", _DEFAULT_HEADERS, _DEFAULT_QUERY_PARAMS, False, id="defaults"),
        # A list replaces the default rather than extending it.
        pytest.param("httpchain_redact_headers = X-Tenant-Secret", {"x-tenant-secret"}, _DEFAULT_QUERY_PARAMS, False, id="replaces-default"),
        pytest.param("httpchain_redact_headers = Authorization, Cookie", {"authorization", "cookie"}, _DEFAULT_QUERY_PARAMS, False, id="comma-separated"),
        pytest.param("httpchain_redact_query_params =\n    sig\n    Session_Id", _DEFAULT_HEADERS, {"sig", "session_id"}, False, id="one-per-line"),
        pytest.param("httpchain_redact_headers =\nhttpchain_redact_query_params =", set(), set(), False, id="empty-disables"),
        pytest.param("httpchain_har_redact = true", _DEFAULT_HEADERS, _DEFAULT_QUERY_PARAMS, True, id="har-redact"),
    ],
)
def test_redaction_rules_from_ini(pytester, ini, headers, query_params, har_redacts):
    """Read once at configure time: the report's rules, and the HAR's, which
    are the same rules under httpchain_har_redact and none otherwise."""
    pytester.makeini(f"[pytest]\n{ini}\n")
    config = pytester.parseconfigure()

    redaction = config.stash[_REDACTION]
    assert (redaction.headers, redaction.query_params) == (headers, query_params)
    assert config.stash[_HAR_REDACTION] is (redaction if har_redacts else NO_REDACTION)


def test_httpx_request_log_is_redacted_for_the_session(pytester):
    """httpx logs every request's URL at INFO, which a run capturing INFO
    prints with a failure's report: the session's rules apply to it, and the
    filter leaves with the session (an in-process run must not leave it behind)."""
    config = pytester.parseconfigure()
    httpx_logger = logging.getLogger("httpx")
    log_filter = config.stash[_HTTPX_LOG_FILTER]
    assert log_filter in httpx_logger.filters

    request_line = ("GET", httpx.URL("https://x.test/p?page=1&token=t"), "HTTP/1.1", 200, "OK")
    record = httpx_logger.makeRecord("httpx", logging.INFO, __file__, 1, 'HTTP Request: %s %s "%s %d %s"', request_line, None)
    unrelated = httpx_logger.makeRecord("httpx", logging.INFO, __file__, 1, "load_ssl_context verify=%r", (True,), None)
    assert log_filter.filter(record)
    assert log_filter.filter(unrelated)
    assert record.getMessage() == 'HTTP Request: GET https://x.test/p?page=1&token=[REDACTED] "HTTP/1.1 200 OK"'
    assert unrelated.getMessage() == "load_ssl_context verify=True"

    config._ensure_unconfigure()
    assert log_filter not in httpx_logger.filters


def _collect_parent(suffix: str) -> MagicMock:
    parent = MagicMock()
    parent.config.getini.return_value = suffix
    return parent


@pytest.mark.parametrize(
    ("suffix", "filename", "expected_name"),
    [
        ("http", "test_example.http.json", "example"),
        ("http", "test_my_api_test.http.json", "my_api_test"),
        ("api", "test_endpoint.api.json", "endpoint"),
        ("my-test", "test_example.my-test.json", "example"),
        # The suffix is matched literally, regex metacharacters included.
        ("v1.2", "test_example.v1.2.json", "example"),
    ],
)
def test_collect_file_matches(suffix, filename, expected_name):
    parent = _collect_parent(suffix)
    file_path = Path("/some/path") / filename
    with patch("pytest_httpchain.plugin.JsonModule") as json_module:
        assert pytest_collect_file(file_path, parent) is json_module.from_parent.return_value
    json_module.from_parent.assert_called_once_with(parent, path=file_path, name=expected_name)


@pytest.mark.parametrize(
    ("suffix", "filename"),
    [
        ("http", "test_example.api.json"),
        ("http", "example.http.json"),
        ("http", "test_example.http.yaml"),
        ("http", "test_example.json"),
        ("http", "test_example.py"),
        ("http", "test_.http.json"),
        # Unescaped, the '.' in the suffix would match any character.
        ("v1.2", "test_example.v1X2.json"),
    ],
)
def test_collect_file_ignores(suffix, filename):
    assert pytest_collect_file(Path("/some/path") / filename, _collect_parent(suffix)) is None


def test_regroup_pulls_each_scenario_together_and_keeps_other_items():
    """Each scenario class is emitted in stage order at its first item's
    position; a non-scenario test keeps its place between them."""

    class _A(Carrier):
        pass

    class _B(Carrier):
        pass

    class _Item:
        def __init__(self, cls: type | None, stage: int = 0):
            self.cls = cls
            self.function = SimpleNamespace(_httpchain_stage_index=stage)
            self.stash = pytest.Stash()

    a0, a1, b0, plain = _Item(_A, 0), _Item(_A, 1), _Item(_B, 0), _Item(None)
    items: list[Any] = [a1, plain, b0, a0]
    _regroup_carrier_items(items, {id(it): i for i, it in enumerate(items)})
    assert items == [a0, a1, plain, b0]


class _ParamItem:
    """A scenario item as the regroup reads its params: ``params`` maps each
    parametrized arg to ``(param index, scope)``, as ``item.callspec`` holds them."""

    def __init__(self, cls: type[Carrier], stage: int, **params: tuple[int, str]):
        self.cls = cls
        self.function = SimpleNamespace(_httpchain_stage_index=stage)
        self.stash = pytest.Stash()
        if params:
            self.callspec = SimpleNamespace(
                indices={name: index for name, (index, _) in params.items()},
                _arg2scope={name: SimpleNamespace(value=scope) for name, (_, scope) in params.items()},
            )


@pytest.mark.parametrize("scope", ["class", "module", "package", "session"])
def test_regroup_runs_each_high_scoped_param_as_its_own_chain(scope):
    """A fixture parametrized above function scope that every stage requests
    splits the scenario into one chain per param, each in stage order, the
    chains in the order they first appear. Sorting by stage alone ran every
    param's first stage before any second one. A function-scoped param (stage
    `parametrize`) varies in place inside each chain, in collection order."""

    class _Scenario(Carrier):
        pass

    def item(stage: int, tenant: int, v: int | None = None) -> _ParamItem:
        params = {"tenant": (tenant, scope)} | ({"v": (v, "function")} if v is not None else {})
        return _ParamItem(_Scenario, stage, **params)

    a0v0, a0v1, a1, b0v0, b0v1, b1 = item(0, 0, 0), item(0, 0, 1), item(1, 0), item(0, 1, 0), item(0, 1, 1), item(1, 1)
    collected = [a0v0, a0v1, b0v0, b0v1, a1, b1]
    items: list[Any] = [b1, b0v1, b0v0, a1, a0v1, a0v0]  # a sorter put tenant 1 first
    _regroup_carrier_items(items, {id(it): i for i, it in enumerate(collected)})
    assert items == [b0v0, b0v1, b1, a0v0, a0v1, a1]
    assert [it.stash[_CHAIN_KEY] for it in items] == [(("tenant", 1),)] * 3 + [(("tenant", 0),)] * 3


def test_regroup_varies_a_param_not_every_stage_requests_in_place():
    """Stages without the fixture belong to no single param, so it does not
    split the scenario: its instances stay in stage order, like stage
    `parametrize`, within the one chain."""

    class _Scenario(Carrier):
        pass

    create, per_tenant_a, per_tenant_b, after = (
        _ParamItem(_Scenario, 0),
        _ParamItem(_Scenario, 1, tenant=(0, "class")),
        _ParamItem(_Scenario, 1, tenant=(1, "class")),
        _ParamItem(_Scenario, 2),
    )
    collected = [create, per_tenant_a, per_tenant_b, after]
    items: list[Any] = [per_tenant_b, after, create, per_tenant_a]
    _regroup_carrier_items(items, {id(it): i for i, it in enumerate(collected)})
    assert items == collected
    assert [it.stash[_CHAIN_KEY] for it in items] == [()] * 4


def test_regroup_chains_do_not_depend_on_selection():
    """Selecting only the stages that request the fixture (``-k per_tenant``,
    or their node id) leaves no stage without it, but the scenario's chains
    are those of all its stages, as the class collector recorded them: the
    survivors still vary in place within one chain, rather than each param
    starting afresh only because of the selection."""

    class _Scenario(Carrier):
        pass

    create, per_tenant_a, per_tenant_b = (
        _ParamItem(_Scenario, 0),
        _ParamItem(_Scenario, 1, tenant=(0, "class")),
        _ParamItem(_Scenario, 1, tenant=(1, "class")),
    )
    collected = [create, per_tenant_a, per_tenant_b]
    scenario_chain_args = _chain_args(collected)
    assert scenario_chain_args == {_Scenario: frozenset()}
    items: list[Any] = [per_tenant_b, per_tenant_a]
    _regroup_carrier_items(items, {id(it): i for i, it in enumerate(collected)}, scenario_chain_args)
    assert items == [per_tenant_a, per_tenant_b]
    assert [it.stash[_CHAIN_KEY] for it in items] == [()] * 2


@pytest.mark.parametrize(
    ("requesting", "params", "warns"),
    [
        # create[a], create[b], read[a], read[b]: read[a] sees create[b]'s save,
        # and a class-scoped fixture is set up four times instead of twice.
        pytest.param({1, 2}, 2, True, id="several-stages"),
        # Its instances run back to back, each param set up once: nothing crosses.
        pytest.param({1}, 2, False, id="one-stage"),
        # Every stage requests it, so it splits the scenario into chains instead.
        pytest.param({0, 1, 2}, 2, False, id="every-stage"),
        # A single param has nothing to cross into.
        pytest.param({1, 2}, 1, False, id="one-param"),
    ],
)
def test_warns_when_fixture_params_vary_across_stages(requesting, params, warns):
    """A fixture parametrized above function scope that several stages, but
    not all, request varies in place across them, so each of those stages runs
    for every param before the next one does. Collection warns, naming the fix:
    request it from every stage."""
    scenario = Scenario.model_validate({"stages": [{"name": name, "request": {"url": "http://x"}} for name in ("login", "create", "read")]})
    cls = type("_Scenario", (Carrier,), {"scenario": scenario})
    items: list[Any] = [
        _ParamItem(cls, stage, tenant=(param, "class")) if stage in requesting else _ParamItem(cls, stage)
        for stage in range(3)
        for param in (range(params) if stage in requesting else [0])
    ]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _warn_on_params_varying_across_stages(cls, items, _chain_args(items)[cls])
    messages = [str(w.message) for w in caught]
    if not warns:
        assert messages == []
        return
    assert len(messages) == 1
    assert "the class-scoped fixture 'tenant' has params and is requested by stages ['create', 'read'] but not by every stage" in messages[0]
    assert "'read' for the first param sees what 'create' saved for the last" in messages[0]
    assert "Request 'tenant' from every stage" in messages[0]


def test_regroup_tiebreak_survives_xdist_nodeid_rewrite():
    """Up to pytest 9.1 an item hashes its ``_nodeid``, and an xdist loadgroup
    worker rewrites ``_nodeid`` between the hook's pre-yield recording of
    collection order and its post-yield regroup. Positions keyed by the item
    were then found or missed depending on each worker's hash seed, so workers
    ordered a parametrized stage's instances differently and xdist aborted with
    "Different tests were collected"."""

    class _Scenario(Carrier):
        pass

    class _Item:  # pytest <= 9.1: Node.__hash__ is hash(self._nodeid)
        def __init__(self, param: int):
            self.cls = _Scenario
            self.function = SimpleNamespace(_httpchain_stage_index=0)
            self.stash = pytest.Stash()
            self._nodeid = f"f.json::_Scenario::test 0 - s0[{param}]"

        def __hash__(self) -> int:
            return hash(self._nodeid)

    collected = [_Item(p) for p in range(20)]
    items: list[Any] = list(collected)
    hook = pytest_collection_modifyitems(SimpleNamespace(stash=pytest.Stash()), items)
    next(hook)
    items.reverse()  # a sorter (pytest-randomly, --ff, ...)
    for item in items:  # what an xdist loadgroup worker does
        item._nodeid = f"{item._nodeid}@f.json"
    with pytest.raises(StopIteration):
        next(hook)
    assert items == collected


def test_xdist_group_nodeid_carried_to_structured_id():
    """From pytest 9.2 ``nodeid`` is derived from a structured ``_id``, so the
    ``@<group>`` suffix an xdist loadgroup worker writes to ``_nodeid`` is carried
    over — for scenario items only, and only where xdist wrote one. Up to 9.1
    ``_nodeid`` is the slot ``nodeid`` reads, and nothing changes."""

    class _Scenario(Carrier):
        pass

    class _NodeId(str):
        @classmethod
        def parse(cls, nodeid: str) -> "_NodeId":
            return cls(nodeid)

    class _Item:  # pytest >= 9.2: nodeid reads _id, _nodeid is a plain attribute
        def __init__(self, cls: type | None, grouped: bool):
            self.cls = cls
            self._id = _NodeId(f"f.json::{cls.__name__ if cls else 'plain'}::t")
            if grouped:  # what an xdist loadgroup worker does
                self._nodeid = f"{self.nodeid}@f.json"

        @property
        def nodeid(self) -> str:
            return str(self._id)

    class _LegacyItem:  # pytest <= 9.1: _nodeid is a slot, there is no _id
        __slots__ = ("__dict__", "_nodeid", "cls")

        def __init__(self):
            self.cls = _Scenario
            self._nodeid = "f.json::_Scenario::t@f.json"

        @property
        def nodeid(self) -> str:
            return self._nodeid

    grouped, ungrouped, plain, legacy = _Item(_Scenario, True), _Item(_Scenario, False), _Item(None, True), _LegacyItem()
    items: list[Any] = [grouped, ungrouped, plain, legacy]
    _apply_xdist_group_nodeids(items)
    assert grouped.nodeid == "f.json::_Scenario::t@f.json"
    assert ungrouped.nodeid == "f.json::_Scenario::t"
    assert plain.nodeid == "f.json::plain::t"  # not a scenario: not the plugin's to touch
    assert legacy.nodeid == "f.json::_Scenario::t@f.json"
    assert not hasattr(legacy, "_id")


def test_format_section_reports_formatter_failure():
    """Reporting must never break the report: a formatter that raises becomes
    the section's text."""

    def broken(_exchange):
        raise ValueError("boom")

    assert _format_section("request", broken, object()) == "<Error formatting request: boom>"


def _hop(status: int, **headers: str) -> httpx.Response:
    return httpx.Response(status, headers=headers)


@pytest.mark.parametrize(
    ("history", "label"),
    [
        pytest.param([], "", id="none"),
        pytest.param([_hop(302, Location="/b")], " (after 1 redirect)", id="redirect"),
        pytest.param([_hop(301, Location="/b"), _hop(307, Location="/c")], " (after 2 redirects)", id="redirects"),
        # A digest challenge's 401 is in the same history, and is no redirect.
        pytest.param([_hop(401, **{"WWW-Authenticate": "Digest"})], " (after 1 auth exchange)", id="auth-exchange"),
        pytest.param([_hop(302, Location="/b"), _hop(401)], " (after 1 redirect and 1 auth exchange)", id="both"),
    ],
)
def test_earlier_hops_label(history, label):
    """The report shows the last response httpx got, and says what came before it."""
    assert _earlier_hops(history) == label


class TestReportSectionsBuiltOnlyWhenShown:
    """Formatting an exchange parses and re-serializes the whole body, and on a
    suite of thousands of passing stages nothing ever prints the result — but
    -rA/-rP do print it, so the guard must not cost those runs their output."""

    @pytest.mark.parametrize(
        ("args", "failed", "expected"),
        [
            pytest.param((), True, True, id="failed"),
            # Default reportchars ('fE') renders no PASSES block at all.
            pytest.param((), False, False, id="passed-default"),
            pytest.param(("-rA",), False, True, id="passed-rA"),
            pytest.param(("-rP",), False, True, id="passed-rP"),
            pytest.param(("-rfEP",), False, True, id="passed-rfEP"),
            # --xfail-tb renders the XFAILURES block, whose reports are 'skipped'.
            pytest.param(("--xfail-tb",), False, True, id="xfail-tb"),
        ],
    )
    def test_sections_will_be_shown(self, pytester, args, failed, expected):
        assert _sections_will_be_shown(pytester.parseconfigure(*args), MagicMock(failed=failed)) is expected

    def test_worker_without_terminal_reporter_formats_everything(self, pytester):
        """An xdist worker unregisters the terminal reporter and ships its
        sections to the controller, which does the rendering: the worker cannot
        tell what will be shown, so it must not drop anything."""
        config = pytester.parseconfigure()
        config.pluginmanager.unregister(name="terminalreporter")
        assert _sections_will_be_shown(config, MagicMock(failed=False))

    @staticmethod
    def _run_hook(config, *, failed: bool) -> list[tuple[str, str]]:
        """Drive the report hook over one recorded exchange, returning the
        sections it attached."""
        request = httpx.Request("GET", "https://example.com/")
        response = httpx.Response(200, json={"a": 1}, request=request)

        class _Scenario(Carrier):
            last_request = request
            last_response = response
            last_exchanges = [(request, response, None)]

        report = MagicMock(failed=failed, skipped=False, sections=[])
        # `cls` is reserved by Mock's own constructor, so it is set afterwards.
        item = MagicMock(config=config, nodeid="t::s")
        item.cls = _Scenario
        hook = pytest_runtest_makereport(item, MagicMock(when="call"))
        hook.send(None)
        with pytest.raises(StopIteration):
            hook.send(report)
        return report.sections

    @pytest.mark.parametrize(
        ("args", "failed", "expected"),
        [
            pytest.param((), False, [], id="passed"),
            pytest.param((), True, ["HTTP Request", "HTTP Response"], id="failed"),
            pytest.param(("-rA",), False, ["HTTP Request", "HTTP Response"], id="passed-with-rA"),
        ],
    )
    def test_hook_formats_only_when_the_sections_will_be_read(self, pytester, args, failed, expected):
        assert [title for title, _ in self._run_hook(pytester.parseconfigure(*args), failed=failed)] == expected

    def test_har_write_failure_is_logged_not_raised(self, pytester, tmp_path, caplog):
        not_a_dir = tmp_path / "file"
        not_a_dir.write_text("")
        config = pytester.parseconfigure("--httpchain-output-dir", str(not_a_dir))

        with caplog.at_level(logging.WARNING, logger="pytest_httpchain.plugin"):
            sections = self._run_hook(config, failed=True)

        assert [title for title, _ in sections] == ["HTTP Request", "HTTP Response"]
        assert [record.getMessage().split(": ", 1)[0] for record in caplog.records] == ["Failed to write HAR file for t::s"]
