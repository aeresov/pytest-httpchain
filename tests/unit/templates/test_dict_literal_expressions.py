"""Dict literals, JSON-style literals and comprehensions inside templates."""

import pytest

from pytest_httpchain.templates import TemplatesError, get_max_comprehension_length, set_max_comprehension_length, walk


@pytest.mark.parametrize(
    ("expr", "context", "expected"),
    [
        pytest.param(
            "{{ [{'id': d, 'active': False} for d in ids] }}",
            {"ids": ["a", "b"]},
            [{"id": "a", "active": False}, {"id": "b", "active": False}],
            id="in-list-comprehension",
        ),
        pytest.param(
            # A literal's closing } needs a space before the template's }}.
            "{{ {'outer': {'inner': id, 'name': name} } }}",
            {"id": "123", "name": "test"},
            {"outer": {"inner": "123", "name": "test"}},
            id="nested",
        ),
        pytest.param(
            "{{ 'string with } character: ' + msg }}",
            {"msg": "test"},
            "string with } character: test",
            id="single-brace-inside-string",
        ),
        pytest.param(
            "{{ [dict(id=d) for d in ids] }}",
            {"ids": ["a", "b"]},
            [{"id": "a"}, {"id": "b"}],
            id="dict-constructor-in-comprehension",
        ),
        pytest.param("{{ [42 for _ in range(3)] }}", {}, [42, 42, 42], id="underscore-loop-variable"),
    ],
)
def test_evaluates(expr, context, expected):
    assert walk(expr, context) == expected


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("{{ true }}", True),
        ("{{ false }}", False),
        ("{{ null }}", None),
        ("{{ {'enabled': false, 'value': null, 'active': true} }}", {"enabled": False, "value": None, "active": True}),
    ],
)
def test_lowercase_json_literals(expr, expected):
    result = walk(expr, {})
    assert result == expected
    assert type(result) is type(expected)


@pytest.fixture
def comprehension_cap():
    """``set_max_comprehension_length``, with the process-wide cap restored after."""
    previous = get_max_comprehension_length()
    yield set_max_comprehension_length
    set_max_comprehension_length(previous)


def test_comprehension_cap_is_inclusive(comprehension_cap):
    """Exceeding the cap reads "Expression too complex", not "Invalid
    expression": IterableTooLong subclasses InvalidExpression, so the except
    order in _eval_expr is load-bearing."""
    comprehension_cap(100)
    assert walk("{{ [i for i in range(100)] }}", {}) == list(range(100))
    with pytest.raises(TemplatesError, match="Expression too complex"):
        walk("{{ [i for i in range(101)] }}", {})
