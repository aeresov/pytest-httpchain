"""Scenario ``ssl`` config against a server presenting a real certificate.

``tests/unit/test_carrier.py::TestSSLClientWiring`` pins what the engine hands
``httpx.Client`` — bool ``verify`` straight through, everything else as a ready
``ssl.SSLContext``. That is only half the claim: it cannot tell a correctly
built context from one that trusts the wrong thing, because no handshake ever
happens. These tests complete it, with trustme issuing a throwaway CA for the
mock server (see the ``https_server`` / ``mtls_server`` fixtures).

The certificate paths are scenario-relative, like ``$ref`` and ``body.binary``:
the fixtures write ``ca.pem`` and ``client.pem`` next to the scenario file.
"""

import pytest

# Imported here, in the OUTER session, deliberately. `pytester` snapshots
# `sys.modules` before each run and deletes whatever that run imported, so if
# the copied conftest is the first to `import trustme`, teardown evicts part of
# `cryptography` — a later run then re-imports a second copy of
# `...asymmetric.ec`, and `ec.SECP256R1()` fails an isinstance check against the
# surviving copy's `EllipticCurve` ABC ("curve must be an EllipticCurve
# instance"). Importing it before any snapshot is taken keeps exactly one copy
# alive for every run, so the fixtures' lazy import is always a cache hit.
import trustme  # noqa: F401

# Real TLS handshakes plus per-scenario CA generation (trustme issues actual
# keys), so these belong with the suite's slower families.
pytestmark = pytest.mark.slow


def test_ca_bundle_is_trusted(run_scenario):
    """``ssl.verify: <path>`` makes the throwaway CA's certificate acceptable,
    and the response body genuinely round-trips over the TLS connection."""
    result = run_scenario("ssl/test_ssl_verify_ca_bundle.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_default_verify_rejects_untrusted_certificate(run_scenario):
    """The negative control for the test above: with no ``ssl`` block at all,
    the same server's certificate fails verification, the stage fails cleanly
    (no raw httpx traceback), and the chain aborts."""
    result = run_scenario("ssl/test_ssl_verify_untrusted.http.json")
    result.assert_outcomes(errors=0, failed=1, passed=0, skipped=1)
    result.stdout.fnmatch_lines(["*CERTIFICATE_VERIFY_FAILED*"])


def test_verify_false_accepts_untrusted_certificate(run_scenario):
    """``verify: false`` is what turns the failure above into a pass — proof
    the flag reaches the transport rather than only the client kwargs."""
    result = run_scenario("ssl/test_ssl_verify_disabled.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_client_certificate_is_presented(run_scenario):
    """``ssl.cert`` (single key+chain bundle) satisfies a server demanding a
    client certificate."""
    result = run_scenario("ssl/test_ssl_client_cert.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_missing_client_certificate_fails_handshake(run_scenario):
    """The negative control for ``ssl.cert``: trusting the CA is not enough, so
    the passing case above cannot be explained by ``verify`` alone."""
    result = run_scenario("ssl/test_ssl_client_cert_missing.http.json")
    result.assert_outcomes(errors=0, failed=1, passed=0)
    # How the refusal surfaces is the platform's choice, so only the refusal is
    # asserted: OpenSSL reports the server's CERTIFICATE_REQUIRED alert, while
    # Windows reports just the reset that follows it (WinError 10054) and never
    # shows the alert. Pinning the OpenSSL spelling made this Linux-only.
    output = result.stdout.str()
    assert "CERTIFICATE_REQUIRED" in output or "10054" in output, output
