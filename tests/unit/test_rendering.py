"""rendering.py: what the guard every declared model is rendered through
refuses (a field a template rendered to None, named with its template and
what the None would have done, declared or in a model rendered whole) and
what it lets through (a None that means something, auth rendered whole in
any form), the relocated report of a verify value validated alone, and an
escape rendered once.

Where the carrier applies the guard, at each render site of a stage, is
pinned in test_carrier.py.
"""

import warnings
from collections import ChainMap
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from pydantic_core import PydanticCustomError

import pytest_httpchain.rendering as rendering_module
from pytest_httpchain.errors import StageExecutionError, VerificationError
from pytest_httpchain.models import (
    AuthCredentials,
    BearerAuth,
    ConnectionExpect,
    DigestAuth,
    FilesBody,
    FileSpec,
    ParallelRepeatConfig,
    RegexSave,
    Request,
    ResponseBody,
    SSLConfig,
    UserFunctionKwargs,
    UserFunctionName,
    VarsSubstitution,
    Verify,
)
from pytest_httpchain.redaction import DEFAULT_REDACTION
from pytest_httpchain.rendering import listed_expectations, render_declared, rendered_match
from tests.unit.helpers import Posing
from tests.unit.models.helpers import GRAPHQL_QUERY_TAG, OBJECT_TAG


class TestClientWiring:
    """The requests sent on the shared client: a request rendered declares
    only the fields it declared, so the client's own settings apply to the
    rest. The client and what it sends are pinned in test_carrier.py
    (TestClientWiring)."""

    def test_rendered_request_keeps_its_declared_fields(self):
        """Rendering dumped every field, defaults included, so the rendered
        request declared a timeout and a redirect setting it never had, and
        they overrode the client's."""
        rendered = render_declared(Request.model_validate({"url": "{{ u }}", "headers": {"A": "{{ a }}"}}), {"u": "http://t/", "a": "1"}, "request")
        assert rendered.model_fields_set == {"url", "headers"}


def _header_matcher(field: str) -> Verify:
    """A matcher whose ``field`` is templated beside a static check, so it
    still re-validates (a matcher needs one field set) once ``field`` is gone."""
    static = "not_contains" if field == "contains" else "contains"
    return Verify.model_validate({"headers": {"Location": {field: "{{ x }}", static: "/items"}}})


