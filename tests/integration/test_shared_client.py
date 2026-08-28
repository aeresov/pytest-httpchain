"""One `httpx.Client` per scenario, shared by every stage.

Documented in docs/usage/scenarios.md as a guarantee, so it needs a pin: the
cookie jar is the observable half, and nothing else in the suite exercises it.
"""


def test_cookie_jar_is_shared_across_stages(pytester):
    """A Set-Cookie from an earlier stage is sent by a later one with no save
    step — the stages share one client, not one per request."""
    pytester.copy_example("conftest.py")
    pytester.copy_example("client/test_shared_cookie_jar.http.json")

    result = pytester.runpytest()

    result.assert_outcomes(passed=2)
