import pytest
from pytest_httpchain_models import FunctionsSubstitution, UserFunctionKwargs, UserFunctionName, VarsSubstitution

from pytest_httpchain.exceptions import StageExecutionError
from pytest_httpchain.utils import call_user_function, process_substitutions


# Test helper functions to be imported
def sample_func():
    return "sample_result"


def func_with_args(a, b, c=None):
    return {"a": a, "b": b, "c": c}


def add_numbers(x, y):
    return x + y


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
                functions={"my_func": "tests.unit.test_utils:sample_func"},
            ),
        ]
        result = process_substitutions(substitutions)

        assert "my_func" in result
        assert callable(result["my_func"])
        assert result["my_func"]() == "sample_result"

    def test_functions_substitution_with_kwargs(self):
        func_def = UserFunctionKwargs(
            name=UserFunctionName(root="tests.unit.test_utils:func_with_args"),
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
            FunctionsSubstitution(functions={"adder": "tests.unit.test_utils:add_numbers"}),
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


class TestCallUserFunction:
    def test_call_with_simple_name(self):
        func_call = UserFunctionName(root="tests.unit.test_utils:sample_func")
        result = call_user_function(func_call)

        assert result == "sample_result"

    def test_call_with_kwargs(self):
        func_call = UserFunctionKwargs(
            name=UserFunctionName(root="tests.unit.test_utils:func_with_args"),
            kwargs={"a": 1, "b": 2, "c": 3},
        )
        result = call_user_function(func_call)

        assert result == {"a": 1, "b": 2, "c": 3}

    def test_call_with_extra_kwargs(self):
        func_call = UserFunctionKwargs(
            name=UserFunctionName(root="tests.unit.test_utils:func_with_args"),
            kwargs={"a": 1, "b": 2},
        )
        result = call_user_function(func_call, c="extra")

        assert result == {"a": 1, "b": 2, "c": "extra"}

    def test_call_extra_kwargs_override(self):
        func_call = UserFunctionKwargs(
            name=UserFunctionName(root="tests.unit.test_utils:func_with_args"),
            kwargs={"a": 1, "b": 2, "c": "original"},
        )
        # extra_kwargs should override kwargs from UserFunctionKwargs
        result = call_user_function(func_call, c="overridden")

        assert result["c"] == "overridden"

    def test_call_simple_name_with_extra_kwargs(self):
        func_call = UserFunctionName(root="tests.unit.test_utils:func_with_args")
        result = call_user_function(func_call, a=10, b=20, c=30)

        assert result == {"a": 10, "b": 20, "c": 30}

    def test_invalid_function_call_format(self):
        # Pass something that's neither UserFunctionName nor UserFunctionKwargs
        with pytest.raises(StageExecutionError, match="Invalid function call format"):
            call_user_function("invalid_string")

    def test_invalid_function_call_none(self):
        with pytest.raises(StageExecutionError, match="Invalid function call format"):
            call_user_function(None)
