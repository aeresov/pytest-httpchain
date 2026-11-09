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
