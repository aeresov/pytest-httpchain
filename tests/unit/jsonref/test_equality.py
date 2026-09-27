"""json_equal: equality as JSON means it, the one definition the sibling
merge, ``verify.jmespath`` and the validator's checks on it share."""

import pytest

from pytest_httpchain.jsonref import json_equal


@pytest.mark.parametrize(
    ("a", "b", "equal"),
    [
        pytest.param(True, 1, False, id="true-vs-1"),
        pytest.param(False, 0, False, id="false-vs-0"),
        pytest.param(0, False, False, id="0-vs-false"),
        pytest.param(True, True, True, id="true-vs-true"),
        pytest.param(1, 1.0, True, id="int-vs-float"),
        pytest.param(1, 1.5, False, id="int-vs-other-float"),
        pytest.param("1", 1, False, id="text-vs-number"),
        pytest.param(None, None, True, id="null"),
        pytest.param(None, 0, False, id="null-vs-0"),
        pytest.param(None, "", False, id="null-vs-empty"),
        pytest.param([1, [2.0, {"a": True}]], [1.0, [2, {"a": True}]], True, id="nested"),
        pytest.param([1, [2, {"a": True}]], [1, [2, {"a": 1}]], False, id="nested-bool-vs-1"),
        pytest.param([1, 2], [1, 2, 3], False, id="array-length"),
        pytest.param([1, 2], [2, 1], False, id="array-order"),
        pytest.param({"a": 1, "b": 2}, {"b": 2.0, "a": 1}, True, id="object-key-order"),
        pytest.param({"a": 1}, {"a": 1, "b": 2}, False, id="object-keys"),
        pytest.param({"a": None}, {}, False, id="null-member-vs-none"),
        pytest.param([], {}, False, id="array-vs-object"),
    ],
)
def test_json_equal(a, b, equal):
    assert json_equal(a, b) is equal
    assert json_equal(b, a) is equal
