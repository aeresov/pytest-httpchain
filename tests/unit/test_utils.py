import pytest

from pytest_httpchain.errors import StageExecutionError
from pytest_httpchain.models import FunctionsSubstitution, UserFunctionKwargs, UserFunctionName, VarsSubstitution
from pytest_httpchain.templates import TemplatesError
from pytest_httpchain.utils import make_marker, process_substitutions

# The functions these tests import live in a module of their own; see its
# docstring for why they are not defined here.
HELPERS = "tests.unit.utils_test_helpers"


class TestProcessSubstitutions:
    @pytest.mark.parametrize(
        ("substitutions", "expected"),
        [
            pytest.param([], {}, id="empty"),
            pytest.param([VarsSubstitution(vars={"name": "Alice", "items": [1, 2, 3]})], {"name": "Alice", "items": [1, 2, 3]}, id="literals"),
            pytest.param(
                [VarsSubstitution(vars={"first": 1}), VarsSubstitution(vars={"second": "{{ first + 1 }}"}), VarsSubstitution(vars={"third": "{{ second + 1 }}"})],
                {"first": 1, "second": 2, "third": 3},
                id="later-steps-see-earlier",
            ),
            pytest.param([VarsSubstitution(vars={"key": "first"}), VarsSubstitution(vars={"key": "second"})], {"key": "second"}, id="later-step-overrides"),
        ],
    )
    def test_vars_steps(self, substitutions, expected):
        assert process_substitutions(substitutions) == expected

    def test_step_sees_prior_steps_over_context(self):
        """Each step reads the incoming context through the names seeded so far,
        which shadow it; what comes back is a plain dict of only this call's own
        names, whatever the layering underneath."""
        context = {"name": "outer", "other": "kept"}
        substitutions = [
            VarsSubstitution(vars={"name": "inner"}),
            VarsSubstitution(vars={"echo": "{{ name }}", "passthrough": "{{ other }}"}),
        ]
        result = process_substitutions(substitutions, context)

        assert type(result) is dict
        assert result == {"name": "inner", "echo": "inner", "passthrough": "kept"}

    def test_step_does_not_see_its_own_names(self):
        """A step sees PRIOR steps only — the boundary `scoping` validates
        against, which a single mutating context layer would erase."""
        with pytest.raises(TemplatesError, match="Undefined variable"):
            process_substitutions([VarsSubstitution(vars={"first": 1, "second": "{{ first + 1 }}"})])

    @pytest.mark.parametrize(
        ("function", "args", "expected"),
        [
            pytest.param(UserFunctionName(f"{HELPERS}:add_numbers"), (3, 4), 7, id="name"),
            pytest.param(UserFunctionKwargs(name=UserFunctionName(f"{HELPERS}:func_with_args"), kwargs={"a": 1, "b": 2}), (), {"a": 1, "b": 2, "c": None}, id="declared-kwargs"),
            # The form the model advertises ('module.{{ submodule }}:fn'): it
            # resolves against the context at seed time, since nothing
            # downstream ever sees a context.
            pytest.param(UserFunctionName("{{ mod }}:add_numbers"), (3, 4), 7, id="templated-name"),
            pytest.param(
                UserFunctionKwargs(name=UserFunctionName("{{ mod }}:func_with_args"), kwargs={"a": 1, "b": 2}), (), {"a": 1, "b": 2, "c": None}, id="templated-name-with-kwargs"
            ),
            # Declared kwargs are passed through unrendered — the runtime half of
            # HTTPCHAIN030, which reports a template there as dead text.
            pytest.param(
                UserFunctionKwargs(name=UserFunctionName(f"{HELPERS}:func_with_args"), kwargs={"a": "{{ mod }}", "b": 2}),
                (),
                {"a": "{{ mod }}", "b": 2, "c": None},
                id="kwargs-not-rendered",
            ),
        ],
    )
    def test_functions_step_binds_a_callable(self, function, args, expected):
        result = process_substitutions([FunctionsSubstitution(functions={"fn": function})], {"mod": HELPERS})
        assert result["fn"](*args) == expected

    def test_templated_name_resolving_to_non_string_is_rejected(self):
        """A complete `{{ }}` import name preserves the value's type, so it can
        come back as something that is not a name at all. That must be a clean
        StageExecutionError naming the type, not a confusing failure deeper in
        the importer.
        """
        substitutions = [
            VarsSubstitution(vars={"ref": [1, 2]}),
            FunctionsSubstitution(functions={"my_func": UserFunctionName("{{ ref }}")}),
        ]
        with pytest.raises(StageExecutionError, match="must resolve to a string, got list"):
            process_substitutions(substitutions)


@pytest.mark.parametrize(
    ("mark_str", "expected"),
    [
        ("slow", ("slow", (), {})),
        ('skip(reason="flaky upstream")', ("skip", (), {"reason": "flaky upstream"})),
        ('xfail(True, reason="known")', ("xfail", (True,), {"reason": "known"})),
    ],
)
def test_make_marker(mark_str, expected):
    mark = make_marker(mark_str).mark
    assert (mark.name, mark.args, mark.kwargs) == expected


@pytest.mark.parametrize(
    ("mark_str", "error"),
    [
        ("foo.bar", ValueError),  # attribute access is not a marker expression
        ("skip(reason=some_name)", ValueError),  # only literal arguments
        # Unpacking is rejected either way; `**` used to be silently dropped,
        # turning a strict xfail into a non-strict one.
        ('skip(*["x"])', ValueError),
        ('xfail(**{"strict": True})', ValueError),
        ("skip(", SyntaxError),
    ],
)
def test_make_marker_rejects_non_literal_expressions(mark_str, error):
    with pytest.raises(error):
        make_marker(mark_str)
