import socket
import sys

import pytest

from tests.integration.helpers import named, stage


@pytest.mark.parametrize(
    ("scenario", "line"),
    named(
        # Not "HTTP request timed out": in-process pytester can leave httpx's
        # exception mapping undone (see test_har_output), so only the cause is
        # stable here; the mapping itself is pinned in test_carrier.
        ("timeout_error", "*timed out*"),
        ("expression_failure", "*Expression*failed*"),
        ("header_failure", "*Header*doesn't match*"),
        ("malformed_json_save", "*Cannot extract variables, response is not valid JSON*"),
        ("malformed_json_schema", "*Cannot validate schema, response is not valid JSON*"),
    ),
)
def test_stage_fails_for_the_stated_reason(run_scenario, scenario, line):
    result = run_scenario(f"errors/test_{scenario}.http.json")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines([line])


def test_verify_failure(run_scenario):
    result = run_scenario("errors/test_verify_failure.http.json")
    result.assert_outcomes(failed=1)
    # A non-parallel stage failure must surface the real error, not be
    # mislabeled as a parallel-execution failure (M1).
    result.stdout.fnmatch_lines(["*Status code doesn't match*"])
    result.stdout.no_fnmatch_line("*Parallel execution failed*")


def test_parallel_failure(run_scenario):
    result = run_scenario("errors/test_parallel_failure.http.json")
    result.assert_outcomes(failed=1)
    # A genuinely parallel stage keeps the iteration-labeled prefix (M1).
    result.stdout.fnmatch_lines(["*Parallel execution failed at iteration*"])


def test_connection_refused(run_scenario):
    """Test connection refused error when server is not running"""
    result = run_scenario("errors/test_connection_refused.http.json")
    result.assert_outcomes(failed=1)
    # A clean connection-level failure of a server that is not running is the
    # guard here. POSIX refuses ("Connection refused"); Windows spells it
    # "actively refused" — but GitHub's Windows CI runners silently DROP
    # loopback SYNs to closed ports (verified even for a just-released bound
    # port), so the failure there surfaces as the request timeout instead.
    # "timed out" is accepted only on Windows: on POSIX a timeout instead of a
    # refusal is a real regression and must not be masked.
    out = result.stdout.str()
    accepted = ("Connection refused", "actively refused")
    if sys.platform == "win32":
        accepted += ("timed out",)
    assert any(text in out for text in accepted), out


_UNRESOLVABLE_HOST = "this-hostname-definitely-does-not-exist-12345.invalid"


def test_invalid_hostname(run_scenario):
    """Test error handling for invalid hostname (DNS resolution failure)"""
    # This is the suite's only real-network dependency. Some resolvers (captive
    # portals, ISPs, corporate DNS) wildcard NXDOMAIN and hand back an address
    # for any name, which turns the intended resolution failure into a
    # connection/timeout error and flakes the assertion below. Skip rather than
    # flake when the environment's resolver hijacks the lookup.
    try:
        socket.getaddrinfo(_UNRESOLVABLE_HOST, 80)
    except socket.gaierror:
        pass  # good: the resolver correctly fails to resolve the bogus name
    else:
        pytest.skip("resolver wildcards NXDOMAIN; cannot test DNS failure here")

    result = run_scenario("errors/test_invalid_hostname.http.json")
    result.assert_outcomes(failed=1)
    # Must fail specifically because the host name could not be resolved (DNS),
    # not for an unrelated reason. httpx classifies this as either a ConnectError
    # ("HTTP connection error") or a generic failure ("Unexpected error")
    # depending on resolver state, so the stable fragment is the OS resolver
    # text rather than the plugin's wrapper prefix — and that text differs per
    # platform/libc.
    out = result.stdout.str()
    resolver_texts = (
        "Name or service not known",  # glibc
        "Name does not resolve",  # musl
        "getaddrinfo failed",  # Windows (WinError 11001)
        "nodename nor servname provided",  # macOS
    )
    assert any(text in out for text in resolver_texts), out


def test_reserved_name_runtime_warning_under_error_filter(pytester, run_scenario):
    """HTTPCHAIN027's runtime twin is a ScenarioValidationWarning; under
    filterwarnings=error it must surface as a clean stage failure that aborts
    the chain — not a raw warning-exception traceback that bypasses it."""
    pytester.makepyfile(userfuncs="def make_reserved(response):\n    return {'response': 'shadowed'}\n")
    steps = [{"verify": {"status": 200}}, {"save": {"user_functions": ["userfuncs:make_reserved"]}}]
    scenario = {"stages": [stage("s0", response=steps), stage("s1")]}
    result = run_scenario(scenario, args=("-s", "-W", "error::pytest_httpchain.ScenarioValidationWarning"))
    result.assert_outcomes(failed=1, skipped=1)
    result.stdout.fnmatch_lines(["*HTTPCHAIN027*"])
