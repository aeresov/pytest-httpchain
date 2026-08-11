"""End-to-end redirect handling: the ``allow_redirects`` -> httpx
``follow_redirects`` wiring against a real 302, and the report labeling of a
redirected exchange. Nothing else exercises a genuine redirect through the
engine — the unit tests build ``Response.history`` by hand."""


def test_redirect_followed_by_default(run_scenario):
    """allow_redirects defaults to true: the 302 is followed to the target."""
    result = run_scenario("redirects/test_redirect_follow.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_redirect_not_followed_when_disabled(run_scenario):
    """allow_redirects: false surfaces the 302 response itself."""
    result = run_scenario("redirects/test_redirect_no_follow.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_redirected_failure_report_says_hop_count(run_scenario):
    """The shown request is the final hop's; the report must say a redirect
    happened rather than presenting it as what the stage authored."""
    result = run_scenario("redirects/test_redirect_fail.http.json")
    result.assert_outcomes(errors=0, failed=1, passed=0)
    result.stdout.fnmatch_lines(["*after 1 redirect*"])
