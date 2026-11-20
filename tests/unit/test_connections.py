"""connections.py: a connection's sessions, readers, inbox and waits.

With an in-process adapter (`tests.unit.connections_test_helpers`), no
sockets: the round trip over TCP is tests/integration/test_connections.py,
and how the carrier runs a connection step is test_carrier.py's.
"""

import collections
import enum
import logging
import threading
import time
import types
from collections.abc import Iterator
from typing import Any
from unittest.mock import Mock

import pytest

import pytest_httpchain.connections as connections_module
from pytest_httpchain import ProtocolAdapter
from pytest_httpchain.connections import (
    ADAPTER_METHODS,
    DEBUG_INFO_LIMIT,
    LATE_WINDOW,
    Connections,
    ExchangeRecord,
    MessageValueError,
    Miss,
    ReaderEnd,
    adapter_error,
    checked_message,
    close_all,
    debug_text,
    declared_match,
    expect_failure,
    not_open_failure,
)
from pytest_httpchain.errors import RequestError, StageExecutionError, VerificationError
from pytest_httpchain.models import ConnectionExpect
from pytest_httpchain.redaction import NO_REDACTION
from tests.unit import connections_test_helpers as helpers
from tests.unit.helpers import DictAsList, IntAsStr, ListAsDict, Proxy, StrAsBytes, StrAsInt

ADAPTER = "tests.unit.connections_test_helpers:FakeAdapter"


@pytest.fixture
def registry() -> Iterator[Connections]:
    """A chain's registry, every session in it ended once the test is done."""
    chain = Connections()
    yield chain
    chain.close_all()
    assert not helpers.readers(), "a test left a reader running"


@pytest.fixture
def timed_session(monkeypatch):
    """A session driven synchronously, with a clock its wait can advance."""
    clock = types.SimpleNamespace(now=100.0)
    monkeypatch.setattr(connections_module, "time", types.SimpleNamespace(monotonic=lambda: clock.now))
    session = connections_module.Session("dev", ADAPTER, object(), 1, 2, "unused-reader")
    return session, clock


def _open(registry: Connections, peer_name: str, adapter: str = ADAPTER, inbox_limit: int = 1000, **params: Any):
    return registry.open("dev", adapter, {"peer": peer_name, **params}, 3.0, inbox_limit, label="UnitScenario")


def _wait_for(condition, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition never held"
        time.sleep(0.005)


def _queued(session, count: int) -> None:
    """Wait until the reader has appended ``count`` messages in all."""
    _wait_for(lambda: session.mark() >= count)


@pytest.fixture
def stuck_adapter(registry: Connections) -> Iterator[str]:
    """The import path of `connections_test_helpers.StuckAdapter`, whose
    readers stay stuck until the test is done: then they are released, and
    waited for to end. It requests `registry` so that pytest tears it down
    first: the registry's teardown asserts that no reader is left running."""
    helpers.STUCK_RELEASE.clear()
    yield "tests.unit.connections_test_helpers:StuckAdapter"
    helpers.STUCK_RELEASE.set()
    _wait_for(lambda: not helpers.readers())


def _is(expected: dict[str, Any]):
    """A predicate taking the messages equal to ``expected``."""
    return lambda message: None if message == expected else Miss(f"not {expected}")


class _Text(str):
    """A real subclass of str, as a message or an adapter may hold one."""


class _Raw(bytes): ...


class _Ratio(float): ...


class _Code(enum.IntEnum):
    READY = 1


class _State(enum.StrEnum):
    OPEN = "open"


class _Flags(enum.IntFlag):
    A = 1
    B = 2


class TestCheckedMessage:
    @pytest.mark.parametrize(
        ("value", "message"),
        [
            # The design's own example: the path of the value, and what to return.
            ({"body": {"items": (1, 2)}}, "receive() returned tuple at body.items; return a list"),
            ({"a": [1, {"b": {1, 2}}]}, "receive() returned set at a[1].b; return a list"),
            ({"raw": bytearray(b"x")}, "receive() returned bytearray at raw; return bytes"),
            ({"when": object()}, "receive() returned object at when; return a JSON value or bytes"),
            ({"a-b": {"c": ()}}, 'receive() returned tuple at ["a-b"].c; return a list'),
            ({1: "x"}, "receive() returned a key of type int at the top level; keys must be strings"),
            ({"a": {2: "x"}}, "receive() returned a key of type int at a; keys must be strings"),
            ({None: "x", 7: "also invalid"}, "receive() returned a key of type NoneType at the top level; keys must be strings"),
            ({"a": {None: object()}}, "receive() returned a key of type NoneType at a; keys must be strings"),
            # A value posing as one a message holds, as a proxy or a Mock(spec=...)
            # does, passes isinstance, and nothing that goes by its real type:
            # json.dumps, which a failure line writes a message with, refuses it.
            ({"token": Proxy(1234)}, "receive() returned Proxy posing as int at token; return a JSON value or bytes"),
            ({"token": Mock(spec=int)}, "receive() returned Mock posing as int at token; return a JSON value or bytes"),
            ({"a": [Proxy("s3cret")]}, "receive() returned Proxy posing as str at a[0]; return a JSON value or bytes"),
            ({"raw": Proxy(b"\x00")}, "receive() returned Proxy posing as bytes at raw; return a JSON value or bytes"),
            ({"meta": Proxy({"a": 1})}, "receive() returned Proxy posing as dict at meta; return a dict"),
            ({"items": Proxy([1])}, "receive() returned Proxy posing as list at items; return a list"),
            ({Proxy("k"): 1}, "receive() returned a key of type Proxy posing as str at the top level; keys must be strings"),
            (Proxy({"a": 1}), "receive() returned Proxy posing as dict; return a dict"),
            # A real JSON type whose __class__ names another: it passes a check of
            # its real type, and what reads the message after the gate goes by
            # isinstance, reading it as what it claims.
            ({"token": IntAsStr(4111)}, "receive() returned IntAsStr posing as str at token; return a JSON value or bytes"),
            ({"raw": StrAsBytes("s3cret")}, "receive() returned StrAsBytes posing as bytes at raw; return a JSON value or bytes"),
            ({"items": ListAsDict([1, 2])}, "receive() returned ListAsDict posing as dict at items; return a list"),
            ({StrAsInt("k"): 1}, "receive() returned a key of type StrAsInt posing as int at the top level; keys must be strings"),
            (DictAsList({"a": 1}), "receive() returned DictAsList posing as list; return a dict"),
            # A dict subclass's keys are checked as a dict's are.
            (collections.OrderedDict({1: "x"}), "receive() returned a key of type int at the top level; keys must be strings"),
            ([1, 2], "receive() returned list; return a dict"),
            ((1, 2), "receive() returned tuple; return a dict"),
            ("text", "receive() returned str; return a dict"),
        ],
        ids=[
            *("tuple", "set-in-list", "bytearray", "object", "quoted-key", "int-key", "nested-int-key", "none-before-int-key", "nested-none-key"),
            *("posing-int", "mock-int", "posing-str-in-list", "posing-bytes", "posing-dict", "posing-list", "posing-key", "posing-message"),
            *("int-as-str", "str-as-bytes", "list-as-dict", "key-str-as-int", "message-dict-as-list", "dict-subclass-int-key"),
            *("list", "top-tuple", "text"),
        ],
    )
    def test_refuses_what_no_message_holds(self, value, message):
        with pytest.raises(MessageValueError) as excinfo:
            checked_message(value, "receive()")
        assert str(excinfo.value) == message

    def test_first_violation_in_document_order(self):
        with pytest.raises(MessageValueError, match=r"at a; return a list"):
            checked_message({"a": (1,), "b": (2,)}, "receive()")

    def test_takes_json_values_and_bytes_at_any_depth(self):
        message = {"a": [None, True, 1, 1.5, "s", b"\x00", {"b": [b"\xff"]}]}
        assert checked_message(message, "connect()") is message

    def test_takes_a_subclass_of_what_a_message_holds(self):
        """A value is read by its real type, and a subclass of a JSON type or
        of bytes is one: an enum's member, a str or dict subclass."""

        class Ints(list): ...

        message = collections.OrderedDict(
            code=_Code.READY,
            state=_State.OPEN,
            flags=_Flags.A | _Flags.B,
            name=_Text("dev"),
            raw=_Raw(b"\x00"),
            ratio=_Ratio(0.5),
            items=Ints([1, 2]),
            nested={_Text("key"): collections.defaultdict(list)},
        )
        assert checked_message(message, "receive()") is message

    def test_deep_and_cyclic_messages_are_no_recursion_error(self):
        deep: dict[str, Any] = {}
        node = deep
        for _ in range(5000):
            node["n"] = node = {}
        checked_message(deep, "receive()")
        cyclic: dict[str, Any] = {"a": []}
        cyclic["a"].append(cyclic)
        checked_message(cyclic, "receive()")


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (ConnectionRefusedError("refused"), True),
        (TimeoutError("timed out"), True),
        # The other side closing, as receive() reports it: a connect() that
        # reads its greeting with receive() raises it for a handshake cut short.
        (EOFError("connection closed by peer"), True),
        (ValueError("bad frame"), False),
        (RequestError("not yet", retryable=True), True),
        (RequestError("never"), False),
    ],
    ids=["oserror", "timeout", "eof", "other", "opted-in", "not-opted-in"],
)
def test_adapter_error_is_retryable_for_transport_failures(error, retryable):
    failure = adapter_error("dev", "connect()", error)
    assert failure.retryable is retryable
    assert failure.__cause__ is error
    assert str(failure).startswith("Connection 'dev': connect() failed: ")


