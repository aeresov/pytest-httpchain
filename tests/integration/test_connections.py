"""Connection stages over a real socket: the reference adapter of
docs/usage/connections.md against the examples' newline-JSON server.

What a connection step means, its waits and its failures, is pinned in
tests/unit/test_connections.py and test_carrier.py, with an in-process adapter;
this is the round trip through pytest and TCP.
"""

import socket
import sys
import threading

import pytest

from tests.integration.helpers import HAR_ARGS, LINE_JSON_ADAPTER, connection_stage, line_json_scenario, named, stage

SCENARIO = "connections/test_connection_round_trip.http.json"
PUSH_SCENARIO = "connections/test_connection_push.http.json"
ADAPTER = LINE_JSON_ADAPTER


def _readers() -> list[str]:
    return [thread.name for thread in threading.enumerate() if thread.name.startswith("httpchain-connection-")]


def _ping(name: str, ping_id: int, connection: str = "device", **fields) -> dict:
    """A stage that pings and takes the pong of the same id."""
    return connection_stage(name, connection, send={"op": "ping", "id": ping_id}, expect={"match": {"op": "pong", "id": ping_id}, "timeout": 5}, **fields)


def _bye(name: str = "bye", connection: str = "device", **fields) -> dict:
    """A stage that says bye, takes the server's bye and closes the connection."""
    return connection_stage(name, connection, send={"op": "bye"}, expect={"match": "op == 'bye'", "timeout": 5}, close=True, **fields)


def test_open_send_expect_close_round_trip(run_scenario):
    """Open checks the greeting connect() returned, a ping takes its pong
    (its response steps read it as ``message``), and a bye closes the
    connection, whose reader has ended once the run has."""
    run_scenario(SCENARIO, ADAPTER).assert_outcomes(passed=3)
    assert not _readers()


def test_pushed_messages_queue_until_taken_or_drained(run_scenario):
    """Messages pushed before a stage expects them wait in the queue: a stage
    that sends nothing takes the oldest match from all of it, and drain
    discards what is left, which an absent expect then confirms."""
    run_scenario(PUSH_SCENARIO, ADAPTER).assert_outcomes(passed=4)
    assert not _readers()


