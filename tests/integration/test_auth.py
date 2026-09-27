import pytest

from tests.integration.helpers import HAR_ARGS, har_entries, named, stage


@pytest.mark.parametrize(
    ("scenario", "passed"),
    named(
        ("scenario_auth", 1),
        ("request_auth", 1),
        ("auth_from_fixture", 1),
        # The built-ins against the example server's flask-httpauth endpoints.
        # Basic: from scenario substitutions, and a stage's auth: false
        # answered 401, so the client's credentials really stayed home.
        ("builtin_basic", 3),
        # Digest: the scenario's one flow answers the first challenge and then
        # a parallel stage's concurrent requests; a request's answers its own.
        # A verify function reads each request of the exchange, as it would
        # any request (they went out unread: RequestNotRead).
        ("builtin_digest", 4),
        # Bearer: rendered per stage, from the token a login stage saved.
        ("builtin_bearer", 3),
    ),
)
def test_auth(run_scenario, scenario, passed):
    run_scenario(f"auth/test_{scenario}.http.json", "auth.py").assert_outcomes(passed=passed)


def test_request_auth_failure_fails_stage_cleanly(run_scenario):
    """A raising request-level auth function becomes a clean stage failure that
    aborts the chain — not a raw traceback bypassing the abort machinery (the
    scenario-level twin lives in the lazy-init suite; this is request_builder's
    per-request branch)."""
    result = run_scenario("auth/test_request_auth_failure.http.json", "auth.py")
    result.assert_outcomes(failed=1, skipped=1)
    result.stdout.fnmatch_lines(["*Failed to configure authentication*"])


def test_auth_from_a_vars_object(run_scenario):
    """An auth written as one template over a ``vars`` object, which renders as
    a namespace: the scheme it was written as, at scenario level (validated on
    its own once rendered) and on a request (as part of the request)."""
    logins = {"basic_login": {"basic": {"username": "user", "password": "pass"}}, "digest_login": {"digest": {"username": "user", "password": "pass"}}}
    scenario = {
        "substitutions": [{"vars": logins}],
        "auth": "{{ basic_login }}",
        "stages": [stage("scenario_auth", "/answer"), stage("request_auth", "/digest", request={"auth": "{{ digest_login }}"})],
    }
    run_scenario(scenario).assert_outcomes(passed=2)


def test_bearer_token_is_redacted_in_the_report(run_scenario):
    """The flow sets the Authorization header on the request httpx sends, which
    is the one the report shows: the token a login stage saved (the server
    issues them as "s3cret-...") is redacted like a header the stage wrote
    itself."""
    me = stage("me", "/me", request={"auth": {"bearer": "{{ token }}"}}, response=[{"verify": {"status": 418}}])
    result = run_scenario({"stages": [_LOGIN, me]})

    result.assert_outcomes(passed=1, failed=1)
    output = result.stdout.str()
    assert "s3cret" not in output
    assert "authorization: [REDACTED]" in output


_LOGIN = stage(
    "login",
    "/login",
    request={"method": "POST", "body": {"json": {"username": "user", "password": "pass"}}},
    response=[{"save": {"jmespath": {"token": "token"}}}],
)


@pytest.mark.parametrize(
    ("scenario", "outcomes"),
    [
        pytest.param(
            {"substitutions": [{"vars": {"token": "s3cret-scenario"}}], "auth": "{{ token }}", "stages": [stage("me", "/me")]},
            {"failed": 1},
            id="scenario",
        ),
        # The token the server issued, which a login stage saved.
        pytest.param({"stages": [_LOGIN, stage("me", "/me", request={"auth": "{{ token }}"})]}, {"passed": 1, "failed": 1}, id="request"),
    ],
)
def test_token_written_as_the_whole_auth_is_not_quoted(run_scenario, scenario, outcomes):
    """``"auth": "{{ token }}"`` for ``{"bearer": "{{ token }}"}``: a string auth
    is a user function's name, which a template stands for until it renders,
    so validate passes it, and the token it renders is no name. The failure
    says what a string auth is, without printing the token, where the name's
    own messages quoted it."""
    result = run_scenario(scenario)

    result.assert_outcomes(**outcomes)
    result.stdout.fnmatch_lines(["*Not a user function's 'module:function' name (not shown, as auth can carry a credential)*"])
    assert "s3cret" not in result.stdout.str()


@pytest.mark.parametrize(
    ("scenario", "reason"),
    [
        # The module found (json), the function (the password) not.
        pytest.param(
            {"substitutions": [{"vars": {"creds": "json:s3cret_pass"}}], "auth": "{{ creds }}", "stages": [stage("answer", "/answer")]},
            "its module has no function of that name",
            id="scenario",
        ),
        pytest.param(
            {"substitutions": [{"vars": {"creds": "s3cret_user:s3cret_pass"}}], "stages": [stage("answer", "/answer", request={"auth": "{{ creds }}"})]},
            "importing its module raised ModuleNotFoundError",
            id="request",
        ),
    ],
)
def test_credentials_written_as_the_whole_auth_are_not_quoted(run_scenario, scenario, reason):
    """Basic credentials as one string, ``"user:password"``, have the shape of
    a user function's name, so validate passes them and they are imported as
    one. The import failure named the user name as the module, and the
    password as the function when the user name is a module's; a name a
    template rendered is not quoted now, only why it failed."""
    result = run_scenario(scenario)

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines([f"*auth's template rendered a user function name (not shown, as auth can carry a credential) that does not import: {reason}; *"])
    assert "s3cret" not in result.stdout.str()


def test_bearer_token_rendered_to_none_fails_the_stage(run_scenario):
    """A token a template rendered to None is not "no auth": the stage fails
    before sending, naming the field, rather than going out unauthenticated or
    with the scenario's credentials."""
    scenario = {"auth": {"basic": {"username": "user", "password": "pass"}}, "stages": [stage("bearer", "/answer", request={"auth": {"bearer": "{{ get('token') }}"}})]}
    result = run_scenario(scenario)

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*'request.auth.bearer' was declared as \"{{ get('token') }}\" but rendered to None*"])


def test_digest_challenge_is_reported_and_exported(run_scenario, pytester):
    """A digest request first goes out without credentials, and httpx keeps the
    401 challenge in the response's history, where redirects go too. The
    report labels it as the auth exchange it was, not a redirect, and the HAR
    file holds both requests."""
    request = {"auth": {"digest": {"username": "user", "password": "pass"}}}
    result = run_scenario({"stages": [stage("digest", "/digest", request=request, response=[{"verify": {"status": 418}}])]}, args=HAR_ARGS)

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*HTTP Request (after 1 auth exchange)*", "authorization: [[]REDACTED]"])
    challenge, answer = har_entries(pytester.path / "har_out")
    assert challenge["response"]["status"] == 401
    assert "authorization" not in {header["name"].lower() for header in challenge["request"]["headers"]}
    assert answer["response"]["status"] == 200
    assert "authorization" in {header["name"].lower() for header in answer["request"]["headers"]}