def test_adapter_methods_name_the_arguments_the_plugin_passes():
    """What `validate --deep` checks an adapter against, as the Protocol has it."""
    assert ADAPTER_METHODS == {"connect": ("timeout",), "send": ("message",), "receive": ("timeout",), "close": ()}
    assert all(callable(getattr(ProtocolAdapter, name)) for name in ADAPTER_METHODS)
    assert not hasattr(ProtocolAdapter, "debug_info")


class TestOpen:
    def test_connects_with_params_and_starts_a_named_daemon_reader(self, registry):
        peer = helpers.peer("open", greeting={"hello": "world"})
        session, connected = _open(registry, "open", host="h")
        assert connected == {"hello": "world"}
        assert (peer.current.params, peer.current.timeout) == ({"host": "h"}, 3.0)
        assert registry.is_open("dev")
        assert not registry.never_opened("dev")
        assert session.generation == 1
        assert helpers.readers() == ["httpchain-connection-UnitScenario-dev-1"]
        assert all(thread.daemon for thread in helpers.reader_threads())

    def test_reader_starts_in_an_empty_context(self, registry):
        peer = helpers.peer("context")
        token = helpers.PROBE.set("the test's")
        try:
            _open(registry, "context", adapter="tests.unit.connections_test_helpers:ContextAdapter")
        finally:
            helpers.PROBE.reset(token)
        _wait_for(lambda: hasattr(peer, "seen_probe"))
        assert peer.seen_probe == "unset"

    def test_half_open_connect_is_closed_and_never_registered(self, registry):
        peer = helpers.peer("refused", connect_error=ConnectionRefusedError("refused"))
        with pytest.raises(RequestError, match=r"connect\(\) failed: ConnectionRefusedError: refused") as excinfo:
            _open(registry, "refused")
        assert excinfo.value.retryable
        assert peer.closes == 1
        assert registry.never_opened("dev")
        assert registry.session("dev") is None
        assert not helpers.readers()

    def test_close_failing_after_a_failed_connect_is_logged(self, registry, caplog):
        helpers.peer("both", connect_error=OSError("down"), close_error=RuntimeError("cannot close"))
        with caplog.at_level(logging.ERROR, logger="pytest_httpchain.connections"), pytest.raises(RequestError, match="OSError: down"):
            _open(registry, "both")
        assert "after its connect() failed raised: RuntimeError: cannot close" in caplog.text

    def test_connect_result_is_checked_as_a_message(self, registry):
        peer = helpers.peer("bad greeting", greeting={"version": (1, 2)})
        with pytest.raises(RequestError, match=r"connect\(\) returned tuple at version; return a list") as excinfo:
            _open(registry, "bad greeting")
        assert not excinfo.value.retryable
        assert peer.closes == 1

    def test_constructor_error_is_a_request_failure(self, registry):
        helpers.peer("init", init_error=TypeError("unexpected keyword 'hots'"))
        with pytest.raises(RequestError, match=r"FakeAdapter\(\*\*params\) failed: TypeError") as excinfo:
            _open(registry, "init")
        assert not excinfo.value.retryable

    @pytest.mark.parametrize(
        ("adapter", "message"),
        [
            ("tests.unit.connections_test_helpers:Missing", "Adapter 'Missing' not found in module 'tests.unit.connections_test_helpers'"),
            ("tests.unit.connections_test_helpers:not_a_class", "'tests.unit.connections_test_helpers:not_a_class' is not a class"),
            ("no_such_module_xyz:Adapter", "Failed to import module 'no_such_module_xyz'"),
        ],
    )
    def test_adapter_that_does_not_import_fails_for_good(self, registry, adapter, message):
        helpers.peer("import")
        with pytest.raises(StageExecutionError, match=message) as excinfo:
            _open(registry, "import", adapter=adapter)
        assert not excinfo.value.retryable

    def test_reopen_is_a_fresh_session(self, registry):
        peer = helpers.peer("reopen")
        first, _ = _open(registry, "reopen")
        peer.push({"n": 1})
        _queued(first, 1)
        second, _ = _open(registry, "reopen")
        assert (first.generation, second.generation) == (1, 2)
        assert peer.adapters[0].closed
        assert not first.reader_alive
        assert second.mark() == 0, "nothing queued on the old session survives a reopen"
        assert helpers.readers() == ["httpchain-connection-UnitScenario-dev-2"]

    def test_failed_reopen_leaves_the_connection_failed(self, registry):
        peer = helpers.peer("flaky")
        _open(registry, "flaky")
        peer.connect_error = ConnectionResetError("reset")
        with pytest.raises(RequestError):
            _open(registry, "flaky")
        assert registry.not_open("dev") == "failed: ConnectionResetError: reset"

    def test_stuck_reader_bars_reopening(self, registry, stuck_adapter, monkeypatch, caplog):
        monkeypatch.setattr(connections_module, "JOIN_BUDGET", 0.05)
        helpers.peer("stuck")
        _open(registry, "stuck", adapter=stuck_adapter)
        with pytest.raises(RequestError, match="the previous reader for 'dev' did not stop") as excinfo:
            _open(registry, "stuck")
        assert not excinfo.value.retryable
        with pytest.raises(RequestError, match="cannot be opened again in this chain"):
            _open(registry, "stuck")


