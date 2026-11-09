"""Connection stages over a real socket: the reference adapter of
docs/usage/connections.md against the examples' newline-JSON server.

What a connection step means, its waits and its failures, is pinned in
tests/unit/test_connections.py and test_carrier.py, with an in-process adapter;
this is the round trip through pytest and TCP.
"""

import threading

import pytest

from tests.integration.helpers import HAR_ARGS

SCENARIO = "connections/test_connection_round_trip.http.json"
ADAPTER = "connections/line_json.py"


def _readers() -> list[str]:
    return [thread.name for thread in threading.enumerate() if thread.name.startswith("httpchain-connection-")]


def test_open_send_expect_close_round_trip(run_scenario):
    """Open checks the greeting connect() returned, a ping takes its pong
    (its response steps read it as ``message``), and a bye closes the
    connection, whose reader has ended once the run has."""
    run_scenario(SCENARIO, ADAPTER).assert_outcomes(passed=3)
    assert not _readers()


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
