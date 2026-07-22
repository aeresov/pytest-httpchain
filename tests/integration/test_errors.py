from tests.integration.conftest import run_scenario


def test_verify_failure(pytester):
    """Test verification failure reports clearly"""
    result = run_scenario(pytester, "errors/test_verify_failure.http.json")
    result.assert_outcomes(errors=0, failed=1, passed=0)
    # A non-parallel stage failure must surface the real error, not be
    # mislabeled as a parallel-execution failure (M1).
    result.stdout.fnmatch_lines(["*Status code doesn't match*"])
    result.stdout.no_fnmatch_line("*Parallel execution failed*")


def test_timeout_error(pytester):
    """Test request timeout handling"""
    result = run_scenario(pytester, "errors/test_timeout_error.http.json")
    result.assert_outcomes(errors=0, failed=1, passed=0)
    # Must fail specifically because the request timed out, not for any other reason.
    result.stdout.fnmatch_lines(["*timed out*"])


def test_expression_failure(pytester):
    """Test expression verification failure"""
    result = run_scenario(pytester, "errors/test_expression_failure.http.json")
    result.assert_outcomes(errors=0, failed=1, passed=0)
    # Must fail specifically on the expression verification, not elsewhere.
    result.stdout.fnmatch_lines(["*Expression*failed*"])


def test_header_failure(pytester):
    """Test header verification failure"""
    result = run_scenario(pytester, "errors/test_header_failure.http.json")
    result.assert_outcomes(errors=0, failed=1, passed=0)
    # Must fail specifically on the header mismatch, not elsewhere.
    result.stdout.fnmatch_lines(["*Header*doesn't match*"])


def test_parallel_failure(pytester):
    """Test parallel execution failure handling"""
    result = run_scenario(pytester, "errors/test_parallel_failure.http.json")
    result.assert_outcomes(errors=0, failed=1, passed=0)
    # A genuinely parallel stage keeps the iteration-labeled prefix (M1).
    result.stdout.fnmatch_lines(["*Parallel execution failed at iteration*"])


def test_connection_refused(pytester):
    """Test connection refused error when server is not running"""
    result = run_scenario(pytester, "errors/test_connection_refused.http.json")
    result.assert_outcomes(errors=0, failed=1, passed=0)
    # A clean connection-level failure of a server that is not running is the
    # guard here. POSIX refuses ("Connection refused"); Windows spells it
    # "actively refused" — but GitHub's Windows CI runners silently DROP
    # loopback SYNs to closed ports (verified even for a just-released bound
    # port), so the failure there surfaces as the request timeout instead.
    out = result.stdout.str()
    accepted = ("Connection refused", "actively refused", "timed out")
    assert any(text in out for text in accepted), out


def test_invalid_hostname(pytester):
    """Test error handling for invalid hostname (DNS resolution failure)"""
    result = run_scenario(pytester, "errors/test_invalid_hostname.http.json")
    result.assert_outcomes(errors=0, failed=1, passed=0)
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


def test_malformed_json_save(pytester):
    """Test error handling when trying to save from malformed JSON response"""
    result = run_scenario(pytester, "errors/test_malformed_json_save.http.json")
    result.assert_outcomes(errors=0, failed=1, passed=0)
    result.stdout.fnmatch_lines(["*not valid JSON*"])


def test_malformed_json_schema(pytester):
    """Test error handling when validating malformed JSON against schema"""
    result = run_scenario(pytester, "errors/test_malformed_json_schema.http.json")
    result.assert_outcomes(errors=0, failed=1, passed=0)
    result.stdout.fnmatch_lines(["*not valid JSON*"])