class TestCloseAndState:
    def test_close_records_the_stage(self, registry):
        peer = helpers.peer("close")
        _open(registry, "close")
        assert registry.close("dev", "bye") is None
        assert peer.closes == 1
        assert not helpers.readers()
        assert registry.not_open("dev") == "closed by stage 'bye'"
        assert not registry.is_open("dev")
        assert not registry.never_opened("dev")

    def test_close_error_is_returned_for_the_stage_to_fail_with(self, registry):
        helpers.peer("close error", close_error=RuntimeError("busy"))
        _open(registry, "close error")
        assert registry.close("dev", "bye") == "Closing connection 'dev' failed: RuntimeError: busy"

    def test_close_of_a_connection_never_opened_does_nothing(self, registry):
        assert registry.close("dev", "bye") is None
        assert registry.never_opened("dev")

    def test_not_open_failure_says_how_to_connect_again(self):
        failure = not_open_failure("status", "device", "closed by stage 'bye'")
        assert str(failure) == "Stage 'status': connection 'device' is not open (closed by stage 'bye'); set \"open\": true to connect again"
        assert not failure.retryable


class TestInbox:
    def test_drain_discards_what_is_queued(self, registry):
        peer = helpers.peer("drain")
        session, _ = _open(registry, "drain")
        peer.push({"n": 1}, {"n": 2})
        _queued(session, 2)
        assert session.drain() == 2
        outcome = session.expect(None, 0.05, after=None, since=time.monotonic())
        assert outcome.result == "timeout"

    def test_bounded_inbox_drops_the_oldest_and_counts_them(self, registry):
        peer = helpers.peer("bounded")
        session, _ = _open(registry, "bounded", inbox_limit=2)
        peer.push(*({"n": n} for n in range(5)))
        _queued(session, 5)
        assert session.dropped == 3
        taken = [session.expect(None, 1, after=None, since=0).taken for _ in range(2)]
        assert [arrival.message for arrival in taken if arrival is not None] == [{"n": 3}, {"n": 4}]

    def test_send_returns_what_the_adapter_returned(self, registry):
        peer = helpers.peer("send", send_return=lambda message: {"seq": len(peer.sent)})
        session, _ = _open(registry, "send")
        assert session.send({"op": "a"}) == {"seq": 1}
        assert peer.sent == [{"op": "a"}]

    def test_send_return_is_checked_as_a_message(self, registry):
        helpers.peer("send tuple", send_return={"ids": (1,)})
        session, _ = _open(registry, "send tuple")
        with pytest.raises(RequestError, match=r"send\(\) returned tuple at ids; return a list") as excinfo:
            session.send({})
        assert not excinfo.value.retryable

    def test_send_failure_is_retryable_for_a_socket_error(self, registry):
        helpers.peer("send error", send_error=BrokenPipeError("pipe"))
        session, _ = _open(registry, "send error")
        with pytest.raises(RequestError, match=r"send\(\) failed: BrokenPipeError: pipe") as excinfo:
            session.send({})
        assert excinfo.value.retryable


