"""Tests for function name pattern validation."""

import pytest
from pytest_httpchain_userfunc import NAME_PATTERN, UserFunctionError, import_function


class TestValidNamePatterns:
    """Tests for valid function name patterns (regex acceptance)."""

    @pytest.mark.parametrize(
        "name",
        ["simple", "_", "_private", "__dunder", "name_", "func123", "camelCase", "ALLCAPS", "x"],
    )
    def test_bare_name_matches_pattern(self, name: str):
        """Bare function names are accepted by the regex pattern."""
        assert NAME_PATTERN.match(name) is not None

    @pytest.mark.parametrize(
        "name",
        ["simple", "_", "_private", "__dunder", "name_", "func123", "camelCase", "ALLCAPS", "x"],
    )
    def test_bare_name_requires_module(self, name: str):
        """Bare function names raise an error requiring module path."""
        with pytest.raises(UserFunctionError, match="Module path is required"):
            import_function(name)


class TestValidModulePatterns:
    """Tests for valid module:function patterns."""

    def test_simple_module(self):
        func = import_function("json:loads")
        assert callable(func)

    def test_nested_module(self):
        func = import_function("os.path:join")
        assert callable(func)

    def test_deeply_nested_module(self):
        func = import_function("urllib.parse:urlencode")
        assert callable(func)

    def test_underscore_in_module(self):
        func = import_function("collections.abc:Callable")
        assert func is not None

    def test_numeric_in_module(self):
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
        with pytest.raises(UserFunctionError, match="Invalid function name format"):
            import_function(name)

    def test_double_dot_valid_but_import_fails(self):
        with pytest.raises(UserFunctionError, match="Failed to import module"):
            import_function("a..b:func")

    def test_trailing_dot_valid_but_import_fails(self):
        with pytest.raises(UserFunctionError, match="Failed to import module"):
            import_function("mod.:func")


class TestEdgeCases:
    """Edge case tests for name patterns."""

    def test_single_char_module(self):
        with pytest.raises(UserFunctionError, match="Failed to import module"):
            import_function("z:func")

    def test_very_long_name(self):
        long_name = "a" * 100
        with pytest.raises(UserFunctionError, match="Module path is required"):
            import_function(long_name)

    def test_very_long_module_name(self):
        long_name = "a" * 100 + ":func"
        with pytest.raises(UserFunctionError, match="Failed to import module"):
            import_function(long_name)

    def test_unicode_rejected(self):
        with pytest.raises(UserFunctionError, match="Invalid function name format"):
            import_function("функция")

    def test_emoji_rejected(self):
        with pytest.raises(UserFunctionError, match="Invalid function name format"):
            import_function("func🎉")
