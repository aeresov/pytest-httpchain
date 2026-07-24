"""Tests for function name pattern validation."""

import pytest

from pytest_httpchain.userfunc import NAME_PATTERN, UserFunctionError, import_function

# Bare (module-less) function names: the regex now rejects them, so validation
# and the importer agree on one grammar. Shared by the two tests below so the
# list stays in sync.
BARE_NAMES = ["simple", "_", "_private", "__dunder", "name_", "func123", "camelCase", "ALLCAPS", "x"]


class TestBareNamesRejected:
    """Bare names fail the shared grammar — at the pattern AND the importer."""

    @pytest.mark.parametrize("name", BARE_NAMES)
    def test_bare_name_rejected_by_pattern(self, name: str):
        """The regex itself rejects module-less names."""
        assert NAME_PATTERN.match(name) is None

    @pytest.mark.parametrize("name", BARE_NAMES)
    def test_bare_name_requires_module(self, name: str):
        """import_function keeps the actionable 'use module:func' hint."""
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
        # wsgiref.simple_server has an underscore in a module segment.
        func = import_function("wsgiref.simple_server:demo_app")
        assert func is not None

    def test_numeric_in_module(self):
        func = import_function("base64:b64encode")
        assert callable(func)


class TestInvalidNamePatterns:
    """Tests for invalid function name patterns that should be rejected.

    The module path (when present) must be a well-formed dotted path: identifier
    segments separated by single dots, with no leading, trailing, or doubled dots.
    Malformed module paths like 'a..b:func' and 'mod.:func' are rejected by the
    regex (not deferred to import time).
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
            ("a..b:func", "doubled dot in module"),
            ("mod.:func", "trailing dot in module"),
            ("a.123.b:func", "leading digit in module segment"),
            ("a.-b:func", "hyphen in module segment"),
        ],
    )
    def test_invalid_pattern_rejected(self, name: str, description: str):
        with pytest.raises(UserFunctionError, match="Invalid function name format"):
            import_function(name)


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