class TestExpect:
    @pytest.mark.parametrize("mode", ["message", "absent"])
    @pytest.mark.parametrize("arrival_offset", [-0.05, 0.05], ids=["in-time", "late"])
    def test_arrival_during_matching_is_checked_before_the_deadline_outcome(self, timed_session, mode, arrival_offset):
        session, clock = timed_session
        since = clock.now
        deadline = since + 1
        noise, wanted = {"kind": "noise"}, {"kind": "wanted"}
        session._append(noise)
        evaluated = []

        def matches(message):
            evaluated.append(message)
            if message == wanted:
                return None
            clock.now = deadline + arrival_offset
            session._append(wanted)
            clock.now = deadline + 0.1
            return Miss("noise")

        outcome = session.expect(matches, 1, after=None, since=since, mode=mode)
        if arrival_offset < 0:
            assert outcome.result == ("taken" if mode == "message" else "seen")
            assert outcome.taken is not None
            assert outcome.taken.message == wanted
            assert evaluated == [noise, wanted]
        else:
            assert outcome.result == ("timeout" if mode == "message" else "absent")
            assert outcome.taken is None
            assert evaluated == [noise]
        assert [arrival.message for arrival in outcome.considered] == [noise]

    @pytest.mark.parametrize("mode", ["message", "absent"])
    @pytest.mark.parametrize("error", [None, ConnectionResetError("reset during matching")], ids=["eof", "reset"])
    @pytest.mark.parametrize("end_offset", [-0.05, 0.05], ids=["in-time", "late"])
    def test_reader_end_during_matching_is_checked_before_the_deadline_outcome(self, timed_session, mode, error, end_offset):
        session, clock = timed_session
        since = clock.now
        deadline = since + 1
        session._append({"noise": True})
        evaluated = []

        def disconnected(timeout):
            raise error if error is not None else EOFError()

        session.adapter = types.SimpleNamespace(receive=disconnected)

        def rejects(message):
            evaluated.append(message)
            clock.now = deadline + end_offset
            session._read()
            clock.now = deadline + 0.1
            return Miss("noise")

        outcome = session.expect(rejects, 1, after=None, since=since, mode=mode)
        assert evaluated == [{"noise": True}]
        if end_offset < 0:
            assert outcome.result == "ended"
            assert outcome.end is not None
            assert outcome.end.error is error
            assert session.observed
        else:
            assert outcome.result == ("timeout" if mode == "message" else "absent")
            assert outcome.end is None
            assert not session.observed

    @pytest.mark.parametrize("after", [None, 0], ids=["whole-queue", "after-send"])
    def test_late_overflow_during_matching_is_not_counted_as_unseen(self, timed_session, after):
        session, clock = timed_session
        since = clock.now
        deadline = since + 1
        session._append({"noise": True})
        evaluated = []

        def rejects(message):
            evaluated.append(message)
            clock.now = deadline + 0.05
            for n in range(3):
                session._append({"late": n})
            clock.now = deadline + 0.1
            return Miss("noise")

        outcome = session.expect(rejects, 1, after=after, since=since, mode="absent")
        assert (outcome.result, outcome.unseen) == ("absent", 0)
        assert evaluated == [{"noise": True}]

    def test_continuous_late_arrivals_do_not_prolong_a_wait_after_matching(self, timed_session):
        session, clock = timed_session
        since = clock.now
        deadline = since + 1
        session._append({"noise": True})
        produced = []
        evaluated = []
        injecting = False

        class BusyCondition(threading.Condition):
            def __exit__(self, exc_type, exc_val, exc_tb):
                nonlocal injecting
                super().__exit__(exc_type, exc_val, exc_tb)
                if clock.now > deadline and not injecting:
                    assert len(produced) < 10, "late traffic kept the expired wait running"
                    produced.append({"late": len(produced)})
                    injecting = True
                    try:
                        session._append(produced[-1])
                    finally:
                        injecting = False

        session._condition = BusyCondition()

        def rejects(message):
            evaluated.append(message)
            clock.now = deadline + 0.1
            return Miss("noise")

        outcome = session.expect(rejects, 1, after=None, since=since, mode="absent")
        assert (outcome.result, outcome.unseen) == ("absent", 0)
        assert evaluated == [{"noise": True}]
        assert len(produced) >= 2

    @pytest.mark.parametrize("after", [None, 0], ids=["whole-queue", "after-send"])
    @pytest.mark.parametrize("on_time_count", [0, 3], ids=["only-late-drops", "in-time-and-late-drops"])
    def test_late_overflow_does_not_make_an_absent_wait_uncertain(self, timed_session, monkeypatch, after, on_time_count):
        session, clock = timed_session
        since = clock.now
        deadline = since + 1

        def delayed_wake(timeout):
            clock.now = deadline - 0.05
            for n in range(on_time_count):
                session._append({"n": n})
            clock.now = deadline + 0.05
            for n in range(3):
                session._append({"late": n})
            clock.now = deadline + 0.1

        monkeypatch.setattr(session._condition, "wait", delayed_wake)
        outcome = session.expect(None, 1, after=after, since=since, mode="absent")
        assert (outcome.result, outcome.unseen) == ("absent", on_time_count)
        failure = expect_failure(outcome, stage="quiet", connection="dev", match=None, timeout=1, mode="absent", sent=after is not None, reopens=False, ended_before=False)
        if on_time_count:
            assert isinstance(failure, VerificationError)
            assert "3 messages were dropped" in str(failure)
        else:
            assert failure is None

    @pytest.mark.parametrize("after", [None, 0], ids=["whole-queue", "after-send"])
    def test_deadline_includes_a_delay_acquiring_the_inbox_lock(self, timed_session, after):
        """The deadline and drop cutoff exist before the first lock acquisition,
        so scheduler delay there does not admit late matches or late losses."""
        session, clock = timed_session
        since = clock.now
        deadline = since + 1
        entering = threading.Event()

        class NotifyingCondition(threading.Condition):
            def __enter__(self):
                entering.set()
                return super().__enter__()

        session._condition = NotifyingCondition()
        outcomes = []
        worker = threading.Thread(target=lambda: outcomes.append(session.expect(None, 1, after=after, since=since, mode="absent")))
        with session._condition:
            entering.clear()
            worker.start()
            assert entering.wait(2), "the waiter never tried to acquire the inbox lock"
            clock.now = deadline + 0.05
            for n in range(3):
                session._append({"late": n})
            clock.now = deadline + 0.1
        worker.join(2)
        assert not worker.is_alive()
        outcome = outcomes[0]
        assert (outcome.result, outcome.unseen) == ("absent", 0)
        assert outcome.considered == ()
        assert session.drain() == 2

    @pytest.mark.parametrize(
        ("mode", "arrival_offset", "result"),
        [("message", -0.05, "taken"), ("message", 0.05, "timeout"), ("absent", -0.05, "seen"), ("absent", 0.05, "absent")],
        ids=["message-in-time", "message-late", "absent-in-time", "absent-late"],
    )
    def test_arrival_time_decides_a_wait_that_resumes_after_its_deadline(self, timed_session, monkeypatch, mode, arrival_offset, result):
        """A delayed wake neither accepts a late match nor overlooks an
        on-time one. Late matches remain available to subsequent stages."""
        session, clock = timed_session
        since = clock.now
        deadline = since + 1
        message = {"op": "reply"}

        def delayed_wake(timeout):
            clock.now = deadline + arrival_offset
            session._append(message)
            clock.now = deadline + 0.1

        monkeypatch.setattr(session._condition, "wait", delayed_wake)
        outcome = session.expect(None, 1, after=None, since=since, mode=mode)
        assert outcome.result == result
        assert outcome.considered == ()
        if arrival_offset < 0:
            assert outcome.taken is not None
            assert outcome.taken.message == message
        else:
            assert outcome.taken is None
            assert outcome.ended_at == deadline
            if mode == "message":
                # A delayed return must not move the late-report window.
                clock.now = deadline + LATE_WINDOW + 0.05
                session._append({"op": "outside the late window"})
                assert [arrival.message for arrival in session.late(outcome, since)] == [message]
            taken = session.expect(None, 1, after=None, since=since).taken
            assert taken is not None
            assert taken.message == message

    @pytest.mark.parametrize("mode", ["message", "absent", "closed"])
    @pytest.mark.parametrize("end_offset", [-0.05, 0.05], ids=["in-time", "late"])
    def test_reader_end_time_decides_a_wait_that_resumes_after_its_deadline(self, timed_session, monkeypatch, mode, end_offset):
        session, clock = timed_session
        since = clock.now
        deadline = since + 1

        def disconnected(timeout):
            raise EOFError

        session.adapter = types.SimpleNamespace(receive=disconnected)

        def delayed_wake(timeout):
            clock.now = deadline + end_offset
            session._read()
            clock.now = deadline + 0.1

        monkeypatch.setattr(session._condition, "wait", delayed_wake)
        outcome = session.expect(None, 1, after=None, since=since, mode=mode)
        if end_offset < 0:
            assert outcome.result == ("closed" if mode == "closed" else "ended")
            assert outcome.end is not None
            assert session.observed
        else:
            assert outcome.result == ("absent" if mode == "absent" else "timeout")
            assert outcome.end is None
            assert not session.observed
            assert session.expect(None, 1, after=None, since=since, mode="closed").result == "closed"

    def test_prequeued_message_is_available_to_a_zero_timeout_wait(self, timed_session):
        session, clock = timed_session
        message = {"queued": True}
        session._append(message)
        clock.now += 1
        outcome = session.expect(None, 0, after=None, since=clock.now)
        assert outcome.result == "taken"
        assert outcome.taken is not None
        assert outcome.taken.message == message

    def test_takes_the_oldest_match_and_leaves_the_rest_queued(self, registry):
        peer = helpers.peer("match")
        session, _ = _open(registry, "match")
        peer.push({"n": 1}, {"n": 2}, {"n": 2, "second": True})
        _queued(session, 3)
        outcome = session.expect(lambda message: None if message["n"] == 2 else Miss("n is not 2"), 1, after=None, since=0)
        assert outcome.result == "taken"
        assert outcome.taken is not None
        assert outcome.taken.message == {"n": 2}
        assert [(arrival.seq, arrival.miss) for arrival in outcome.considered] == [(1, Miss("n is not 2"))]
        rest = [session.expect(None, 1, after=None, since=0).taken for _ in range(2)]
        assert [arrival.message for arrival in rest if arrival is not None] == [{"n": 1}, {"n": 2, "second": True}]

    def test_after_a_send_only_later_arrivals_count(self, registry):
        """A late reply to an earlier attempt, queued before the send, cannot satisfy the expect."""
        peer = helpers.peer("after send", reply=lambda message: [{"reply_to": message["id"]}])
        session, _ = _open(registry, "after send")
        peer.push({"reply_to": 1})
        _queued(session, 1)
        mark = session.mark()
        session.send({"id": 2})
        outcome = session.expect(lambda message: None if "reply_to" in message else Miss("no reply"), 1, after=mark, since=time.monotonic())
        assert outcome.taken is not None
        assert outcome.taken.message == {"reply_to": 2}
        assert outcome.earlier == 1

    def test_arrival_waking_the_wait(self, registry):
        peer = helpers.peer("wake")
        session, _ = _open(registry, "wake")
        threading.Timer(0.1, peer.push, args=({"late": False},)).start()
        started = time.monotonic()
        outcome = session.expect(None, 5, after=None, since=started)
        assert outcome.result == "taken"
        assert time.monotonic() - started < 2
        assert outcome.taken is not None
        assert 0.05 < outcome.taken.at < 2

    def test_a_timeout_does_not_linger_for_late_messages(self, registry):
        """The wait ends at its deadline: every attempt of a polling stage
        waited the late window out too, for a report only the last one's
        failure shows."""
        helpers.peer("prompt")
        session, _ = _open(registry, "prompt")
        started = time.monotonic()
        outcome = session.expect(_is({"n": 9}), 0.05, after=None, since=started)
        assert outcome.result == "timeout"
        assert time.monotonic() - started < 0.05 + LATE_WINDOW / 2

    def test_late_messages_are_collected_for_the_failure_that_is_reported(self, registry):
        peer = helpers.peer("late")
        session, _ = _open(registry, "late")
        peer.push({"n": 0})
        since = time.monotonic()
        threading.Timer(0.15, peer.push, args=({"n": 1},)).start()
        record = ExchangeRecord("dev", ADAPTER, session=session, mode="message", since=since)
        record.outcome = outcome = session.expect(_is({"n": 9}), 0.1, after=None, since=since)
        assert outcome.result == "timeout"
        assert [arrival.message for arrival in outcome.considered] == [{"n": 0}]
        late = record.late()
        assert [arrival.message for arrival in late] == [{"n": 1}]
        # Left queued, and a copy: the report's is not the inbox's.
        assert late[0].message is not session.expect(None, 1, after=None, since=since).taken.message

    @pytest.mark.parametrize("mode", ["closed", "absent"])
    def test_only_a_wait_for_a_message_has_late_messages(self, registry, mode):
        peer = helpers.peer("no late")
        session, _ = _open(registry, "no late")
        record = ExchangeRecord("dev", ADAPTER, session=session, mode=mode)
        record.outcome = session.expect(None if mode == "closed" else _is({"n": 9}), 0.05, after=None, since=0, mode=mode)
        peer.push({"n": 1})
        started = time.monotonic()
        assert record.late() == ()
        assert time.monotonic() - started < LATE_WINDOW / 2

    def test_messages_dropped_during_a_wait_are_counted_as_unseen(self, registry):
        """The inbox_limit drops the oldest message even when no wait has
        looked at it: what arrived after the send and is gone by the next
        look is unseen, and may have matched."""
        peer = helpers.peer("unseen")
        session, _ = _open(registry, "unseen", inbox_limit=2)
        after = session.mark()
        peer.push(*({"n": n} for n in range(5)))
        _queued(session, 5)
        outcome = session.expect(_is({"n": 0}), 0.05, after=after, since=0, mode="absent")
        assert (outcome.result, outcome.unseen) == ("absent", 3)
        assert [arrival.message for arrival in outcome.considered] == [{"n": 3}, {"n": 4}]

    def test_a_timeout_says_what_was_dropped_unseen(self, registry):
        peer = helpers.peer("unseen timeout")
        session, _ = _open(registry, "unseen timeout", inbox_limit=1)
        after = session.mark()
        peer.push({"n": 1}, {"n": 2})
        _queued(session, 2)
        outcome = session.expect(_is({"n": 9}), 0.05, after=after, since=0)
        failure = expect_failure(outcome, stage="wait", connection="dev", match="n == `9`", timeout=0.05, mode="message", sent=True, reopens=False, ended_before=False)
        assert str(failure).endswith("(1 message arrived after the send); 1 message was dropped by the connection's inbox_limit before the stage could look at it")
        assert failure is not None
        assert failure.retryable

    def test_what_an_earlier_wait_took_is_not_unseen(self, registry):
        """A stage that did not send looks at the whole queue: a message an
        earlier stage took from it is no part of its wait."""
        peer = helpers.peer("seen")
        session, _ = _open(registry, "seen", inbox_limit=5)
        peer.push(*({"n": n} for n in range(5)))
        _queued(session, 5)
        assert session.expect(_is({"n": 3}), 1, after=None, since=0).result == "taken"
        outcome = session.expect(_is({"n": 9}), 0.05, after=None, since=0, mode="absent")
        assert (outcome.result, outcome.unseen) == ("absent", 0)
        assert len(outcome.considered) == 4

    def test_a_wait_over_the_whole_queue_counts_what_was_dropped_since_the_last_drain(self, registry):
        """Messages pile up while other stages run: those the inbox_limit
        dropped before a stage that sends nothing looked at the queue may
        have matched, as much as those dropped during its wait. Draining
        starts the count again: what was discarded then was meant to be."""
        peer = helpers.peer("piled")
        session, _ = _open(registry, "piled", inbox_limit=3)
        peer.push(*({"n": n} for n in range(5)))
        _queued(session, 5)
        outcome = session.expect(_is({"n": 0}), 0.05, after=None, since=0, mode="absent")
        assert (outcome.result, outcome.unseen) == ("absent", 2)
        failure = expect_failure(outcome, stage="quiet", connection="dev", match="n == `0`", timeout=0.05, mode="absent", sent=False, reopens=False, ended_before=False)
        assert isinstance(failure, VerificationError)
        assert not failure.retryable
        assert "2 messages were dropped by the connection's inbox_limit before the stage could look at them" in str(failure)

        assert session.drain() == 3
        peer.push({"n": 7})
        _queued(session, 6)
        outcome = session.expect(_is({"n": 0}), 0.05, after=None, since=0, mode="absent")
        assert (outcome.result, outcome.unseen) == ("absent", 0)

    def test_late_messages_are_those_that_arrived_within_the_window(self, registry):
        """Asked well after the deadline (the stage's context managers exit
        first), the late list is still what arrived within the window."""
        peer = helpers.peer("too late")
        session, _ = _open(registry, "too late")
        since = time.monotonic()
        record = ExchangeRecord("dev", ADAPTER, session=session, mode="message", since=since)
        record.outcome = session.expect(_is({"n": 9}), 0.02, after=None, since=since)
        time.sleep(LATE_WINDOW + 0.05)
        peer.push({"n": 1})
        _queued(session, 1)
        assert record.late() == ()

    def test_absent_passes_at_the_deadline(self, registry):
        peer = helpers.peer("absent")
        session, _ = _open(registry, "absent")
        peer.push({"n": 1})
        outcome = session.expect(_is({"n": 2}), 0.1, after=None, since=0, mode="absent")
        assert outcome.result == "absent"

    def test_absent_fails_on_the_first_match(self, registry):
        peer = helpers.peer("absent seen")
        session, _ = _open(registry, "absent seen")
        peer.push({"n": 2})
        outcome = session.expect(_is({"n": 2}), 5, after=None, since=0, mode="absent")
        assert outcome.result == "seen"
        failure = expect_failure(outcome, stage="quiet", connection="dev", match={"n": 2}, timeout=5, mode="absent", sent=False, reopens=False, ended_before=False)
        assert isinstance(failure, VerificationError)
        assert not failure.retryable
        assert 'matched {"n": 2} within 5 s, where none should: message #1' in str(failure)

    def test_absent_hit_quotes_the_message_redacted(self, registry):
        peer = helpers.peer("absent secret")
        session, _ = _open(registry, "absent secret")
        peer.push({"n": 2, "auth": {"Token": "s3cret"}})
        outcome = session.expect(None, 5, after=None, since=0, mode="absent")
        fields: dict[str, Any] = {"stage": "quiet", "connection": "dev", "match": None, "timeout": 5, "mode": "absent", "sent": False, "reopens": False, "ended_before": False}
        assert str(expect_failure(outcome, **fields)).endswith(': {"n": 2, "auth": {"Token": "[REDACTED]"}}')
        assert str(expect_failure(outcome, **fields, redaction=NO_REDACTION)).endswith(': {"n": 2, "auth": {"Token": "s3cret"}}')

    @pytest.mark.parametrize(
        ("error", "message"),
        [(None, {"error": None, "detail": None}), (ConnectionResetError("reset by peer"), {"error": "ConnectionResetError", "detail": "reset by peer"})],
        ids=["eof", "reset"],
    )
    def test_closed_passes_when_the_reader_ends(self, registry, error, message):
        peer = helpers.peer("closed")
        session, _ = _open(registry, "closed")
        peer.push({"ignored": True})
        threading.Timer(0.05, peer.end, args=(error,)).start()
        outcome = session.expect(None, 5, after=None, since=0, mode="closed")
        assert outcome.result == "closed"
        assert outcome.end is not None
        assert outcome.end.as_message() == message
        assert session.observed

    def test_closed_times_out_while_the_connection_stays(self, registry):
        helpers.peer("stays")
        session, _ = _open(registry, "stays")
        outcome = session.expect(None, 0.05, after=None, since=0, mode="closed")
        failure = expect_failure(outcome, stage="kicked", connection="dev", match=None, timeout=0.05, mode="closed", sent=False, reopens=False, ended_before=False)
        assert str(failure) == "Stage 'kicked': connection 'dev' did not close within 0.05 s"

    def test_reader_failure_wakes_the_waiter_at_once(self, registry):
        peer = helpers.peer("reset")
        session, _ = _open(registry, "reset")
        threading.Timer(0.05, peer.end, args=(ConnectionResetError("reset"),)).start()
        started = time.monotonic()
        outcome = session.expect(None, 30, after=None, since=started)
        assert time.monotonic() - started < 2, "the wait ended with the reader, not at its timeout"
        assert outcome.result == "ended"
        failure = expect_failure(outcome, stage="push", connection="dev", match="a", timeout=30, mode="message", sent=False, reopens=False, ended_before=False)
        assert isinstance(failure, RequestError)
        assert not failure.retryable
        assert str(failure) == "Stage 'push': connection 'dev' failed: ConnectionResetError: reset while the stage waited for \"a\""
        assert isinstance(failure.__cause__, ConnectionResetError)
        reopening = expect_failure(outcome, stage="push", connection="dev", match="a", timeout=30, mode="message", sent=False, reopens=True, ended_before=False)
        assert reopening is not None
        assert reopening.retryable

    def test_absent_fails_when_the_reader_ends(self, registry):
        """The connection ending is not "nothing arrived"."""
        peer = helpers.peer("absent end")
        session, _ = _open(registry, "absent end")
        threading.Timer(0.05, peer.end).start()
        outcome = session.expect(_is({"n": 1}), 5, after=None, since=0, mode="absent")
        assert outcome.result == "ended"
        failure = expect_failure(outcome, stage="quiet", connection="dev", match={"n": 1}, timeout=5, mode="absent", sent=False, reopens=False, ended_before=False)
        assert isinstance(failure, RequestError)
        assert not failure.retryable
        assert "closed by the other side while the stage waited for no message to match" in str(failure)

    def test_queued_messages_are_taken_before_the_end_is_reported(self, registry):
        peer = helpers.peer("last words")
        session, _ = _open(registry, "last words")
        peer.push({"op": "bye"})
        peer.end()
        _wait_for(lambda: session.end is not None)
        outcome = session.expect(_is({"op": "bye"}), 1, after=None, since=0)
        assert outcome.result == "taken"

    def test_ended_before_the_stage_is_the_connection_not_open(self, registry):
        peer = helpers.peer("gone")
        session, _ = _open(registry, "gone")
        peer.end(ConnectionResetError("reset"))
        _wait_for(lambda: session.end is not None)
        outcome = session.expect(None, 1, after=None, since=0)
        failure = expect_failure(outcome, stage="s", connection="dev", match=None, timeout=1, mode="message", sent=False, reopens=True, ended_before=True)
        assert str(failure) == "Stage 's': connection 'dev' is not open (failed: ConnectionResetError: reset); set \"open\": true to connect again"
        assert failure is not None
        assert not failure.retryable

    def test_receive_returning_no_message_fails_the_connection(self, registry):
        peer = helpers.peer("tuple")
        session, _ = _open(registry, "tuple")
        peer.push({"body": {"items": (1, 2)}})
        outcome = session.expect(None, 5, after=None, since=0)
        assert outcome.result == "ended"
        assert outcome.end is not None
        assert outcome.end.describe() == "failed: MessageValueError: receive() returned tuple at body.items; return a list"

    def test_timeout_message_counts_arrivals_and_names_the_first_evaluation_error(self, registry):
        peer = helpers.peer("timeout text")
        session, _ = _open(registry, "timeout text")
        peer.push({"a": 1}, {"a": 2})
        _queued(session, 2)
        mark = 0
        misses = iter([Miss("JMESPath 'a' doesn't match"), Miss("length() needs array", error=True)])
        outcome = session.expect(lambda message: next(misses), 0.05, after=mark, since=0)
        match = ConnectionExpect.model_validate({"match": {"op": "pong", "id": "{{ id }}"}}).match
        failure = expect_failure(outcome, stage="ping", connection="device", match=match, timeout=0.05, mode="message", sent=True, reopens=False, ended_before=False)
        assert str(failure) == (
            'Stage \'ping\': no message on connection \'device\' matched {"op": "pong", "id": "{{ id }}"} within 0.05 s '
            "(2 messages arrived after the send); the first that could not be evaluated: length() needs array"
        )
        assert failure is not None
        assert failure.retryable

    def test_predicate_error_ends_the_wait(self, registry):
        """The scenario's own error (an unusable operand) is raised as it is, at once."""
        peer = helpers.peer("unusable")
        session, _ = _open(registry, "unusable")
        peer.push({"a": 1})

        def unusable(message):
            raise VerificationError("expect.match: gt must resolve to a number", retryable=False)

        with pytest.raises(VerificationError, match="gt must resolve"):
            session.expect(unusable, 30, after=None, since=0)