@pytest.mark.parametrize(
    ("name", "scenario", "passed"),
    named(
        # A JMESPath string is taken when truthy; an object of expectations
        # when all hold, its values compared as values.
        (
            "string-and-object-match",
            line_json_scenario(
                connection_stage("by string", send={"op": "ping", "id": 1}, expect={"match": "op == 'pong' && id == `1`", "timeout": 5}),
                connection_stage("by object", send={"op": "ping", "id": 2}, expect={"match": {"op": "pong", "id": {"gt": 1}}, "timeout": 5}),
                _bye(),
            ),
            3,
        ),
        # send() returns the frame's seq, read as `sent` by the match and by
        # the response steps; the server's reply carries it back as reply_to.
        (
            "sent-correlation",
            line_json_scenario(
                connection_stage(
                    "first",
                    send={"op": "ping", "id": 7},
                    expect={"match": {"reply_to": "{{ sent.seq }}"}, "timeout": 5},
                    response=[{"verify": {"jmespath": {"op": "pong", "id": 7}, "expressions": ["{{ sent.seq == 1 and message.reply_to == 1 }}"]}}],
                ),
                connection_stage(
                    "second",
                    send={"op": "ping", "id": 8},
                    expect={"match": {"reply_to": "{{ sent.seq }}"}, "timeout": 5},
                    response=[{"verify": {"expressions": ["{{ sent.seq == 2 }}"]}}],
                ),
                _bye(),
            ),
            3,
        ),
        # Nothing matching arrives by the deadline: the pong does not count.
        (
            "absent-passes",
            line_json_scenario(connection_stage("quiet", send={"op": "ping", "id": 1}, expect={"match": "push", "absent": True, "timeout": 0.2}), _bye()),
            2,
        ),
        # The server hangs up: closed waits for that, and its message says
        # how the connection ended (EOFError from receive() is a clean end).
        (
            "closed-on-peer-hangup",
            line_json_scenario(
                connection_stage(
                    "kicked",
                    send={"op": "hangup"},
                    expect={"closed": True, "timeout": 5},
                    response=[{"verify": {"jmespath": {"error": None, "detail": None}}}],
                ),
            ),
            1,
        ),
        # Closed by a stage, the connection opens again on open: true, a
        # new session greeted afresh.
        (
            "close-then-reopen",
            line_json_scenario(
                _ping("ping", 1),
                _bye(),
                connection_stage("again", open=True, response=[{"verify": {"jmespath": {"greeting": "hello"}}}]),
                _ping("ping again", 2),
                _bye("bye again"),
            ),
            5,
        ),
        # Each connection has its own session: the server counts per
        # connection, so b's first count is 1 after two on a.
        (
            "two-connections",
            line_json_scenario(
                connection_stage("a once", "a", send={"op": "count"}, expect={"match": "op == 'count'", "timeout": 5}),
                connection_stage("a twice", "a", send={"op": "count"}, expect={"match": "op == 'count'", "timeout": 5}, response=[{"verify": {"jmespath": {"n": 2}}}]),
                connection_stage("b once", "b", send={"op": "count"}, expect={"match": "op == 'count'", "timeout": 5}, response=[{"verify": {"jmespath": {"n": 1}}}]),
                _bye("bye a", "a"),
                _bye("bye b", "b"),
                connections=("a", "b"),
            ),
            5,
        ),
        # Each attempt sends again and takes the reply to its own send: a
        # verify failure on n=1 and n=2 is retried, n=3 passes.
        (
            "retry-resends",
            line_json_scenario(
                connection_stage(
                    "poll",
                    send={"op": "count"},
                    expect={"match": {"reply_to": "{{ sent.seq }}"}, "timeout": 5},
                    response=[{"verify": {"jmespath": {"n": 3}}}],
                    stage={"retry": {"attempts": 3, "delay": 0}},
                ),
                _bye(),
            ),
            2,
        ),
    ),
)
def test_connection_steps_pass(run_scenario, name, scenario, passed):
    run_scenario(scenario, ADAPTER).assert_outcomes(passed=passed)
    assert not _readers()


@pytest.mark.slow
def test_retry_after_an_expect_timeout(run_scenario):
    """A timed-out expect is a verify failure, retried by default: the next
    attempt sends again, and takes the reply to that send. The first reply
    (n=1) does not match and stays queued."""
    scenario = line_json_scenario(
        connection_stage(
            "poll",
            send={"op": "count"},
            expect={"match": {"op": "count", "n": 2}, "timeout": 0.3},
            stage={"retry": {"attempts": 2, "delay": 0}},
        ),
        _bye(),
    )
    run_scenario(scenario, ADAPTER, args=("-s", "--log-level=INFO", "-rA")).stdout.fnmatch_lines(
        ["*Stage 'poll': attempt 1 of 2 failed, next in 0s: Stage 'poll': no message on connection 'device' matched {\"op\": \"count\", \"n\": 2} within 0.3 s*", "*2 passed*"],
    )