class TestRenderedAwayFields:
    """A field the scenario declared, whose template rendered to None, is
    refused. walk() re-validates the rendered model, but an optional field
    accepts None, so it arrived looking undeclared: the assertion went
    unchecked, the setting unapplied, and the stage was green. Only ``status``
    and ``body.schema`` used to be guarded; the guard now covers every model the
    carrier renders. Where validation rejects the None as well, the same message
    replaces pydantic's report for it. Each render site of a stage that applies
    the guard is pinned in test_carrier.py (TestRenderedAwayFields)."""

    RENDERS_NONE = ChainMap({"x": None})

    @pytest.mark.parametrize(
        ("where", "declared", "path"),
        [
            pytest.param("verify", Verify(status="{{ x }}"), "verify.status", id="verify-status"),
            pytest.param("verify", Verify(body=ResponseBody(schema="{{ x }}")), "verify.body.schema", id="verify-body-schema"),
            *(
                pytest.param("verify", _header_matcher(field), f"verify.headers.Location.{field}", id=f"header-{field}")
                for field in ("contains", "not_contains", "matches", "not_matches")
            ),
            # No limiter at all: the stage ran unthrottled.
            pytest.param("parallel", ParallelRepeatConfig.model_validate({"repeat": 2, "calls_per_sec": "{{ x }}"}), "parallel.calls_per_sec", id="calls-per-sec"),
            # Sent unauthenticated, or with the scenario-level credentials.
            pytest.param("request", Request.model_validate({"url": "http://t/", "auth": "{{ x }}"}), "request.auth", id="request-auth"),
            # Connected without the client certificate.
            pytest.param("ssl", SSLConfig(cert="{{ x }}"), "ssl.cert", id="ssl-cert"),
        ],
    )
    def test_rendered_away_field_is_refused(self, where, declared, path):
        with pytest.raises(StageExecutionError) as excinfo:
            render_declared(declared, self.RENDERS_NONE, where)
        assert str(excinfo.value) == f"'{path}' was declared as " + "'{{ x }}' but rendered to None, which would silently disable it"

    def test_rendered_away_regex_group_says_what_it_would_have_saved(self):
        """A regex save's group left out does not disable the save: it saves
        the default group (group 1, or the whole match), so the refusal says
        that, not that the None would have disabled anything."""
        with pytest.raises(StageExecutionError) as excinfo:
            render_declared(RegexSave.model_validate({"regex": {"v": {"pattern": "(a)(b)", "group": "{{ x }}"}}}), self.RENDERS_NONE, "save")
        assert str(excinfo.value) == "'save.regex.v.group' was declared as '{{ x }}' but rendered to None, which would silently save the default group instead"

    @pytest.mark.parametrize("field", ["filename", "content_type"])
    def test_rendered_away_file_name_or_type_says_what_it_would_have_sent(self, field):
        """A multipart file's filename or content type left out is sent as
        the default one (the path's name, the guessed type), so the refusal
        says that."""
        declared = Request.model_validate({"url": "http://t/", "body": {"multipart": {"files": {"f": {"content": "c", field: "{{ x }}"}}}}})
        with pytest.raises(StageExecutionError) as excinfo:
            render_declared(declared, self.RENDERS_NONE, "request")
        declared_as = f"'request.body.multipart.files.f.{field}' was declared as " + "'{{ x }}'"
        assert str(excinfo.value) == f"{declared_as} but rendered to None, which would silently send the default {field.replace('_', ' ')} instead"

    @pytest.mark.parametrize(
        "specs",
        [
            pytest.param([{"content": "a"}, {"content": "b", "filename": None}], id="list-of-dicts"),
            # `vars` objects in a tuple: the tuple is the list, each namespace
            # the file object it stands for.
            pytest.param((SimpleNamespace(content="a"), SimpleNamespace(content="b", filename=None)), id="tuple-of-namespaces"),
        ],
    )
    def test_file_objects_in_a_list_rendered_whole_are_refused_too(self, specs):
        """A list of file objects one template renders (``"f": "{{ specs }}"``)
        is a list of models known only once validated: each is checked as a
        model rendered whole, a key it set to None refused."""
        context = ChainMap({"specs": specs})
        declared = FilesBody.model_validate({"files": {"f": "{{ specs }}"}})
        with pytest.raises(StageExecutionError) as excinfo:
            render_declared(declared, context, "request.body")
        assert str(excinfo.value) == "'request.body.files.f[1].filename' was declared as '{{ specs }}' but rendered to None, which would silently send the default filename instead"

    def test_other_list_rendered_whole_is_not_walked(self, monkeypatch):
        """Only a list of files holds models, one level deep: any other list
        a template renders, a JSON body of a million values say, holds none,
        and walking each of its items, and each list in them, doubled the time
        the body took to render."""
        calls = 0
        walk_whole = rendering_module._rendered_whole_away

        def counted(*args):
            nonlocal calls
            calls += 1
            return walk_whole(*args)

        monkeypatch.setattr(rendering_module, "_rendered_whole_away", counted)
        declared = Request.model_validate({"url": "http://t/", "method": "POST", "body": {"json": "{{ big }}"}})
        rendered = render_declared(declared, ChainMap({"big": [[i, i + 1] for i in range(1000)]}), "request")
        assert rendered.body.json[999] == [999, 1000]
        # The model's fields and the body's, but none of the list's items.
        assert calls < 50

    @pytest.mark.parametrize(
        ("spec", "sent"),
        [
            pytest.param({"path": "a.txt", "content": None, "base64": None}, FileSpec(path=Path("a.txt")), id="dict"),
            pytest.param(SimpleNamespace(content="a", path=None), FileSpec(content="a"), id="namespace"),
            pytest.param([{"content": "a", "path": None}], [FileSpec(content="a")], id="in-a-list"),
        ],
    )
    def test_file_object_rendered_whole_with_null_sources_is_sent(self, spec, sent):
        """A file object counts its sources by value: a None in the ones it
        does not use (a user function, a saved object filling every key) is
        not set, as in the same object written out, and disables nothing."""
        declared = FilesBody.model_validate({"files": {"f": "{{ spec }}"}})
        assert render_declared(declared, ChainMap({"spec": spec}), "request.body").files["f"] == sent

    @pytest.mark.parametrize(
        ("where", "declared", "path"),
        [
            # A multipart file's only source: "sets exactly one of: path,
            # content, base64", asking for what the scenario did set.
            pytest.param(
                "request",
                Request.model_validate({"url": "http://t/", "body": {"multipart": {"files": {"f": {"path": "{{ x }}"}}}}}),
                "request.body.multipart.files.f.path",
                id="multipart-file-path",
            ),
            # A matcher's only field: pydantic's report asked the scenario to
            # "set at least one of: contains, ..." — the field it did set.
            pytest.param("verify", Verify.model_validate({"headers": {"Location": {"contains": "{{ x }}"}}}), "verify.headers.Location.contains", id="single-field-matcher"),
            # A required field, which pydantic reported as "URL input should be
            # a string or URL", naming neither the template nor the None.
            pytest.param("request", Request.model_validate({"url": "{{ x }}"}), "request.url", id="required-field"),
            # A user-function name is a RootModel, dumped as its bare root value.
            pytest.param("auth", UserFunctionName("{{ x }}"), "auth", id="scenario-auth"),
            # A built-in's credential: not "no auth", which the stage would
            # have sent unauthenticated or with the scenario's credentials.
            pytest.param("request", Request.model_validate({"url": "http://t/", "auth": {"bearer": "{{ x }}"}}), "request.auth.bearer", id="request-auth-bearer"),
            pytest.param(
                "request",
                Request.model_validate({"url": "http://t/", "auth": {"basic": {"username": "u", "password": "{{ x }}"}}}),
                "request.auth.basic.password",
                id="request-auth-basic-password",
            ),
            pytest.param("auth", DigestAuth.model_validate({"digest": {"username": "{{ x }}", "password": "p"}}), "auth.digest.username", id="scenario-auth-digest-username"),
            pytest.param("verify", Verify.model_validate({"user_functions": ["{{ x }}"]}), "verify.user_functions[0]", id="function-name-in-a-list"),
            pytest.param("save", RegexSave.model_validate({"regex": {"v": {"pattern": "{{ x }}"}}}), "save.regex.v.pattern", id="regex-save-pattern"),
            pytest.param("save", RegexSave.model_validate({"regex": {"v": {"pattern": "(a)", "all": "{{ x }}"}}}), "save.regex.v.all", id="regex-save-all"),
        ],
    )
    def test_rendered_away_field_validation_rejects_is_named(self, where, declared, path):
        """Nothing is silently disabled here — validation rejects the None — but
        the failure is still the guard's, naming the field and the template,
        without the "silently disable" clause that would not be true."""
        with pytest.raises(StageExecutionError) as excinfo:
            render_declared(declared, self.RENDERS_NONE, where)
        assert str(excinfo.value) == f"'{path}' was declared as " + "'{{ x }}' but rendered to None"
        assert isinstance(excinfo.value.__cause__, ValidationError)

    @pytest.mark.parametrize(
        "template",
        [
            pytest.param("{{ {'contains': x, 'not_contains': 'error'} }}", id="built-by-the-expression"),
            # Saved whole from the response: a JMESPath multiselect such as
            # `{contains: no_such_key, not_contains: 'error'}` gives the key null.
            pytest.param("{{ matcher }}", id="saved-whole"),
        ],
    )
    def test_matcher_rendered_whole_is_refused_too(self, template):
        """A header matcher written as one template is a string until it renders,
        so its fields are known only from the validated matcher: a key the
        rendered mapping set to None is refused like a declared field (the static
        sibling kept the matcher valid, and ``contains`` went unchecked)."""
        context = self.RENDERS_NONE.new_child({"matcher": {"contains": None, "not_contains": "error"}})
        with pytest.raises(StageExecutionError) as excinfo:
            render_declared(Verify.model_validate({"headers": {"Location": template}}), context, "verify")
        assert str(excinfo.value) == f"'verify.headers.Location.contains' was declared as {template!r} but rendered to None, which would silently disable it"

    @pytest.mark.parametrize("source", ["saved", "vars"])
    def test_regex_capture_rendered_whole_is_refused_too(self, source):
        """A ``save.regex`` entry written as one template renders a capture
        object, as a header matcher can be: a key it set to None is refused,
        from a saved object or from a ``vars`` one (a namespace), saying what
        the None would have done, as for a group declared as a template."""
        capture = {"pattern": "(a)(b)", "group": None}
        context = ChainMap(VarsSubstitution(vars={"capture": capture}).vars) if source == "vars" else {"capture": capture}
        with pytest.raises(StageExecutionError) as excinfo:
            render_declared(RegexSave.model_validate({"regex": {"v": "{{ capture }}"}}), context, "save")
        assert str(excinfo.value) == "'save.regex.v.group' was declared as '{{ capture }}' but rendered to None, which would silently save the default group instead"

    @pytest.mark.parametrize(
        ("declared", "context", "path", "invalid"),
        [
            # None is a valid auth: before, the refusal alone was reported, and
            # the url error was only in its __cause__.
            pytest.param(Request.model_validate({"url": "{{ u }}", "auth": "{{ x }}"}), {"u": "ftp://t/", "x": None}, "request.auth", "url", id="beside-a-none-that-validates"),
            pytest.param(
                Verify.model_validate({"status": "{{ u }}", "headers": {"H": {"contains": "{{ x }}", "not_contains": "e"}}}),
                {"u": 999, "x": None},
                "verify.headers.H.contains",
                "status",
                id="beside-a-matcher-field",
            ),
            # None is invalid for timeout too, but that is the refusal's to say:
            # the report lists only what else is wrong.
            pytest.param(
                Request.model_validate({"url": "{{ u }}", "timeout": "{{ x }}"}), {"u": "ftp://t/", "x": None}, "request.timeout", "url", id="beside-a-none-that-is-rejected"
            ),
        ],
    )
    def test_other_validation_errors_are_reported_under_the_refusal(self, declared, context, path, invalid):
        """A rendered-away field does not hide a validation error it did not
        cause, nor take the blame for it: the refusal comes first, then
        pydantic's report on the rest, found by validating again with the
        declared templates back in place."""
        with pytest.raises(StageExecutionError) as excinfo:
            render_declared(declared, context, path.split(".")[0])
        refusal, report = str(excinfo.value).split("\n", 1)
        assert refusal == f"'{path}' was declared as " + "'{{ x }}' but rendered to None"
        assert {line.split(".")[0] for line in report.splitlines()[1:] if not line.startswith(" ")} == {invalid}

    def test_invalid_value_other_than_none_keeps_the_validation_report(self):
        """Only a rendered-away field is the guard's to name: any other value
        validation rejects surfaces as pydantic reports it."""
        with pytest.raises(ValidationError, match="timeout"):
            render_declared(Request.model_validate({"url": "http://t/", "timeout": "{{ x }}"}), {"x": "slow"}, "request")

    @pytest.mark.parametrize(
        ("graphql", "context", "error"),
        [
            pytest.param(
                {"query": "{{ q }}"},
                {"q": "query A { a } query A { b }"},
                [
                    f"body.graphql.graphql.query.{GRAPHQL_QUERY_TAG}",
                    "  Value error, Invalid GraphQL query: There can be only one operation named 'A'. [type=value_error]",
                ],
                id="value-refusal",
            ),
            # As the scenario loads, the hint stays beside a type refusal: a
            # string there may have been meant as a template. This one was.
            pytest.param(
                {"query": "{ a }", "variables": "{{ v }}"},
                {"v": "x"},
                [
                    f"body.graphql.graphql.variables.`{OBJECT_TAG}`",
                    "  Input should be a valid dictionary [type=dict_type]",
                ],
                id="type-refusal",
            ),
        ],
    )
    def test_rendered_literal_refused_is_not_told_to_hold_a_template(self, graphql, context, error):
        """A value a template rendered that its field's other member refuses
        fails on that member's error alone: the template branch's "Must contain
        at least one template expression" beside it read as if the template
        had gone missing."""
        declared = Request.model_validate({"url": "http://t/", "method": "POST", "body": {"graphql": graphql}})
        with pytest.raises(ValidationError) as excinfo:
            render_declared(declared, context, "request")
        assert str(excinfo.value).splitlines()[:3] == ["1 validation error for Request", *error]

    @pytest.mark.parametrize(
        ("declared", "context"),
        [
            pytest.param(Verify(), RENDERS_NONE, id="undeclared"),
            pytest.param(Verify(status="{{ x }}"), {"x": 200}, id="rendered-to-a-value"),
            # A JSON body of null, which request_builder sends as such.
            pytest.param(Request.model_validate({"url": "http://t/", "body": {"json": "{{ x }}"}}), RENDERS_NONE, id="json-body"),
            pytest.param(Verify(description="{{ x }}"), RENDERS_NONE, id="description"),
            # Values handed on rather than fields left undeclared: a query
            # parameter (httpx sends `?q=`), a user-function kwarg.
            pytest.param(Request.model_validate({"url": "http://t/", "params": {"q": "{{ x }}"}}), RENDERS_NONE, id="param-value"),
            pytest.param(Request.model_validate({"url": "http://t/", "auth": {"name": "mod:fn", "kwargs": {"token": "{{ x }}"}}}), RENDERS_NONE, id="kwarg-value"),
            # The same in a call a template rendered whole: kwargs is a map.
            pytest.param(
                Request.model_validate({"url": "http://t/", "auth": "{{ call }}"}), {"call": {"name": "mod:fn", "kwargs": {"token": None}}}, id="kwarg-value-rendered-whole"
            ),
            # A matcher rendered whole checks only the keys it has: a left-out
            # key was never declared.
            pytest.param(Verify.model_validate({"headers": {"Location": "{{ matcher }}"}}), {"matcher": {"not_contains": "error"}}, id="matcher-rendered-whole"),
        ],
    )
    def test_none_that_means_something_passes(self, declared, context):
        render_declared(declared, context, "model")

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param(False, False, id="false"),
            pytest.param({"bearer": "tok"}, BearerAuth(bearer="tok"), id="builtin"),
            pytest.param("mod:fn", UserFunctionName("mod:fn"), id="function-name"),
        ],
    )
    @pytest.mark.parametrize("source", ["saved", "vars"])
    def test_auth_rendered_whole_takes_any_form(self, value, expected, source):
        """A request's auth written as one template is whatever it renders to,
        ``false`` included; only None is refused (above). From ``vars``, an
        object is a namespace, which stands for the object it was written as:
        refused, the stage failed where ``validate`` had passed it."""
        context = ChainMap(VarsSubstitution(vars={"a": value}).vars) if source == "vars" else {"a": value}
        rendered = render_declared(Request.model_validate({"url": "http://t/", "auth": "{{ a }}"}), context, "request")
        assert rendered.auth == expected

    @pytest.mark.parametrize(
        ("auth", "expected"),
        [
            # A whole call from a vars object: its kwargs as written inline,
            # dicts all the way down.
            pytest.param("{{ call }}", UserFunctionKwargs(name=UserFunctionName("mod:fn"), kwargs={"config": {"realm": "r"}}), id="call-whole"),
            # A kwarg a template rendered is handed on as it is, as any user
            # function's is.
            pytest.param(
                {"name": "mod:fn", "kwargs": {"config": "{{ config }}"}},
                UserFunctionKwargs(name=UserFunctionName("mod:fn"), kwargs={"config": SimpleNamespace(realm="r")}),
                id="kwarg-rendered",
            ),
            # Credentials from a vars object, put in place by the expression.
            pytest.param("{{ {'digest': login} }}", DigestAuth(digest=AuthCredentials(username="u", password="p")), id="credentials-rendered"),
        ],
    )
    def test_auth_from_vars_objects(self, auth, expected):
        context = ChainMap(
            VarsSubstitution(vars={"call": {"name": "mod:fn", "kwargs": {"config": {"realm": "r"}}}, "config": {"realm": "r"}, "login": {"username": "u", "password": "p"}}).vars
        )
        rendered = render_declared(Request.model_validate({"url": "http://t/", "auth": auth}), context, "request")
        assert rendered.auth == expected


