import pytest

from pytest_httpchain.errors import StageExecutionError
from pytest_httpchain.models import FunctionsSubstitution, UserFunctionKwargs, UserFunctionName, VarsSubstitution
from pytest_httpchain.templates import TemplatesError
from pytest_httpchain.utils import process_substitutions

# The functions these tests import live in a module of their own; see its
# docstring for why they are not defined here.
HELPERS = "tests.unit.utils_test_helpers"


class TestProcessSubstitutions:
    def test_empty_substitutions(self):
        result = process_substitutions([])
        assert result == {}

    def test_vars_substitution_simple(self):
        substitutions = [
            VarsSubstitution(vars={"name": "Alice", "age": 30}),
        ]
        result = process_substitutions(substitutions)

        assert result == {"name": "Alice", "age": 30}

    def test_vars_substitution_with_template(self):
        substitutions = [
            VarsSubstitution(vars={"base": 10}),
            VarsSubstitution(vars={"doubled": "{{ base * 2 }}"}),
        ]
        result = process_substitutions(substitutions)

        assert result["base"] == 10
        assert result["doubled"] == 20

    def test_vars_substitution_with_context(self):
        context = {"existing": "value"}
        substitutions = [
            VarsSubstitution(vars={"new": "{{ existing }}_appended"}),
        ]
        result = process_substitutions(substitutions, context)

        assert result["new"] == "value_appended"

    def test_vars_substitution_chaining(self):
        substitutions = [
            VarsSubstitution(vars={"first": 1}),
            VarsSubstitution(vars={"second": "{{ first + 1 }}"}),
            VarsSubstitution(vars={"third": "{{ second + 1 }}"}),
        ]
        result = process_substitutions(substitutions)

        assert result["first"] == 1
        assert result["second"] == 2
        assert result["third"] == 3

    def test_vars_substitution_complex_types(self):
        substitutions = [
            VarsSubstitution(vars={"items": [1, 2, 3], "data": {"key": "value"}}),
        ]
        result = process_substitutions(substitutions)

        assert result["items"] == [1, 2, 3]
        # Dicts are converted to SimpleNamespace by the models
        assert result["data"].key == "value"

    def test_functions_substitution_simple_name(self):
        substitutions = [
            FunctionsSubstitution(
                functions={"my_func": UserFunctionName(f"{HELPERS}:sample_func")},
            ),
        ]
        result = process_substitutions(substitutions)

        assert "my_func" in result
        assert callable(result["my_func"])
        assert result["my_func"]() == "sample_result"

    def test_functions_substitution_with_kwargs(self):
        func_def = UserFunctionKwargs(
            name=UserFunctionName(f"{HELPERS}:func_with_args"),
            kwargs={"a": 1, "b": 2},
        )
        substitutions = [
            FunctionsSubstitution(functions={"my_func": func_def}),
        ]
        result = process_substitutions(substitutions)

        assert "my_func" in result
        assert callable(result["my_func"])
        # Wrapped function should have default kwargs
        assert result["my_func"](c=3) == {"a": 1, "b": 2, "c": 3}

    def test_mixed_substitutions(self):
        substitutions = [
            VarsSubstitution(vars={"x": 5, "y": 10}),
            FunctionsSubstitution(functions={"adder": UserFunctionName(f"{HELPERS}:add_numbers")}),
        ]
        result = process_substitutions(substitutions)

        assert result["x"] == 5
        assert result["y"] == 10
        assert callable(result["adder"])
        assert result["adder"](3, 4) == 7

    def test_vars_override_previous(self):
        substitutions = [
            VarsSubstitution(vars={"key": "first"}),
            VarsSubstitution(vars={"key": "second"}),
        ]
        result = process_substitutions(substitutions)

        assert result["key"] == "second"

    def test_functions_substitution_templated_name_rendered(self):
        """A templated import name — the form the model itself advertises
        ('module.{{ submodule_name }}:funcname') — resolves against the current
        context at seed time; nothing downstream ever sees a context."""
        substitutions = [
            VarsSubstitution(vars={"mod": HELPERS}),
            FunctionsSubstitution(functions={"my_func": UserFunctionName("{{ mod }}:sample_func")}),
        ]
        result = process_substitutions(substitutions)

        assert result["my_func"]() == "sample_result"

    def test_functions_substitution_templated_name_with_kwargs_rendered(self):
        func_def = UserFunctionKwargs(
            name=UserFunctionName("{{ mod }}:func_with_args"),
            kwargs={"a": 1, "b": 2},
        )
        substitutions = [
            FunctionsSubstitution(functions={"my_func": func_def}),
        ]
        result = process_substitutions(substitutions, {"mod": HELPERS})

        assert result["my_func"](c=3) == {"a": 1, "b": 2, "c": 3}

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
        substitutions = [
            VarsSubstitution(vars={"first": 1, "second": "{{ first + 1 }}"}),
        ]

        with pytest.raises(TemplatesError, match="Undefined variable"):
            process_substitutions(substitutions)

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
