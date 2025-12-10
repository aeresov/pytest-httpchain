"""Tests for import_function functionality."""

import pytest
from pytest_httpchain_userfunc import UserFunctionError, import_function


# Module-level functions for scope testing (import_function only checks f_globals)
def module_level_function():
    """Function at module level for testing scope lookup."""
    return "module_level_result"


def module_level_adder(a, b):
    """Another module-level function for testing."""
    return a + b


class TestImportFromExplicitModule:
    """Tests for importing functions with explicit module paths."""

    def test_import_from_stdlib_module(self):
        """Import a function from a standard library module."""
        func = import_function("json:loads")
        assert callable(func)
        assert func('{"a": 1}') == {"a": 1}

    def test_import_from_nested_module(self):
        """Import a function from a nested module path."""
        func = import_function("os.path:join")
        assert callable(func)
        assert func("a", "b") == "a/b" or func("a", "b") == "a\\b"  # OS-dependent

    def test_import_from_deeply_nested_module(self):
        """Import from a deeply nested module."""
        func = import_function("urllib.parse:urlencode")
        assert callable(func)
        assert func({"a": "1", "b": "2"}) in ["a=1&b=2", "b=2&a=1"]


class TestImportErrors:
    """Tests for error handling in import_function."""

    def test_nonexistent_module_raises(self):
        """Importing from a non-existent module raises UserFunctionError."""
        with pytest.raises(UserFunctionError, match="Failed to import module"):
            import_function("nonexistent_module_xyz:some_func")

    def test_function_not_in_module_raises(self):
        """Importing a non-existent function from a valid module raises."""
        with pytest.raises(UserFunctionError, match="not found in module"):
            import_function("json:nonexistent_function_xyz")

    def test_non_callable_raises(self):
        """Importing a non-callable attribute raises UserFunctionError."""
        # os.name is a string constant, not a function
        with pytest.raises(UserFunctionError, match="is not callable"):
            import_function("os:name")

    def test_non_callable_in_conftest_skipped(self):
        """Non-callable in conftest is skipped, leading to not found error."""
        # conftest_not_callable is defined but not callable
        with pytest.raises(UserFunctionError, match="not found"):
            import_function("conftest_not_callable")


class TestImportFromConftest:
    """Tests for importing functions from conftest.py."""

    def test_import_simple_function(self):
        """Import a function defined in conftest.py."""
        func = import_function("conftest_helper")
        assert callable(func)
        assert func(2, 3) == 5

    def test_import_no_args_function(self):
        """Import a no-args function from conftest."""
        func = import_function("conftest_no_args")
        assert func() == "conftest_result"

    def test_import_kwargs_function(self):
        """Import a function with kwargs from conftest."""
        func = import_function("conftest_with_kwargs")
        assert func(name="world") == "hello, world"


class TestImportFromCurrentScope:
    """Tests for importing functions from current execution scope.

    Note: import_function only checks f_globals (module-level scope),
    not f_locals (functions defined inside other functions).
    """

    def test_import_from_module_scope(self):
        """Import a function defined at module level."""
        func = import_function("module_level_function")
        assert func() == "module_level_result"

    def test_import_module_level_with_args(self):
        """Import a module-level function that takes arguments."""
        func = import_function("module_level_adder")
        assert func(10, 20) == 30

    def test_import_from_nested_call(self):
        """Import works when called from a nested function."""

        def inner_call():
            return import_function("module_level_function")

        func = inner_call()
        assert func() == "module_level_result"

    def test_import_from_deeply_nested_call(self):
        """Import works when called from deeply nested functions."""

        def level_one():
            return level_two()

        def level_two():
            return import_function("module_level_adder")

        func = level_one()
        assert func(5, 5) == 10

    def test_function_not_found_anywhere(self):
        """Function not in module, conftest, or scope raises error."""
        with pytest.raises(UserFunctionError, match="not found in conftest or current scope"):
            import_function("completely_nonexistent_function_xyz")

    def test_locally_defined_function_not_found(self):
        """Functions defined inside other functions are NOT found (f_locals not checked)."""

        def local_only():
            return "local"

        # This should fail - import_function only checks f_globals
        with pytest.raises(UserFunctionError, match="not found"):
            import_function("local_only")


class TestImportPrecedence:
    """Tests for import resolution precedence."""

    def test_explicit_module_takes_precedence(self):
        """Explicit module path is used even if function exists in conftest."""
        # json:dumps exists in stdlib, even if we had a conftest dumps
        func = import_function("json:dumps")
        # Verify it's the json module's dumps, not a conftest version
        assert func({"a": 1}) == '{"a": 1}'

    def test_conftest_before_scope(self):
        """Conftest functions are found before scope functions."""
        # This is implicit - conftest_helper exists in conftest.py
        # and we can import it without defining it locally
        func = import_function("conftest_helper")
        assert func(1, 1) == 2