@pytest.mark.parametrize(
    ("name", "stages", "outcomes", "line"),
    named(
        # An absent expect fails at once on a match, naming the message;
        # without a send it considers the whole queue, pushes included.
        (
            "absent-hit",
            [
                connection_stage("trigger", send={"op": "push", "count": 1}, expect={"match": "op == 'pushed'", "timeout": 5}),
                connection_stage("nothing pushed", expect={"match": {"push": 1}, "absent": True, "timeout": 5}),
            ],
            {"passed": 1, "failed": 1},
            "*Stage 'nothing pushed': a message on connection 'device' matched {\"push\": 1} within 5 s, where none should: message #1, * s from the wait: {\"push\": 1}*",
        ),
        # The connection ending is not "nothing arrived": an absent expect
        # fails when the peer hangs up mid-wait, at once, not at its timeout.
        (
            "absent-peer-closes",
            [connection_stage("quiet", send={"op": "hangup"}, expect={"match": "push", "absent": True, "timeout": 30})],
            {"failed": 1},
            "*Stage 'quiet': connection 'device' closed by the other side while the stage waited for no message to match \"push\"*",
        ),
        # The peer ended the connection in an earlier stage: a send on it
        # fails as not open, without waiting.
        (
            "send-after-peer-end",
            [connection_stage("kicked", send={"op": "hangup"}, expect={"closed": True, "timeout": 5}), _ping("ping", 1)],
            {"passed": 1, "failed": 1},
            "*Stage 'ping': connection 'device' is not open (closed by the other side); set \"open\": true to connect again*",
        ),
        # The closing stage may skip (a templated skip_if), so the validator
        # leaves it to the run: closed by then, the connection is not open.
        (
            "closed-by-a-skippable-stage",
            [_ping("ping", 1), _bye(stage={"skip_if": "{{ false }}"}), _ping("status", 2)],
            {"passed": 2, "failed": 1},
            "*Stage 'status': connection 'device' is not open (closed by stage 'bye'); set \"open\": true to connect again*",
        ),
    ),
)
def test_connection_step_fails(run_scenario, name, stages, outcomes, line):
    result = run_scenario(line_json_scenario(*stages), ADAPTER)
    result.assert_outcomes(**outcomes)
    result.stdout.fnmatch_lines([line])
    assert not _readers()


def test_expect_after_the_peer_ended_takes_what_was_queued(run_scenario):
    """Messages queued before the peer hung up stay takeable: an expect on
    the ended connection takes a match from them, and fails as not open, at
    once, only once none is left."""
    scenario = line_json_scenario(
        connection_stage("trigger", send={"op": "push", "count": 2}),
        connection_stage("kicked", send={"op": "hangup"}, expect={"closed": True, "timeout": 5}),
        connection_stage("queued", expect={"match": {"push": 2}, "timeout": 5}),
        connection_stage("gone", expect={"match": {"push": 3}, "timeout": 30}),
    )
    result = run_scenario(scenario, ADAPTER)
    result.assert_outcomes(passed=3, failed=1)
    result.stdout.fnmatch_lines(["*Stage 'gone': connection 'device' is not open (closed by the other side); set \"open\": true to connect again*"])


def test_closed_connection_without_open_is_a_collection_error(run_scenario):
    """An unskippable stage closed the connection, so a later stage without
    open: true would fail whatever runs: HTTPCHAIN044, at collection."""
    result = run_scenario(line_json_scenario(_ping("ping", 1), _bye(), _ping("status", 2)), ADAPTER, args="--collect-only")
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*HTTPCHAIN044*"])


def test_always_run_after_an_abort(run_scenario):
    """After a failure, an always_run stage still sends on a connection that
    is open (a farewell), and one whose connection was never opened skips:
    it never connects."""
    scenario = line_json_scenario(
        connection_stage("hello", open=True, response=[{"verify": {"jmespath": {"greeting": "goodbye"}}}]),
        _ping("ping", 1),
        _bye(stage={"always_run": True}),
        connection_stage("spare hello", "spare", open=True, stage={"always_run": True}),
        connections=("device", "spare"),
    )
    result = run_scenario(scenario, ADAPTER, args=("-s", "-rs"))
    result.assert_outcomes(failed=1, passed=1, skipped=2)
    result.stdout.fnmatch_lines(["*Flow aborted, and connection 'spare' is not open*"])
    assert not _readers()