def test_declared_match_shows_templates_as_written():
    match = ConnectionExpect.model_validate({"match": {"id": "{{ sent.seq }}", "n": {"gt": 1}}}).match
    assert declared_match(match) == '{"id": "{{ sent.seq }}", "n": {"gt": 1}}'
    assert declared_match("op == 'pong'") == "\"op == 'pong'\""
    assert declared_match(None) == "any message"


class TestDebugText:
    @pytest.mark.parametrize(
        ("debug", "text"),
        [
            ("raw: 00ff", "raw: 00ff"),
            (RuntimeError("no buffer"), "<debug_info() failed: RuntimeError: no buffer>"),
            (b"bytes", "<debug_info() returned bytes, not text>"),
            # Text only by isinstance: the report joins its lines with it, which
            # a proxy's real type refuses, and cutting a Mock raised in the
            # stage's outcome's place. A real subclass of str is text.
            (Proxy("raw: 00ff"), "<debug_info() returned Proxy posing as str, not text>"),
            (Mock(spec=str), "<debug_info() returned Mock posing as str, not text>"),
            (StrAsBytes("raw"), "<debug_info() returned StrAsBytes posing as bytes, not text>"),
            (_Text("raw: 00ff"), "raw: 00ff"),
            # Nothing to say, as an adapter without the method says nothing.
            (None, None),
        ],
        # Named here: pytest names a parametrized value by reading it, as
        # what passes isinstance for str, and a Mock(spec=str) is none to read.
        ids=["text", "raises", "bytes", "posing-text", "mock-text", "str-as-bytes", "text-subclass", "none"],
    )
    def test_guarded(self, debug, text):
        helpers.peer("debug", debug=debug)
        assert debug_text(helpers.FakeAdapter("debug")) == text

    def test_cut(self):
        helpers.peer("long", debug="x" * (DEBUG_INFO_LIMIT + 10))
        text = debug_text(helpers.FakeAdapter("long"))
        assert text is not None
        assert text.startswith("x" * DEBUG_INFO_LIMIT)
        assert text.endswith(f"... ({DEBUG_INFO_LIMIT + 10} characters)")

    def test_optional(self):
        helpers.peer("minimal")
        assert debug_text(helpers.MinimalAdapter("minimal")) is None


