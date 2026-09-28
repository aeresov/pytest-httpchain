"""Unit tests for Save types: JMESPathSave, RegexSave, SubstitutionsSave, UserFunctionsSave."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from pytest_httpchain.models.entities import (
    FunctionsSubstitution,
    JMESPathSave,
    RegexCapture,
    RegexSave,
    SaveStep,
    SubstitutionsSave,
    UserFunctionKwargs,
    UserFunctionName,
    UserFunctionsSave,
    VarsSubstitution,
)


@pytest.mark.parametrize(
    ("save", "expected"),
    [
        pytest.param({"jmespath": {"result": "data"}}, JMESPathSave, id="jmespath"),
        pytest.param({"regex": {"result": "id=(\\d+)"}}, RegexSave, id="regex"),
        pytest.param({"substitutions": [{"vars": {"x": 1}}]}, SubstitutionsSave, id="substitutions"),
        pytest.param({"user_functions": ["module:func"]}, UserFunctionsSave, id="user_functions"),
    ],
)
def test_raw_save_dict_selects_model(save, expected):
    assert type(SaveStep.model_validate({"save": save}).save) is expected


@pytest.mark.parametrize(
    ("model", "kwargs"),
    [
        pytest.param(JMESPathSave, {"jmespath": {"token": "data.token"}}, id="jmespath"),
        pytest.param(RegexSave, {"regex": {"token": "token=(\\w+)"}}, id="regex"),
        pytest.param(SubstitutionsSave, {"substitutions": []}, id="substitutions"),
        pytest.param(UserFunctionsSave, {"user_functions": []}, id="user_functions"),
    ],
)
def test_description_round_trips(model, kwargs):
    assert model(**kwargs, description="Extract authentication token").description == "Extract authentication token"


class TestJMESPathSave:
    @pytest.mark.parametrize(
        "jmespath",
        [
            pytest.param({"user_id": "data.user.id", "user_name": "data.user.name", "items": "data.items[*].id"}, id="concrete"),
            pytest.param({"result": "{{ jmespath_expr }}"}, id="template"),
        ],
    )
    def test_expressions_round_trip(self, jmespath):
        assert JMESPathSave(jmespath=jmespath).jmespath == jmespath

    def test_invalid_expression_rejected(self):
        with pytest.raises(ValidationError, match="Invalid JMESPath expression"):
            JMESPathSave(jmespath={"result": "[invalid"})

    def test_key_must_be_identifier(self):
        """M24: save keys become context variable names, so a key that is not a
        valid Python identifier can never be referenced in a {{ }} expression.
        (Wiring only: the identifier rules live in test_type_validators.py.)"""
        with pytest.raises(ValidationError, match="Invalid Python variable name"):
            JMESPathSave(jmespath={"my-var": "data.value"})


class TestRegexSave:
    @pytest.mark.parametrize(
        ("entry", "expected"),
        [
            pytest.param('name="csrf" value="([^"]+)"', 'name="csrf" value="([^"]+)"', id="pattern"),
            pytest.param("Order #(?P<id>\\d+)", "Order #(?P<id>\\d+)", id="pattern-named-group"),
            # Partial templates, as a header matcher's pattern takes them.
            pytest.param("id={{ prefix }}(\\d+)", "id={{ prefix }}(\\d+)", id="pattern-template"),
            pytest.param({"pattern": "Order #(?P<id>\\d+)", "group": "id"}, RegexCapture(pattern="Order #(?P<id>\\d+)", group="id"), id="group-name"),
            pytest.param({"pattern": "(a)(b)", "group": 2}, RegexCapture(pattern="(a)(b)", group=2), id="group-number"),
            # 0 is the whole match, which every pattern has.
            pytest.param({"pattern": "ab", "group": 0}, RegexCapture(pattern="ab", group=0), id="group-zero"),
            pytest.param({"pattern": "id=(\\d+)", "all": True}, RegexCapture(pattern="id=(\\d+)", all=True), id="all"),
            # Unknown until rendered: checked then (test_response_steps, test_carrier).
            pytest.param({"pattern": "{{ p }}", "group": 5}, RegexCapture(pattern="{{ p }}", group=5), id="group-of-a-templated-pattern"),
            pytest.param(
                {"pattern": "(a)", "group": "{{ g }}", "all": "{{ every }}"},
                RegexCapture(pattern="(a)", group="{{ g }}", all="{{ every }}"),
                id="templated-group-and-all",
            ),
            # A template over `vars` renders an object as a namespace, which
            # stands for the capture object, as a header matcher's does.
            pytest.param(SimpleNamespace(pattern="(a)", all=True), RegexCapture(pattern="(a)", all=True), id="namespace-is-a-capture"),
        ],
    )
    def test_entries_round_trip(self, entry, expected):
        assert RegexSave.model_validate({"regex": {"v": entry}}).regex == {"v": expected}

    def test_capture_defaults(self):
        """Unset, the group is chosen from the pattern (group 1 if it has any,
        else the whole match), and only the first match is saved."""
        capture = RegexCapture(pattern="(a)")
        assert (capture.group, capture.all) == (None, False)

    @pytest.mark.parametrize("entry", [pytest.param("(", id="pattern"), pytest.param({"pattern": "("}, id="capture")])
    def test_invalid_pattern_rejected(self, entry):
        with pytest.raises(ValidationError, match="Invalid regular expression"):
            RegexSave.model_validate({"regex": {"v": entry}})

    @pytest.mark.parametrize(
        ("pattern", "group", "message"),
        [
            pytest.param("(a)", 2, "regex '(a)' has no group 2 (it has 1 group; 0 is the whole match)", id="number-past-the-last"),
            pytest.param("a", 1, "regex 'a' has no group 1 (it has no groups; 0 is the whole match)", id="number-without-groups"),
            pytest.param("(a)(b)", 3, "regex '(a)(b)' has no group 3 (it has 2 groups; 0 is the whole match)", id="number-past-several"),
            pytest.param("(?P<a>x)(?P<b>y)", "c", "regex '(?P<a>x)(?P<b>y)' has no group named 'c' (its named groups: 'a', 'b')", id="name"),
            pytest.param("(x)", "id", "regex '(x)' has no group named 'id' (it has no named groups)", id="name-without-named-groups"),
        ],
    )
    def test_group_a_literal_pattern_lacks_fails_at_load(self, pattern, group, message):
        """Found without a response: ``validate`` reports it, as the runtime
        would fail the stage on it."""
        with pytest.raises(ValidationError) as excinfo:
            RegexCapture(pattern=pattern, group=group)
        assert [error["msg"] for error in excinfo.value.errors()] == [f"Value error, {message}"]

    @pytest.mark.parametrize(
        "group",
        [
            pytest.param(-1, id="negative"),
            pytest.param(True, id="bool"),
            pytest.param(1.0, id="float"),
            # A number is written as one: text is a name, and a name is an identifier.
            pytest.param("1", id="numeric-text"),
            pytest.param("my-group", id="not-an-identifier"),
        ],
    )
    def test_group_is_a_number_a_name_or_a_template(self, group):
        with pytest.raises(ValidationError):
            RegexCapture(pattern="(?P<g>a)", group=group)

    @pytest.mark.parametrize("value", ["yes", "true", None])
    def test_all_is_true_false_or_a_template(self, value):
        with pytest.raises(ValidationError):
            RegexCapture.model_validate({"pattern": "a", "all": value})

    def test_capture_needs_its_pattern(self):
        """An object is a capture, whatever its keys: its errors are the
        capture's alone, not a pattern's beside them."""
        with pytest.raises(ValidationError) as excinfo:
            RegexSave.model_validate({"regex": {"v": {"group": 1}}})
        assert [(error["loc"], error["type"]) for error in excinfo.value.errors()] == [(("regex", "v", "capture", "pattern"), "missing")]

    def test_key_must_be_identifier(self):
        with pytest.raises(ValidationError, match="Invalid Python variable name"):
            RegexSave(regex={"my-var": "(a)"})


def test_substitutions_save_normalizes_mapping_form():
    """Same ``Substitutions`` type as Stage/Scenario (list-or-mapping input)."""
    save = SubstitutionsSave.model_validate(
        {"substitutions": {"constants": {"vars": {"x": 1}}, "computed": {"functions": {"y": "mod:func"}}}},
    )
    assert save.substitutions == [VarsSubstitution(vars={"x": 1}), FunctionsSubstitution(functions={"y": UserFunctionName("mod:func")})]


def test_user_functions_save_accepts_name_and_kwargs_forms():
    save = UserFunctionsSave.model_validate({"user_functions": ["simple:func", {"name": "complex:func", "kwargs": {"arg": "value"}}]})
    assert save.user_functions == [
        UserFunctionName("simple:func"),
        UserFunctionKwargs(name=UserFunctionName("complex:func"), kwargs={"arg": "value"}),
    ]
