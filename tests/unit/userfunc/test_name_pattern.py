"""Tests for function name pattern validation."""

import pytest

from pytest_httpchain.userfunc import NAME_PATTERN, UserFunctionError, import_function

# Bare (module-less) function names: the regex now rejects them, so validation
# and the importer agree on one grammar. Shared by the two tests below so the
# list stays in sync.
BARE_NAMES = [
    *["simple", "_", "_private", "__dunder", "name_", "func123", "camelCase", "ALLCAPS", "x"],
    pytest.param("a" * 100, id="100-chars"),
]


class TestBareNamesRejected:
    """Bare names fail the shared grammar — at the pattern AND the importer."""

    @pytest.mark.parametrize("name", BARE_NAMES)
    def test_bare_name_rejected_by_pattern(self, name: str):
        """The regex itself rejects module-less names."""
        assert NAME_PATTERN.fullmatch(name) is None

    @pytest.mark.parametrize("name", BARE_NAMES)
    def test_bare_name_requires_module(self, name: str):
        """import_function keeps the actionable 'use module:func' hint."""
        with pytest.raises(UserFunctionError, match="Module path is required"):
            import_function(name)


class TestExportedPatternAnchoring:
    """``userfunc.NAME_PATTERN`` is public, so callers may use ``match`` as well
    as ``fullmatch``. The pattern is anchored with ``\\A``/``\\Z`` rather than
    ``^``/``$``: ``$`` also matches just before a trailing newline, so an
    anchored-with-``$`` pattern accepted ``"mod:func\\n"`` via ``match``."""

    @pytest.mark.parametrize("name", ["mod:func\n", "mod:func garbage", "mod:func:extra", "pkg.mod:func\n"])
    def test_match_rejects_trailing_input(self, name: str):
        assert NAME_PATTERN.match(name) is None
        assert NAME_PATTERN.fullmatch(name) is None

    @pytest.mark.parametrize(
        "name",
        ["mod:func", "pkg.mod:func", "_p.m2:f_1", "z:func", pytest.param("a" * 100 + ":func", id="100-char-module")],
    )
    def test_match_and_fullmatch_agree_on_valid_names(self, name: str):
        match = NAME_PATTERN.match(name)
        assert match is not None
        assert match.groupdict() == NAME_PATTERN.fullmatch(name).groupdict()

    def test_match_rejects_leading_input(self):
        assert NAME_PATTERN.match(" mod:func") is None
        assert NAME_PATTERN.search("x mod:func") is None


class TestInvalidNamePatterns:
    """Tests for invalid function name patterns that should be rejected.

    The module path (when present) must be a well-formed dotted path: identifier
    segments separated by single dots, with no leading, trailing, or doubled dots.
    Malformed module paths like 'a..b:func' and 'mod.:func' are rejected by the
    regex (not deferred to import time).
    """

    @pytest.mark.parametrize(
        ("name", "description"),
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
            ("mod:func\n", "trailing newline"),
            ("функция", "non-ascii letters"),
            ("func🎉", "emoji"),
        ],
    )
    def test_invalid_pattern_rejected(self, name: str, description: str):
        with pytest.raises(UserFunctionError, match="Invalid function name format"):
            import_function(name)