def test_exchange_snapshot_copies_what_it_shows(registry):
    peer = helpers.peer("snapshot", debug="state")
    session, _ = _open(registry, "snapshot")
    peer.push({"n": [1]})
    _queued(session, 1)
    record = ExchangeRecord("dev", ADAPTER, session=session, sent={"op": "x"}, match=None, timeout=1.0, mode="message")
    record.outcome = session.expect(None, 1, after=None, since=0)
    snapshot = record.freeze()
    assert record.outcome.taken is not None
    record.outcome.taken.message["n"].append(2)
    assert snapshot.taken is not None
    assert snapshot.taken.message == {"n": [1]}
    assert (snapshot.connection, snapshot.adapter, snapshot.generation, snapshot.result, snapshot.debug_info) == ("dev", ADAPTER, 1, "taken", "state")
    assert snapshot.session_age is not None
    assert snapshot.session_age >= 0
    with pytest.raises(AttributeError):
        snapshot.result = "timeout"  # ty: ignore[invalid-assignment]


def test_a_snapshot_nobody_reads_copies_nothing_and_calls_no_user_code(registry):
    """A stage that passed, whose section is not printed: its snapshot holds
    the messages as they are and does not ask the adapter's debug_info()."""
    peer = helpers.peer("light", debug=RuntimeError("debug_info() must not be called"))
    session, _ = _open(registry, "light")
    peer.push({"n": [1]})
    _queued(session, 1)
    sent = {"op": "x"}
    record = ExchangeRecord("dev", ADAPTER, session=session, sent=sent, match=None, timeout=1.0, mode="message")
    record.outcome = session.expect(None, 1, after=None, since=0)
    snapshot = record.freeze(full=False)
    assert snapshot.debug_info is None
    assert snapshot.sent is sent
    assert record.outcome.taken is not None
    assert snapshot.taken is record.outcome.taken
    assert (snapshot.generation, snapshot.result) == (1, "taken")