class TestVerifyObjectsFromVars:
    """``vars`` turns each object into a SimpleNamespace, and walk()'s
    re-validation of a rendered verify step refused one where the step takes an
    object: a ``body.schema`` or a header matcher written as one template over
    ``vars`` failed the stage with pydantic's report, while the same template
    over a saved value worked. Each is now the object it was declared as; the
    checks it then makes are pinned in test_carrier.py
    (TestVerifyObjectsFromVars)."""

    @staticmethod
    def _context(**values):
        return ChainMap(VarsSubstitution(vars=values).vars)

    def test_matcher_key_set_to_none_is_refused(self):
        """As for a matcher saved whole from the response
        (``TestRenderedAwayFields``): a key it sets to None is refused."""
        context = self._context(matcher={"contains": None, "not_contains": "error"})
        with pytest.raises(StageExecutionError) as excinfo:
            render_declared(Verify.model_validate({"headers": {"Location": "{{ matcher }}"}}), context, "verify")
        assert str(excinfo.value) == "'verify.headers.Location.contains' was declared as '{{ matcher }}' but rendered to None, which would silently disable it"


class TestVerifyRenderedValueByValue:
    """A verify step renders one value at a time, a list item validated alone,
    at index 0: its errors are moved back to the item's place in the step
    (`rendering._relocated`). The step's failures, one per value that fails,
    are pinned in test_carrier.py (TestVerifyRenderedValueByValue)."""

    def test_relocated_report_reads_as_pydantic_wrote_it(self):
        """Its own errors rebuilt with their context, link included; a custom
        one, whose template is gone, with its type and message."""
        custom = PydanticCustomError("custom_kind", "Custom {text}", {"text": "words"})
        error = ValidationError.from_exception_data(
            "Verify",
            [
                {"type": "value_error", "loc": ("body", "matches", 0), "input": "(", "ctx": {"error": ValueError("Invalid regular expression")}},
                {"type": custom, "loc": ("body", "matches", 0, "x"), "input": 1},
                {"type": "missing", "loc": ("status",), "input": {}},
            ],
        )
        moved = rendering_module._relocated(error, rendering_module._relocation(("body", "matches", 0), ("body", "matches", 7)))
        assert str(moved) == str(error).replace("body.matches.0", "body.matches.7")
        assert [(e["type"], e["loc"], e["msg"]) for e in moved.errors()] == [
            ("value_error", ("body", "matches", 7), "Value error, Invalid regular expression"),
            ("custom_kind", ("body", "matches", 7, "x"), "Custom words"),
            ("missing", ("status",), "Field required"),
        ]


