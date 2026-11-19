"""Unit tests for module-level helpers in models/entities.py.

Model classes themselves are covered by the sibling test modules
(test_parameters.py, test_request.py, test_stage_scenario.py, ...); this file
covers the free functions that live alongside them.
"""

from typing import Annotated, Literal

import pytest
from pydantic import Tag, ValidationError

from pytest_httpchain.models.entities import (
    CombinationsParameter,
    GraphQL,
    IndividualParameter,
    JMESPathMatcher,
    JsonBody,
    Request,
    Verify,
    XmlBody,
    _create_discriminator,
    parametrize_values_contain_template,
    validate_rendered,
    validate_rendered_expectations,
    validate_rendered_scenario_auth,
    validate_rendered_verify,
)
from tests.unit.models.helpers import FUNCTION_NAME_TAG, GRAPHQL_QUERY_TAG, OBJECT_TAG, STRING_CHECK_TAG


class TestParametrizeValuesContainTemplate:
    """Truth table for parametrize_values_contain_template().

    Contract (per its docstring): return True iff any parametrize VALUE holds a
    ``{{ }}`` template; ``ids`` are never inspected. Both parameter shapes
    (individual and combinations) must be walked — the combinations branch has
    no other test exercising it.
    """

    @pytest.mark.parametrize(
        ("parametrize", "expected"),
        [
            pytest.param(None, False, id="none"),
            pytest.param([], False, id="empty-list"),
            pytest.param(
                [IndividualParameter(individual={"x": [1, 2]})],
                False,
                id="individual-no-template",
            ),
            pytest.param(
                [IndividualParameter(individual={"x": ["{{ a }}", 2]})],
                True,
                id="individual-template-in-value",
            ),
            pytest.param(
                [IndividualParameter(individual={"x": [1, 2]}, ids=["{{ a }}", "b"])],
                False,
                id="individual-template-in-ids-only",
            ),
            pytest.param(
                [CombinationsParameter(combinations=[{"x": 1, "y": 2}])],
                False,
                id="combinations-no-template",
            ),
            pytest.param(
                [CombinationsParameter(combinations=[{"x": "{{ a }}", "y": 2}])],
                True,
                id="combinations-template-in-value",
            ),
            pytest.param(
                [
                    IndividualParameter(individual={"x": [1]}),
                    CombinationsParameter(combinations=[{"y": "{{ b }}"}]),
                ],
                True,
                id="mixed-template-in-second",
            ),
        ],
    )
    def test_truth_table(self, parametrize, expected):
        assert parametrize_values_contain_template(parametrize) is expected


class TestRenderedValidationErrors:
    """The rendered validators report a `ValidationError` as rendered text
    (`validation_errors.reported`, ``rendered``): a value a template rendered
    that another member of its union refuses, for what it says or for its
    type, is not also told to hold a template, which it was rendered from.
    Rebuilt, the error still hides its input where the validator does."""

    @staticmethod
    def _refusal(validate, *args) -> ValidationError:
        with pytest.raises(ValidationError) as excinfo:
            validate(*args)
        return excinfo.value

    def test_validate_rendered(self):
        error = self._refusal(validate_rendered, Request, {"url": "http://t/", "method": "POST", "body": {"graphql": {"query": "query A { a } query A { b }"}}})
        assert [e["loc"] for e in error.errors()] == [("body", "graphql", "graphql", "query", GRAPHQL_QUERY_TAG)]
        assert "Invalid GraphQL query: There can be only one operation named 'A'." in str(error)
        # Request hides its input: its auth and headers render credentials.
        assert "input_value" not in str(error)

    def test_validate_rendered_type_refusal(self):
        """``variables`` rendered to a string: the object branch's refusal
        alone. As the scenario loads, the hint is kept beside it, since the
        string may have been meant as a template; this one was one."""
        error = self._refusal(validate_rendered, Request, {"url": "http://t/", "method": "POST", "body": {"graphql": {"query": "{ a }", "variables": "x"}}})
        assert [(e["loc"], e["type"]) for e in error.errors()] == [(("body", "graphql", "graphql", "variables", OBJECT_TAG), "dict_type")]
        assert "Must contain at least one template expression" not in str(error)

    def test_validate_rendered_keeps_the_input_where_the_model_shows_it(self):
        error = self._refusal(validate_rendered, GraphQL, {"query": "{ user { } }"})
        assert [e["loc"] for e in error.errors()] == [("query", GRAPHQL_QUERY_TAG)]
        assert "input_value='{ user { } }'" in str(error)

    def test_validate_rendered_verify(self):
        declared = Verify.model_validate({"headers": {"Location": {"matches": "{{ p }}"}}})
        error = self._refusal(validate_rendered_verify, declared, {"headers": {"Location": {"matches": "("}}})
        # The header's own union refused the object's type, as a string: kept.
        assert [e["loc"][-2:] for e in error.errors()] == [("Location", "str"), ("matches", STRING_CHECK_TAG)]

    def test_validate_rendered_scenario_auth(self):
        error = self._refusal(validate_rendered_scenario_auth, {"name": "nocolon"})
        assert [e["loc"][-2:] for e in error.errors()] == [("name", FUNCTION_NAME_TAG)]
        assert "input_value" not in str(error)

    def test_validate_rendered_expectations(self):
        declared = {"name": JMESPathMatcher.model_validate({"matches": "{{ p }}"})}
        error = self._refusal(validate_rendered_expectations, declared, {"name": {"matches": "("}})
        assert [e["loc"][-2:] for e in error.errors()] == [("matches", STRING_CHECK_TAG)]
        assert "input_value" not in str(error)


class TestCreateDiscriminator:
    """`_create_discriminator` tells a model instance by its class, paired
    with its tag by the union's own `Tag` metadata, and refuses to build for
    a member it could never tell."""

    _BODIES = Annotated[JsonBody, Tag("json")] | Annotated[XmlBody, Tag("xml")]

    def test_tells_an_instance_by_its_class_and_an_object_by_its_key(self):
        discriminator = _create_discriminator(self._BODIES)
        assert discriminator(XmlBody(xml="<a/>")) == "xml"
        assert discriminator({"json": 1}) == "json"
        assert discriminator(1) == "(non-object: int)"

    def test_extra_tells_a_class_no_member_is(self):
        assert _create_discriminator(self._BODIES, extra={str: "json"})("x") == "json"

    def test_a_member_that_is_no_class_needs_its_value(self):
        """``Literal[False]`` is told by identity, through ``value_to_tag``;
        without a pair giving its tag, nothing would tell it."""
        members = self._BODIES | Annotated[Literal[False], Tag("false")]
        discriminator = _create_discriminator(members, value_to_tag=((False, "false"),))
        assert (discriminator(False), discriminator(0)) == ("false", "(non-object: int)")
        with pytest.raises(TypeError, match="^The union member tagged 'false' is no class, and no value_to_tag pair gives its tag$"):
            _create_discriminator(members)