def test_exchange_snapshot_keeps_the_open_and_the_reader_end(registry):
    """What connect() returned, copied, and how the reader ended: as the wait
    saw it, or, without a wait, as the session has it when the snapshot is taken."""
    peer = helpers.peer("snapshot end", greeting={"hello": ["world"]})
    session, connected = _open(registry, "snapshot end")
    record = ExchangeRecord("dev", ADAPTER, session=session, opened=True, connected=connected)
    assert record.freeze().end is None
    peer.end(ConnectionResetError("reset"))
    _wait_for(lambda: session.end is not None)
    snapshot = record.freeze()
    assert connected is not None
    connected["hello"].append("again")
    assert (snapshot.opened, snapshot.connected) == (True, {"hello": ["world"]})
    assert snapshot.end is not None
    assert snapshot.end.describe() == "failed: ConnectionResetError: reset"


class TestCloseAll:
    def test_reader_end_after_stop_is_not_reported(self, caplog):
        """FakeAdapter's close() makes receive() raise OSError: the plugin's own doing."""
        helpers.peer("quiet end")
        chain = Connections()
        _open(chain, "quiet end")
        with caplog.at_level(logging.DEBUG, logger="pytest_httpchain.connections"):
            chain.close_all()
        assert not caplog.records
        assert not helpers.readers()

    @pytest.mark.parametrize("ending", ["close", "reopen"])
    def test_a_stage_ending_a_session_logs_the_failure_no_stage_saw(self, caplog, ending):
        """A reader that failed is reported where its session ends, whoever
        ends it: a stage closing the connection, or opening it again, dropped
        the failure without a word, which only the chain's end logged."""
        peer = helpers.peer("dropped")
        chain = Connections()
        session, _ = _open(chain, "dropped")
        peer.end(ValueError("bad frame"))
        _wait_for(lambda: session.end is not None)
        with caplog.at_level(logging.WARNING, logger="pytest_httpchain.connections"):
            if ending == "close":
                assert chain.close("dev", "bye") is None
            else:
                _open(chain, "dropped")
        assert "Connection 'dev' (open 1) failed and no stage saw it: ValueError: bad frame" in caplog.text
        chain.close_all()
        assert not helpers.readers()

    def test_a_failure_a_stage_saw_is_not_logged_at_its_close(self, caplog):
        peer = helpers.peer("observed")
        chain = Connections()
        session, _ = _open(chain, "observed")
        peer.end(ValueError("bad frame"))
        assert session.expect(None, 5, after=None, since=0).result == "ended"
        with caplog.at_level(logging.DEBUG, logger="pytest_httpchain.connections"):
            chain.close("dev", "bye")
        assert not caplog.records

    def test_unobserved_failure_is_logged(self, caplog):
        peer = helpers.peer("unseen")
        chain = Connections()
        session, _ = _open(chain, "unseen")
        peer.end(ConnectionResetError("reset"))
        _wait_for(lambda: session.end is not None)
        with caplog.at_level(logging.WARNING, logger="pytest_httpchain.connections"):
            chain.close_all()
        assert "Connection 'dev' (open 1) failed and no stage saw it: ConnectionResetError: reset" in caplog.text
        # A warning, not an error: often the peer's own end, a fixture serving
        # it torn down before the scenario's.
        assert [record.levelno for record in caplog.records] == [logging.WARNING]

    def test_close_errors_are_logged_never_warned(self, caplog):
        helpers.peer("close fails", close_error=RuntimeError("busy"))
        chain = Connections()
        _open(chain, "close fails")
        with caplog.at_level(logging.ERROR, logger="pytest_httpchain.connections"):
            chain.close_all()
        assert "Closing connection 'dev' at the end of the chain failed: RuntimeError: busy" in caplog.text

    def test_stuck_readers_share_one_deadline(self, stuck_adapter, monkeypatch, caplog):
        monkeypatch.setattr(connections_module, "TEARDOWN_BUDGET", 0.3)
        sessions = []
        for name in ("one", "two", "three"):
            chain = Connections()
            helpers.peer(name)
            sessions.append(chain.open(name, stuck_adapter, {"peer": name}, 1, 10, "Unit")[0])
        started = time.monotonic()
        with caplog.at_level(logging.ERROR, logger="pytest_httpchain.connections"):
            close_all(sessions)
        assert time.monotonic() - started < 0.8, "one shared deadline, not one per reader"
        assert caplog.text.count("did not stop within 0.3s of the chain's end") == 3

    def test_interrupt_is_raised_once_every_connection_is_closed(self):
        first, second, third = helpers.peer("first", close_error=KeyboardInterrupt()), helpers.peer("second", close_error=SystemExit()), helpers.peer("third")
        sessions = []
        for name in ("first", "second", "third"):
            sessions.append(Connections().open(name, ADAPTER, {"peer": name}, 1, 10, "Unit")[0])
        # The first interrupt, once every connection was closed.
        with pytest.raises(KeyboardInterrupt):
            close_all(sessions)
        assert (first.closes, second.closes, third.closes) == (1, 1, 1)
        _wait_for(lambda: not helpers.readers())

    @pytest.mark.parametrize("interrupted", ["join", "cleanup"])
    def test_interrupt_still_releases_every_sessions_resources(self, monkeypatch, interrupted):
        released = []
        sessions = []
        for name in ("first", "second"):
            helpers.peer(name)
            session, _ = Connections().open(name, ADAPTER, {"peer": name}, 1, 10, "Unit")
            session.set_cleanup(lambda name=name: released.append(name) or [])
            sessions.append(session)

        def stop():
            raise KeyboardInterrupt

        if interrupted == "join":
            join = sessions[0].join

            def interrupted_join(timeout):
                join(timeout)
                stop()

            monkeypatch.setattr(sessions[0], "join", interrupted_join)
        else:

            def interrupted_cleanup():
                released.append("first")
                stop()

            sessions[0].set_cleanup(interrupted_cleanup)
        with pytest.raises(KeyboardInterrupt):
            close_all(sessions)
        assert released == ["first", "second"]
        assert sessions[0].cleanup() == []
        _wait_for(lambda: not helpers.readers())


