"""Connection stages over a real socket: the reference adapter of
docs/usage/connections.md against the examples' newline-JSON server.

What a connection step means, its waits and its failures, is pinned in
tests/unit/test_connections.py and test_carrier.py, with an in-process adapter;
this is the round trip through pytest and TCP.
"""

import threading

import pytest

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
    assert not _readers()
