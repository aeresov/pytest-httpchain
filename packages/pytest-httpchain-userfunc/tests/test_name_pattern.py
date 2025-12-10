"""Tests for function name pattern validation."""

import pytest
from pytest_httpchain_userfunc import UserFunctionError, import_function


# Module-level functions for testing valid patterns
def simple():
    """Simple function."""
    return "simple"


def _():
    """Underscore-only function."""
    return "underscore"


def _private():
    """Private function with underscore prefix."""
    return "private"


def __dunder():
    """Function with double underscore prefix."""
    return "dunder"


def name_():
    """Function with underscore suffix."""
    return "suffix"


def func123():
    """Function with numeric suffix."""
    return "numeric"


def camelCase():
    """CamelCase function."""
    return "camel"


def ALLCAPS():
    """ALL CAPS function."""
    return "caps"


def x():
    """Single character function."""
    return "x"


class TestValidNamePatterns:
    """Tests for valid function name patterns."""

    def test_simple_name(self):
        """Simple function name without module."""
        func = import_function("simple")
        assert func() == "simple"

    def test_underscore_only_name(self):
        """Function name that is just underscores."""
        func = import_function("_")
        assert func() == "underscore"

    def test_underscore_prefix(self):
        """Function name with underscore prefix."""
        func = import_function("_private")
        assert func() == "private"

    def test_double_underscore_prefix(self):
        """Function name with double underscore prefix."""
        func = import_function("__dunder")
        assert func() == "dunder"

    def test_underscore_suffix(self):
        """Function name with underscore suffix."""
        func = import_function("name_")
        assert func() == "suffix"

    def test_numeric_suffix(self):
        """Function name with numeric suffix."""
        func = import_function("func123")
        assert func() == "numeric"

    def test_mixed_case(self):
        """Function name with mixed case."""
        func = import_function("camelCase")
        assert func() == "camel"

    def test_all_caps(self):
        """Function name in all caps."""
        func = import_function("ALLCAPS")
        assert func() == "caps"


class TestValidModulePatterns:
    """Tests for valid module:function patterns."""

    def test_simple_module(self):
        """Simple module:function pattern."""
        func = import_function("json:loads")
        assert callable(func)

    def test_nested_module(self):
        """Nested module with dots."""
        func = import_function("os.path:join")
        assert callable(func)

    def test_deeply_nested_module(self):
        """Deeply nested module path."""
        func = import_function("urllib.parse:urlencode")
        assert callable(func)

    def test_underscore_in_module(self):
        """Module name with underscore."""
        # collections.abc is a valid module
        func = import_function("collections.abc:Callable")
        assert func is not None

    def test_numeric_in_module(self):
        """Module name with numbers."""
        # Using a stdlib module - base64 has numbers
        func = import_function("base64:b64encode")
        assert callable(func)


class TestInvalidNamePatterns:
    """Tests for invalid function name patterns that should be rejected.

    Note: The regex pattern allows dots anywhere in the module name (including
    consecutive dots and trailing dots), so patterns like 'a..b:func' are valid
    per the regex but may fail at import time.
    """

    @pytest.mark.parametrize(
        "name,description",
        [
            ("", "empty string"),
            ("123func", "leading digit in function"),
            ("123:func", "leading digit in module"),
            ("mod:123func", "leading digit in function after module"),
            ("my-func", "hyphen in function name"),
            ("my-module:func", "hyphen in module name"),
            ("mod:my-func", "hyphen in function after module"),
            ("func!", "exclamation mark"),
            ("func@name", "at symbol"),
            ("func#name", "hash symbol"),
            ("func name", "space in name"),
            ("mod:func name", "space in function after module"),
            ("mod ule:func", "space in module"),
            (":func", "empty module before colon"),
            ("mod:", "empty function after colon"),
            (":", "just colon"),
            ("mod::func", "double colon"),
            (".mod:func", "leading dot in module"),
        ],
    )
    def test_invalid_pattern_rejected(self, name: str, description: str):
        """Invalid patterns are rejected with UserFunctionError."""
        with pytest.raises(UserFunctionError, match="Invalid function name format"):
            import_function(name)

    def test_double_dot_valid_but_import_fails(self):
        """Double dots are valid per regex but fail at import."""
        # The regex accepts this, but import will fail
        with pytest.raises(UserFunctionError, match="Failed to import module"):
            import_function("a..b:func")

    def test_trailing_dot_valid_but_import_fails(self):
        """Trailing dot is valid per regex but fails at import."""
        with pytest.raises(UserFunctionError, match="Failed to import module"):
            import_function("mod.:func")


class TestEdgeCases:
    """Edge case tests for name patterns."""

    def test_single_char_name(self):
        """Single character function name."""
        func = import_function("x")
        assert func() == "x"

    def test_single_char_module(self):
        """Single character module name (if it existed)."""
        # We can't easily test this with stdlib, but the pattern allows it
        # This test verifies the pattern doesn't reject it
        with pytest.raises(UserFunctionError, match="Failed to import module"):
            import_function("z:func")  # Module 'z' doesn't exist, but format is valid

    def test_very_long_name(self):
        """Very long function name - tests regex performance."""
        long_name = "a" * 100
        # Pattern should accept it, but function won't exist
        with pytest.raises(UserFunctionError, match="not found"):
            import_function(long_name)

    def test_unicode_rejected(self):
        """Unicode characters in name are rejected."""
        with pytest.raises(UserFunctionError, match="Invalid function name format"):
            import_function("функция")  # Russian word for "function"

    def test_emoji_rejected(self):
        """Emoji in name is rejected."""
        with pytest.raises(UserFunctionError, match="Invalid function name format"):
            import_function("func🎉")
