"""Tests for import_function functionality."""

import pytest
from pytest_httpchain_userfunc import UserFunctionError, import_function


class TestImportFromExplicitModule:
    """Tests for importing functions with explicit module paths."""

    def test_import_from_stdlib_module(self):
        func = import_function("json:loads")
        assert callable(func)
        assert func('{"a": 1}') == {"a": 1}

    def test_import_from_nested_module(self):
        func = import_function("os.path:join")
        assert callable(func)
        assert func("a", "b") == "a/b" or func("a", "b") == "a\\b"

    def test_import_from_deeply_nested_module(self):
        func = import_function("urllib.parse:urlencode")
        assert callable(func)
        assert func({"a": "1", "b": "2"}) in ["a=1&b=2", "b=2&a=1"]

    def test_import_from_test_helper(self):
        func = import_function("_helpers:helper_add")
        assert callable(func)
        assert func(2, 3) == 5

    def test_import_no_args_function(self):
        func = import_function("_helpers:helper_no_args")
        assert func() == "helper_result"

    def test_import_kwargs_function(self):
        func = import_function("_helpers:helper_with_kwargs")
        assert func(name="world") == "hello, world"


class TestImportErrors:
    """Tests for error handling in import_function."""

    def test_nonexistent_module_raises(self):
        with pytest.raises(UserFunctionError, match="Failed to import module") as exc_info:
            import_function("nonexistent_module_xyz:some_func")
        # The underlying ImportError text is surfaced in the message itself (H6),
        # so a missing module reads differently from one that fails to import.
        assert "No module named" in str(exc_info.value)

    def test_function_not_in_module_raises(self):
        with pytest.raises(UserFunctionError, match="not found in module"):
            import_function("json:nonexistent_function_xyz")

    def test_non_callable_raises(self):
        with pytest.raises(UserFunctionError, match="is not callable"):
            import_function("os:name")

    def test_non_callable_in_helper_raises(self):
        with pytest.raises(UserFunctionError, match="is not callable"):
            import_function("_helpers:not_callable")

    def test_bare_name_raises(self):
        with pytest.raises(UserFunctionError, match="Module path is required"):
            import_function("some_function")

    def test_invalid_format_raises(self):
        with pytest.raises(UserFunctionError, match="Invalid function name format"):
            import_function("123invalid")

    def test_non_import_error_at_module_top_level_is_wrapped(self, tmp_path, monkeypatch):
        """M33: a module whose top-level code raises a non-ImportError is wrapped
        as UserFunctionError instead of escaping as a raw traceback."""
        (tmp_path / "boom_module.py").write_text("raise RuntimeError('top-level boom')\n")
        monkeypatch.syspath_prepend(str(tmp_path))
        with pytest.raises(UserFunctionError, match="Failed to import module") as exc_info:
            import_function("boom_module:whatever")
        assert "top-level boom" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, RuntimeError)