def test_reader_end_describes_itself():
    assert ReaderEnd(None).describe() == "closed by the other side"
    assert ReaderEnd(OSError("x")).describe() == "failed: OSError: x"


class _Unprintable(Exception):
    def __str__(self) -> str:
        raise RuntimeError("no text")


class TestGuards:
    """What an adapter or a message can do wrong that must not break a
    report, a failure line or the reader."""

    def test_exception_whose_str_raises(self):
        assert connections_module.describe_exception(_Unprintable()) == "_Unprintable: <exception str() failed>"
        assert ReaderEnd(_Unprintable()).as_message() == {"error": "_Unprintable", "detail": "<exception str() failed>"}

    def test_bytearray_is_refused_as_bytes(self):
        with pytest.raises(MessageValueError, match=r"receive\(\) returned bytearray at raw; return bytes"):
            checked_message({"raw": bytearray(b"x")}, "receive()")

    def test_mapping_that_is_no_dict_is_refused_as_one(self):
        with pytest.raises(MessageValueError, match=r"receive\(\) returned mappingproxy at meta; return a dict"):
            checked_message({"meta": types.MappingProxyType({})}, "receive()")

    @pytest.mark.parametrize(
        ("message", "shown"),
        [
            # Bytes as hex, the first 64 of them.
            ({"raw": b"\x00\xff"}, '{"raw": "0x00ff"}'),
            ({"raw": b"\x01" * 65}, '{"raw": "0x' + "01" * 64 + '..."}'),
            # A value JSON has no form for, by its repr.
            ({"when": 1j}, '{"when": "1j"}'),
        ],
    )
    def test_shown_message(self, message, shown):
        assert connections_module.shown_message(message) == shown

    def test_shown_message_cut_and_unshowable(self):
        long = connections_module.shown_message({"text": "x" * 300})
        assert long.endswith("... (312 characters)")
        assert len(long) < 300
        cyclic: dict[str, Any] = {}
        cyclic["self"] = cyclic
        assert connections_module.shown_message(cyclic) == "(a message too deep or too long to show)"

    def test_debug_info_interrupt_propagates(self):
        helpers.peer("interrupted debug", debug=KeyboardInterrupt())
        with pytest.raises(KeyboardInterrupt):
            debug_text(helpers.FakeAdapter("interrupted debug"))

    def test_uncopyable_value_is_kept_as_it_is(self):
        generator = (n for n in ())
        message = {"g": generator}
        assert connections_module._copied(message) is message

    def test_unhandled_expect_result_is_a_plugin_bug(self):
        outcome = connections_module.ExpectOutcome("bogus", waited=0)
        with pytest.raises(RuntimeError, match="Unhandled expect result: bogus"):
            expect_failure(outcome, stage="s", connection="dev", match=None, timeout=1, mode="message", sent=False, reopens=False, ended_before=False)


class TestReaderGuards:
    def test_receive_returning_at_once_does_not_spin(self, registry):
        peer = helpers.peer("impatient")
        session, _ = _open(registry, "impatient", adapter="tests.unit.connections_test_helpers:ImpatientAdapter")
        time.sleep(0.2)
        # Without the pause it would call receive() thousands of times.
        assert 1 <= peer.receives < 100
        assert session.end is None

    def test_anything_the_reader_raises_ends_the_session(self, registry, monkeypatch):
        def broken(value, source):
            raise RuntimeError("checker broke")

        monkeypatch.setattr(connections_module, "checked_message", broken)
        peer = helpers.peer("broken check")
        session, _ = _open(registry, "broken check")
        peer.push({"n": 1})
        _wait_for(lambda: session.end is not None)
        assert session.end is not None
        assert session.end.describe() == "failed: RuntimeError: checker broke"

    def test_removing_a_message_already_gone_does_nothing(self, registry):
        peer = helpers.peer("gone")
        session, _ = _open(registry, "gone")
        peer.push({"n": 1})
        _queued(session, 1)
        session._remove(99)
        assert session.drain() == 1


class TestOpenAndCloseGuards:
    def test_reopen_logs_a_failing_close_and_opens(self, registry, caplog):
        peer = helpers.peer("close fails on reopen", close_error=RuntimeError("busy"))
        _open(registry, "close fails on reopen")
        with caplog.at_level(logging.ERROR, logger="pytest_httpchain.connections"):
            session, _ = _open(registry, "close fails on reopen")
        assert "Closing connection 'dev' to reopen it failed: RuntimeError: busy" in caplog.text
        assert session.generation == 2
        peer.close_error = None

    @pytest.mark.parametrize(
        ("connect_error", "close_error"),
        [
            # An interrupt from connect() is no adapter failure: it propagates,
            # the half-open adapter closed.
            (KeyboardInterrupt(), None),
            # And one from that close() too.
            (OSError("refused"), KeyboardInterrupt()),
        ],
    )
    def test_interrupt_while_connecting_propagates(self, registry, connect_error, close_error):
        peer = helpers.peer("interrupted connect", connect_error=connect_error, close_error=close_error)
        with pytest.raises(KeyboardInterrupt):
            _open(registry, "interrupted connect")
        assert peer.closes == 1
        assert registry.session("dev") is None

    @pytest.mark.parametrize("call", ["init", "connect"])
    def test_pytest_exit_while_opening_propagates(self, registry, call):
        """pytest.exit()'s Exit is an Exception, which building or connecting
        the adapter took for its failure (`adapter_error`): a RequestError
        failed one stage where the adapter meant to stop the run."""
        stop = pytest.exit.Exception(f"{call} says stop")
        helpers.peer("exits", **{f"{call}_error": stop})
        with pytest.raises(pytest.exit.Exception) as excinfo:
            _open(registry, "exits")
        assert excinfo.value is stop
        assert registry.session("dev") is None

    def test_pytest_exit_from_send_propagates(self, registry):
        """As from the adapter's other calls (above), and its send() alike."""
        stop = pytest.exit.Exception("send says stop")
        helpers.peer("exits", send_error=stop)
        session, _ = _open(registry, "exits")
        with pytest.raises(pytest.exit.Exception) as excinfo:
            session.send({})
        assert excinfo.value is stop

    def test_close_interrupt_propagates_the_reader_stopped(self, registry, caplog):
        helpers.peer("interrupted close", close_error=KeyboardInterrupt())
        session, _ = _open(registry, "interrupted close")
        released = []
        session.set_cleanup(lambda: released.append("lease") or ["lease release failed"])
        with pytest.raises(KeyboardInterrupt):
            registry.close("dev", "bye")
        assert released == ["lease"]
        assert "lease release failed" in caplog.text
        _wait_for(lambda: not helpers.readers())
        assert registry.not_open("dev") == "closed by stage 'bye'"

    def test_closing_twice_does_nothing_the_second_time(self, registry):
        helpers.peer("twice")
        _open(registry, "twice")
        assert registry.close("dev", "bye") is None
        assert registry.close("dev", "again") is None
        assert registry.not_open("dev") == "closed by stage 'again'"

    def test_close_with_a_stuck_reader_bars_reopening(self, registry, stuck_adapter, monkeypatch, caplog):
        monkeypatch.setattr(connections_module, "JOIN_BUDGET", 0.1)
        helpers.peer("stuck on close")
        registry.open("dev", stuck_adapter, {"peer": "stuck on close"}, 1, 10, "Unit")
        with caplog.at_level(logging.ERROR, logger="pytest_httpchain.connections"):
            assert registry.close("dev", "bye") is None
        assert "The reader of connection 'dev' did not stop within 0.1s of close()" in caplog.text
        with pytest.raises(RequestError, match="the previous reader for 'dev' did not stop, so it cannot be opened again"):
            _open(registry, "stuck on close")