def test_teardown_ends_every_connection(run_scenario):
    """Connections a failure leaves open, and one no stage closes, are all
    closed at the chain's end: no reader thread outlives the run."""
    scenario = line_json_scenario(
        _ping("a", 1, "a"),
        _ping("b", 1, "b"),
        connection_stage("fails", "a", send={"op": "ping", "id": 2}, expect={"match": {"op": "pong"}, "timeout": 5}, response=[{"verify": {"jmespath": {"id": 3}}}]),
        connections=("a", "b"),
    )
    run_scenario(scenario, ADAPTER).assert_outcomes(passed=2, failed=1)
    assert not _readers()


def test_connect_failure_closes_the_half_open_adapter(run_scenario, pytester):
    """A refused connect() fails the stage as a request failure, retried as an
    OSError is, and close() is called on the adapter after each failed
    connect(), so a half-open connection is not left behind."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    pytester.makepyfile(
        tracking="""
from line_json import LineJsonAdapter


class TrackingAdapter(LineJsonAdapter):
    def close(self):
        print("close() after connect():", "connected" if self.sock is not None else "no socket")
        super().close()
"""
    )
    scenario = {
        "connections": {"device": {"adapter": "tracking:TrackingAdapter", "params": {"host": "127.0.0.1", "port": port}, "connect_timeout": 2}},
        "stages": [connection_stage("hello", open=True, stage={"retry": {"attempts": 2, "delay": 0}})],
    }
    result = run_scenario(scenario, ADAPTER)
    result.assert_outcomes(failed=1)
    out = result.stdout.str()
    accepted = ("Connection refused", "actively refused")
    # GitHub's Windows runners drop loopback SYNs to a closed port: there the
    # connect times out instead (see test_errors.py's test_connection_refused).
    if sys.platform == "win32":
        accepted += ("timed out",)
    assert "Connection 'device': connect() failed: " in out, out
    assert any(text in out for text in accepted), out
    assert "(after 2 attempts)" in out
    assert out.count("close() after connect(): no socket") == 2
    assert not _readers()


def test_http_save_feeds_connection_params(run_scenario):
    """A connection's params read what an earlier HTTP stage saved (here the
    port a registration call returned), and a later HTTP stage sends what the
    connection stage saved."""
    scenario = line_json_scenario(
        stage(
            "register",
            "/echo/json",
            request={"method": "POST", "body": {"json": {"port": "{{ env('HTTPCHAIN_EXAMPLE_LINE_JSON_PORT') }}"}}},
            response=[{"verify": {"status": 200}}, {"save": {"jmespath": {"device_port": "received.port"}}}],
        ),
        connection_stage(
            "ping",
            send={"op": "ping", "id": 1},
            expect={"match": {"op": "pong"}, "timeout": 5},
            response=[{"save": {"jmespath": {"server_time": "time"}}}],
        ),
        stage(
            "report",
            "/echo/json",
            request={"method": "POST", "body": {"json": {"time": "{{ server_time }}"}}},
            response=[{"verify": {"status": 200, "jmespath": {"received.time": 1700000000}}}],
        ),
        _bye(),
    )
    scenario["connections"]["device"] = {**scenario["connections"]["device"], "params": {"host": "127.0.0.1", "port": "{{ device_port }}"}}
    run_scenario(scenario, ADAPTER).assert_outcomes(passed=4)
    assert not _readers()


@pytest.mark.slow
def test_connection_scenarios_under_xdist(run_scenario):
    """Two connection scenarios on two xdist workers: each worker's chain
    opens its own connections to its own server."""
    result = run_scenario(SCENARIO, PUSH_SCENARIO, ADAPTER, args=("-n", "2", "--dist", "loadscope"), subprocess=True)
    result.assert_outcomes(passed=7)


@pytest.mark.slow
def test_failed_stage_leaves_the_connection_to_teardown(run_scenario, pytester):
    """A wait that times out fails its stage, naming what it waited for and
    what arrived; the stages after it skip, and the chain's end closes the
    connection it left open."""
    scenario = {
        "marks": ["usefixtures('line_json_server')"],
        "connections": {"device": {"adapter": "line_json:LineJsonAdapter", "params": {"host": "127.0.0.1", "port": "{{ env('HTTPCHAIN_EXAMPLE_LINE_JSON_PORT') }}"}}},
        "stages": [
            {
                "name": "wrong id",
                "connection": {"name": "device", "send": {"op": "ping", "id": 1}, "expect": {"match": {"id": 2}, "timeout": 0.3}},
                "response": [{"verify": {"jmespath": {"op": "pong"}}}],
            },
            {"name": "after", "connection": {"name": "device", "send": {"op": "bye"}}, "response": [{"verify": {"expressions": ["{{ true }}"]}}]},
        ],
    }
    pytester.copy_example(ADAPTER)
    result = run_scenario(scenario)
    result.assert_outcomes(failed=1, skipped=1)
    result.stdout.fnmatch_lines(["*Stage 'wrong id': no message on connection 'device' matched {\"id\": 2} within 0.3 s (1 message arrived after the send)*"])
    # The report's section: what was sent, the match as declared, and the
    # message that came, with why it did not match.
    result.stdout.fnmatch_lines(
        [
            "*- Connection Exchange -*",
            "Connection 'device' (adapter line_json:LineJsonAdapter), open 1, * s old",
            "Sent:",
            "send() returned:",
            "Expect:",
            '  "id": 2',
            "Result: timed out, no message matched",
            "Considered, not matching (1 message):",
            "#1 at +* s after the send: JMESPath 'id' doesn't match: expected 2, got 1",
            "Not considered: 0 messages queued before the send, 0 dropped by inbox_limit",
            "debug_info():",
            "unread bytes: ",
        ],
        consecutive=False,
    )
    assert not _readers()


def test_report_redacts_message_keys_and_writes_no_har(run_scenario, pytester):
    """Under -rP a passing connection stage's report shows its exchange, a
    listed key's value as [REDACTED] in the message sent and the one taken,
    while its verify saw the real value. A connection stage adds nothing to
    the HAR output: with no HTTP stage, no file is written."""
    scenario = {
        "marks": ["usefixtures('line_json_server')"],
        "connections": {"device": {"adapter": "line_json:LineJsonAdapter", "params": {"host": "127.0.0.1", "port": "{{ env('HTTPCHAIN_EXAMPLE_LINE_JSON_PORT') }}"}}},
        "stages": [
            {
                "name": "login",
                "connection": {"name": "device", "send": {"op": "login", "Token": "s3cret-tok"}, "expect": {"match": {"echo.op": "login"}, "timeout": 5}},
                "response": [{"verify": {"jmespath": {"echo.Token": "s3cret-tok"}}}],
            },
            {"name": "bye", "connection": {"name": "device", "send": {"op": "bye"}, "expect": {"match": "op == 'bye'", "timeout": 5}, "close": True}},
        ],
    }
    pytester.copy_example(ADAPTER)
    result = run_scenario(scenario, args=(*HAR_ARGS, "-rP"))
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(["*- Connection Exchange -*", "Sent:", "{", '  "op": "login",', '  "Token": "[REDACTED]"', "}"], consecutive=False)
    result.stdout.fnmatch_lines(["Result: took a message", "#1 at +* s after the send", "{", '  "echo": {', '    "op": "login",', '    "Token": "[REDACTED]"'], consecutive=False)
    assert "s3cret-tok" not in result.stdout.str()
    assert not list(pytester.path.glob("har_out/*.har"))
    assert not _readers()


def test_reopen_is_a_new_session(run_scenario):
    """The report names which open of the connection a stage used: a reopen
    is the second."""
    scenario = line_json_scenario(_bye(), connection_stage("again", open=True, response=[{"verify": {"jmespath": {"greeting": "hello"}}}]))
    result = run_scenario(scenario, ADAPTER, args=("-s", "-rP"))
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(
        ["Connection 'device' (adapter line_json:LineJsonAdapter), open 1, * s old", "Connection 'device' (adapter line_json:LineJsonAdapter), open 2, * s old"], consecutive=False
    )
