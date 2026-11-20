"""Tests for import_adapter: a connection's adapter class, named as a user
function is, in an adapter's words."""

import collections

import pytest

from pytest_httpchain.userfunc import UserFunctionError, import_adapter


def test_returns_the_named_class():
    assert import_adapter("collections:OrderedDict") is collections.OrderedDict


@pytest.mark.parametrize(
    ("name", "message"),
    [
        pytest.param("collections:NoSuchAdapter", "Adapter 'NoSuchAdapter' not found in module 'collections'", id="not-found"),
        pytest.param("collections:namedtuple", "'collections:namedtuple' is not a class", id="a-function"),
        pytest.param("nonexistent_module_xyz:Adapter", "Failed to import module 'nonexistent_module_xyz': No module named", id="no-module"),
        pytest.param("Adapter", "Module path is required: use 'module:Adapter' format instead of 'Adapter'", id="bare-name"),
        pytest.param("a b:C", "Invalid adapter name format: a b:C", id="bad-format"),
    ],
)
def test_refusals_are_worded_for_an_adapter(name, message):
    with pytest.raises(UserFunctionError, match=message):
        import_adapter(name)


def test_pytest_exit_at_a_module_s_top_level_propagates_unwrapped(tmp_path, monkeypatch):
    """As for a user function's module (`import_function`): the stage that
    first opened the connection failed, and the session went on."""
    (tmp_path / "guarded_adapter.py").write_text("import pytest\n\npytest.exit('no device attached', returncode=7)\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(pytest.exit.Exception, match="^no device attached$") as exc_info:
        import_adapter("guarded_adapter:Adapter")
    assert exc_info.value.returncode == 7
