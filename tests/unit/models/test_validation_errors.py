"""A model's ValidationError as an author reads it (models/validation_errors.py).

A field that takes a literal or a template refuses a value neither takes with
one error per branch; where the literal branch judged the string itself, the
template branch's "Must contain at least one template expression" goes.
Errors come from real pydantic validation here, so the rule is pinned against
the error structure pydantic produces, not one written by hand.
"""

from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic_core import PydanticCustomError

from pytest_httpchain.models.types import (
    BaseUrlStr,
    GraphQLName,
    GraphQLQuery,
    NamespaceOrDict,
    PartialTemplateStr,
    ProxyUrlStr,
    UnquotedPartialTemplateStr,
    VariableName,
)
from pytest_httpchain.models.validation_errors import reported, validation_error, without_template_hints

_QUERY = "function-after[validate_graphql_query(), str]"
_NAME = "function-after[validate_graphql_name(), str]"
_TEMPLATE = "function-after[validate_partial_template_str(), str]"
_OBJECT = "function-before[convert_namespace_to_dict(), dict[str,json-or-python[json=any,python=tagged-union[list[...],dict[str,...],str,bool,int,float,none]]]]"


class _Siblings(BaseModel):
    """Two fields whose errors share their parent's location: one a template
    alone, the other a literal alone, so neither error has a member tag."""

    template: PartialTemplateStr
    name: GraphQLName


class _Body(BaseModel):
    query: GraphQLQuery | PartialTemplateStr
    variables: NamespaceOrDict | PartialTemplateStr
    operation_name: GraphQLName | PartialTemplateStr


def _refusal(annotation: Any, value: Any) -> ValidationError:
    adapter: TypeAdapter[Any] = TypeAdapter(annotation)
    with pytest.raises(ValidationError) as excinfo:
        adapter.validate_python(value)
    return excinfo.value


def _where(errors: list) -> list[tuple]:
    return [(*error["loc"], error["type"]) for error in errors]


@pytest.mark.parametrize(
    ("annotation", "value", "kept"),
    [
        # A string the literal branch takes as a string and refuses for what
        # it says: its error alone.
        pytest.param(GraphQLQuery | PartialTemplateStr, "query { user { } }", [(_QUERY, "value_error")], id="literal-refuses-the-value"),
        pytest.param(ProxyUrlStr | UnquotedPartialTemplateStr, "ftp://proxy.test", [("function-wrap[validate_proxy_url()]", "value_error")], id="unquoted-template-branch"),
        # pydantic's own check of a URL's text refuses the value, not its type.
        pytest.param(BaseUrlStr | UnquotedPartialTemplateStr, "ftp://x.test", [("function-wrap[validate_base_url()]", "url_scheme")], id="url-scheme"),
        # A string where the literal branch takes an object may have been meant
        # as a template: the hint stays.
        pytest.param(
            NamespaceOrDict | PartialTemplateStr,
            "id=1",
            [(_OBJECT, "dict_type"), (_TEMPLATE, "value_error")],
            id="literal-refuses-the-type",
        ),
        # The template branch's complaint is another: an empty expression.
        pytest.param(GraphQLQuery | PartialTemplateStr, "{ a(x: {{ }}) }", [(_QUERY, "value_error"), (_TEMPLATE, "value_error")], id="empty-expression"),
        # A key's error shares the entry's location, as a member's does, but is
        # no branch of the value: the value's type refusal keeps its hint.
        pytest.param(
            dict[VariableName, NamespaceOrDict | PartialTemplateStr],
            {"a-b": "id=1"},
            [("a-b", "[key]", "value_error"), ("a-b", _OBJECT, "dict_type"), ("a-b", _TEMPLATE, "value_error")],
            id="key-error-is-no-member",
        ),
        # Two fields alone, their errors at their parent's location and one
        # more: neither is a branch of the other.
        pytest.param(_Siblings, {"template": "plain", "name": "1st"}, [("template", "value_error"), ("name", "value_error")], id="sibling-fields"),
        # Each field of a model judged on its own: a literal one refuses keeps
        # its sibling's hint, which a string where an object goes earned.
        pytest.param(
            _Body,
            {"query": "{ }", "variables": "id=1", "operation_name": "1st"},
            [
                ("query", _QUERY, "value_error"),
                ("variables", _OBJECT, "dict_type"),
                ("variables", _TEMPLATE, "value_error"),
                ("operation_name", _NAME, "value_error"),
            ],
            id="sibling-unions",
        ),
        # A list's items are fields of their own, by index.
        pytest.param(list[GraphQLQuery | PartialTemplateStr], ["{ a }", "{ }"], [(1, _QUERY, "value_error")], id="list-item"),
    ],
)
def test_without_template_hints(annotation, value, kept):
    assert _where(without_template_hints(_refusal(annotation, value).errors())) == kept


def test_the_hint_is_told_apart_by_its_words_where_its_context_is_gone():
    """A wrap validator re-raising errors as custom ones (a jmespath matcher's
    hint does) keeps their type and message, not their context."""
    errors = _refusal(GraphQLQuery | PartialTemplateStr, "{ }").errors()
    for error in errors:
        error.pop("url", None)
    rebuilt = validation_error("query", errors)
    assert [error.get("ctx") for error in rebuilt.errors()] == [None, None]
    assert _where(without_template_hints(rebuilt.errors())) == [(_QUERY, "value_error")]


class TestValidationError:
    def test_reads_as_pydantic_wrote_it(self):
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
        rebuilt = validation_error(error.title, error.errors())
        assert str(rebuilt) == str(error)
        assert [(e["type"], e["loc"], e["msg"]) for e in rebuilt.errors()] == [(e["type"], e["loc"], e["msg"]) for e in error.errors()]

    def test_hides_the_input_when_asked(self):
        """As the ``hide_input_in_errors`` of the model that raised it does: a
        client's proxy URL carries credentials."""
        error = _refusal(ProxyUrlStr | UnquotedPartialTemplateStr, "ftp://user:secret@proxy.test")
        assert "secret" not in str(validation_error(error.title, error.errors(), hide_input=True))
        assert "secret" in str(validation_error(error.title, error.errors()))


class TestReported:
    def test_is_the_error_itself_when_nothing_goes(self):
        error = _refusal(NamespaceOrDict | PartialTemplateStr, "id=1")
        assert reported(error) is error

    def test_rebuilt_without_the_hint(self):
        error = _refusal(GraphQLQuery | PartialTemplateStr, "{ }")
        assert _where(reported(error).errors()) == [(_QUERY, "value_error")]
        assert reported(error).title == error.title

    def test_rebuilt_hiding_the_input_when_asked(self):
        error = _refusal(ProxyUrlStr | UnquotedPartialTemplateStr, "ftp://user:secret@proxy.test")
        assert "secret" not in str(reported(error, hide_input=True))
        assert _where(reported(error, hide_input=True).errors()) == [("function-wrap[validate_proxy_url()]", "value_error")]