class TestEscapedBraces:
    """A declared model holding an escaped `\\{{` and no template is rendered
    all the same, since only rendering removes the escape: handed back as it
    was, it sent the backslash. What a template renders is final. The
    response steps' escapes, through a stage, are pinned in test_carrier.py
    (TestEscapedBraces)."""

    def test_request_with_only_an_escape_is_rendered(self):
        declared = Request.model_validate({"url": "http://t/", "headers": {"X-T": r"\{{name}}"}, "body": {"json": {"t": r"\{{ name }}"}}})
        rendered = render_declared(declared, {}, "request")
        assert (rendered.headers, rendered.body.json) == ({"X-T": "{{name}}"}, {"t": "{{ name }}"})

    def test_rendered_value_holding_an_escape_is_final(self):
        """A path a template rendered is validated again as it rendered: a
        value's `\\{{` is the value's own, never unescaped."""
        rendered = render_declared(Request.model_validate({"url": "http://t/", "body": {"binary": "{{ path }}"}}), {"path": r"files/\{{x}}.bin"}, "request")
        assert rendered.body.binary == Path(r"files/\{{x}}.bin")

    @pytest.mark.parametrize("context", [{}, {"name": "other"}], ids=["name-undefined", "name-defined"])
    @pytest.mark.parametrize(
        ("body", "path_of"),
        [
            pytest.param({"binary": r"data/\{{name}}.bin"}, lambda body: body.binary, id="binary"),
            pytest.param({"files": {"f": r"data/\{{name}}.bin"}}, lambda body: body.files["f"], id="files"),
            pytest.param({"files": {"f": {"path": r"data/\{{name}}.bin"}}}, lambda body: body.files["f"].path, id="file-object"),
            pytest.param({"multipart": {"files": {"f": [r"data/\{{name}}.bin"]}}}, lambda body: body.multipart.files["f"][0], id="multipart"),
        ],
    )
    def test_escaped_literal_path_renders_once(self, body, path_of, context):
        """A path with an escape is rendered once, as any value is: its
        `{{name}}` is the file's own braces, never a template, whatever the
        context holds. Unescaped as it loaded, it was rendered again: a stage
        failure on an undefined `name`, or another file. Its rendered text
        holds a template's syntax, so the field's template branch takes it as
        well as the path branch, and the smart union keeps it a `str`: the
        type pinned here, which the request builder reads as a path."""
        rendered = render_declared(Request.model_validate({"url": "http://t/", "method": "POST", "body": body}), context, "request")
        assert path_of(rendered.body) == "data/{{name}}.bin"

    def test_escaped_ssl_paths_render_once(self):
        """`ssl.verify` has no template branch, so it renders to a `Path`;
        a `cert` path whose rendered text holds braces stays a `str`, beside
        a `Path` rendered from a template."""
        declared = SSLConfig.model_validate({"verify": r"certs/\{{ca}}.pem", "cert": [r"certs/\{{c}}.pem", "{{ dir }}/k.pem"]})
        rendered = render_declared(declared, {"ca": "other", "c": "other", "dir": "keys"}, "ssl")
        assert (rendered.verify, rendered.cert) == (Path("certs/{{ca}}.pem"), ("certs/{{c}}.pem", Path("keys/k.pem")))
        assert isinstance(rendered.cert, tuple)
        assert [type(path) for path in (rendered.verify, *rendered.cert)] == [type(Path()), str, type(Path())]

    def test_graphql_query_with_only_an_escape_renders(self):
        """A literal judged as it renders loads, and renders to that text."""
        declared = Request.model_validate({"url": "http://t/", "method": "POST", "body": {"graphql": {"query": r'query { render(template: "Hello \{{name}}") }'}}})
        assert render_declared(declared, {}, "request").body.graphql.query == 'query { render(template: "Hello {{name}}") }'


