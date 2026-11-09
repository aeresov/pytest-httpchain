"""connections.py: a connection's sessions, readers, inbox and waits.

With an in-process adapter (`tests.unit.connections_test_helpers`), no
sockets: the round trip over TCP is tests/integration/test_connections.py,
and how the carrier runs a connection step is test_carrier.py's.
"""

import logging
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

import pytest_httpchain.connections as connections_module
from pytest_httpchain import ProtocolAdapter
from pytest_httpchain.connections import (
    ADAPTER_METHODS,
    DEBUG_INFO_LIMIT,
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
from tests.unit import connections_test_helpers as helpers

ADAPTER = "tests.unit.connections_test_helpers:FakeAdapter"


def _readers() -> list[str]:
    return [thread.name for thread in threading.enumerate() if thread.name.startswith("httpchain-connection-")]


@pytest.fixture
def registry() -> Iterator[Connections]:
    """A chain's registry, every session in it ended once the test is done."""
    chain = Connections()
    yield chain
    chain.close_all()
    assert not _readers(), "a test left a reader running"


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


def _is(expected: dict[str, Any]):
    """A predicate taking the messages equal to ``expected``."""
    return lambda message: None if message == expected else Miss(f"not {expected}")


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
            ([1, 2], "receive() returned list; return a dict"),
            ((1, 2), "receive() returned tuple; return a dict"),
            ("text", "receive() returned str; return a dict"),
        ],
        ids=["tuple", "set-in-list", "bytearray", "object", "quoted-key", "int-key", "nested-int-key", "list", "top-tuple", "text"],
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
        (ValueError("bad frame"), False),
        (RequestError("not yet", retryable=True), True),
        (RequestError("never"), False),
    ],
    ids=["oserror", "timeout", "other", "opted-in", "not-opted-in"],
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
        assert _readers() == ["httpchain-connection-UnitScenario-dev-1"]
        assert all(thread.daemon for thread in threading.enumerate() if thread.name.startswith("httpchain-connection-"))

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
        assert not _readers()

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
        assert _readers() == ["httpchain-connection-UnitScenario-dev-2"]

    def test_failed_reopen_leaves_the_connection_failed(self, registry):
        peer = helpers.peer("flaky")
        _open(registry, "flaky")
        peer.connect_error = ConnectionResetError("reset")
        with pytest.raises(RequestError):
            _open(registry, "flaky")
        assert registry.not_open("dev") == "failed: ConnectionResetError: reset"

    def test_stuck_reader_bars_reopening(self, registry, monkeypatch, caplog):
        monkeypatch.setattr(connections_module, "JOIN_BUDGET", 0.05)
        helpers.peer("stuck")
        helpers.STUCK_RELEASE.clear()
        try:
            _open(registry, "stuck", adapter="tests.unit.connections_test_helpers:StuckAdapter")
            with pytest.raises(RequestError, match="the previous reader for 'dev' did not stop") as excinfo:
                _open(registry, "stuck")
            assert not excinfo.value.retryable
            with pytest.raises(RequestError, match="cannot be opened again in this chain"):
                _open(registry, "stuck")
        finally:
            helpers.STUCK_RELEASE.set()
        _wait_for(lambda: not _readers())


class TestCloseAndState:
    def test_close_records_the_stage(self, registry):
        peer = helpers.peer("close")
        _open(registry, "close")
        assert registry.close("dev", "bye") is None
        assert peer.closes == 1
        assert not _readers()
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

    def test_timeout_keeps_the_late_messages_for_the_report(self, registry):
        peer = helpers.peer("late")
        session, _ = _open(registry, "late")
        peer.push({"n": 0})
        threading.Timer(0.15, peer.push, args=({"n": 1},)).start()
        outcome = session.expect(_is({"n": 9}), 0.1, after=None, since=time.monotonic())
        assert outcome.result == "timeout"
        assert [arrival.message for arrival in outcome.considered] == [{"n": 0}]
        assert [arrival.message for arrival in outcome.late] == [{"n": 1}]
        assert 0.09 < outcome.waited < 1

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
        ],
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


class TestCloseAll:
    def test_reader_end_after_stop_is_not_reported(self, caplog):
        """FakeAdapter's close() makes receive() raise OSError: the plugin's own doing."""
        helpers.peer("quiet end")
        chain = Connections()
        _open(chain, "quiet end")
        with caplog.at_level(logging.DEBUG, logger="pytest_httpchain.connections"):
            chain.close_all()
        assert not caplog.records
        assert not _readers()

    def test_unobserved_failure_is_logged(self, caplog):
        peer = helpers.peer("unseen")
        chain = Connections()
        session, _ = _open(chain, "unseen")
        peer.end(ConnectionResetError("reset"))
        _wait_for(lambda: session.end is not None)
        with caplog.at_level(logging.ERROR, logger="pytest_httpchain.connections"):
            chain.close_all()
        assert "Connection 'dev' (open 1) failed and no stage saw it: ConnectionResetError: reset" in caplog.text

    def test_close_errors_are_logged_never_warned(self, caplog):
        helpers.peer("close fails", close_error=RuntimeError("busy"))
        chain = Connections()
        _open(chain, "close fails")
        with caplog.at_level(logging.ERROR, logger="pytest_httpchain.connections"):
            chain.close_all()
        assert "Closing connection 'dev' at the end of the chain failed: RuntimeError: busy" in caplog.text

    def test_stuck_readers_share_one_deadline(self, monkeypatch, caplog):
        monkeypatch.setattr(connections_module, "TEARDOWN_BUDGET", 0.3)
        helpers.STUCK_RELEASE.clear()
        sessions = []
        try:
            for name in ("one", "two", "three"):
                chain = Connections()
                helpers.peer(name)
                sessions.append(chain.open(name, "tests.unit.connections_test_helpers:StuckAdapter", {"peer": name}, 1, 10, "Unit")[0])
            started = time.monotonic()
            with caplog.at_level(logging.ERROR, logger="pytest_httpchain.connections"):
                close_all(sessions)
            assert time.monotonic() - started < 0.8, "one shared deadline, not one per reader"
            assert caplog.text.count("did not stop within 0.3s of the chain's end") == 3
        finally:
            helpers.STUCK_RELEASE.set()
        _wait_for(lambda: not _readers())

    def test_interrupt_is_raised_once_every_connection_is_closed(self):
        first, second = helpers.peer("first", close_error=KeyboardInterrupt()), helpers.peer("second")
        sessions = []
        for name in ("first", "second"):
            sessions.append(Connections().open(name, ADAPTER, {"peer": name}, 1, 10, "Unit")[0])
        with pytest.raises(KeyboardInterrupt):
            close_all(sessions)
        assert (first.closes, second.closes) == (1, 1)
        _wait_for(lambda: not _readers())


def test_reader_end_describes_itself():
    assert ReaderEnd(None).describe() == "closed by the other side"
    assert ReaderEnd(OSError("x")).describe() == "failed: OSError: x"