class TestListedExpectations:
    """What a connection's rendered ``expect.match`` expects, as the
    redaction of a miss reads it (`listed_expectations`)."""

    FRAME = {"token": "s3cret", "raw": b"\x00\x01"}

    @pytest.mark.parametrize("key", ["eq", "ne", "contains", "not_contains"])
    def test_an_operand_holding_bytes_is_read_as_it_is(self, key):
        """A binary protocol's frame, an object holding bytes, rendered into
        a matcher's operand. Each operand was read by dumping the matcher,
        whose serializer takes JSON or bytes and no object holding them:
        pydantic warned, which under warnings as errors ended the stage with
        a traceback, and its warning quoted the operand, a listed key's value
        among it, where the miss beside it shows [REDACTED]."""
        match = ConnectionExpect.model_validate({"match": {"frame": {key: "{{ frame }}"}}, "timeout": 1}).match
        rendered = rendered_match(match, {"frame": self.FRAME})
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert listed_expectations(rendered, DEFAULT_REDACTION) == [{key: self.FRAME}]

    def test_a_refused_operand_posing_as_a_string_is_the_stage_s_failure(self):
        """A template on a listed key renders a value posing as a string (a
        proxy): validated again, it is refused, and the refusal is shown
        through the redaction, which read the value as the string it poses
        as and raised TypeError in the failure's place."""
        match = ConnectionExpect.model_validate({"match": {"token": "{{ t }}"}, "timeout": 1}).match
        with pytest.raises(VerificationError) as excinfo:
            rendered_match(match, {"t": Posing("s3cret")}, DEFAULT_REDACTION)
        assert "s3cret" not in str(excinfo.value)

    def test_operands_are_listed_in_the_matcher_s_order(self):
        """Each set one, whatever order the scenario wrote them in."""
        match = ConnectionExpect.model_validate({"match": {"n": {"lt": "{{ high }}", "gt": 1, "ne": 5}}, "timeout": 1}).match
        [listed] = listed_expectations(rendered_match(match, {"high": 9}), DEFAULT_REDACTION)
        assert list(listed.items()) == [("ne", 5), ("gt", 1), ("lt", 9)]
