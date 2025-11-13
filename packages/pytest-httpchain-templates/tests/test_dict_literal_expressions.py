"""Test dict literal expressions in templates."""

import simpleeval
from pytest_httpchain_templates.substitution import walk


def test_dict_literal_in_list_comprehension():
    """Test that dict literals work in list comprehensions."""
    test_device_ids = ["device1", "device2", "device3"]
    context = {"test_device_ids": test_device_ids}

    # Using dict literal syntax
    expr = "{{ [{'kekeke': test_device_id, 'bububu': False} for test_device_id in test_device_ids] }}"
    result = walk(expr, context)

    expected = [
        {"kekeke": "device1", "bububu": False},
        {"kekeke": "device2", "bububu": False},
        {"kekeke": "device3", "bububu": False},
    ]
    assert result == expected
    assert isinstance(result, list)


def test_dict_literal_with_variable():
    """Test that dict literals work with variables."""
    context = {"value": "test123"}

    expr = "{{ {'key': value} }}"
    result = walk(expr, context)

    assert result == {"key": "test123"}
    assert isinstance(result, dict)


def test_nested_dict_literals():
    """Test nested dict literals."""
    context = {"id": "123", "name": "test"}

    # Note: When dict literal ends with }}, add a space before the closing }}
    expr = "{{ {'outer': {'inner': id, 'name': name} } }}"
    result = walk(expr, context)

    assert result == {"outer": {"inner": "123", "name": "test"}}
    assert isinstance(result, dict)


def test_string_with_brace():
    """Test that strings containing } are handled correctly."""
    context = {"msg": "test"}

    expr = "{{ 'string with } character: ' + msg }}"
    result = walk(expr, context)

    assert result == "string with } character: test"
    assert isinstance(result, str)


def test_multiple_templates_in_string():
    """Test multiple template expressions in a single string."""
    context = {"var1": "hello", "var2": "world"}

    expr = "Text with {{ var1 }} and {{ var2 }}"
    result = walk(expr, context)

    assert result == "Text with hello and world"
    assert isinstance(result, str)


def test_dict_constructor_still_works():
    """Ensure dict() constructor still works as before."""
    test_device_ids = ["device1", "device2", "device3"]
    context = {"test_device_ids": test_device_ids}

    expr = "{{ [dict(kekeke=test_device_id) for test_device_id in test_device_ids] }}"
    result = walk(expr, context)

    expected = [{"kekeke": "device1"}, {"kekeke": "device2"}, {"kekeke": "device3"}]
    assert result == expected
    assert isinstance(result, list)


def test_lowercase_boolean_literals():
    """Test that lowercase boolean literals (true, false, null) work."""
    context = {"ids": ["a", "b"]}

    # Test false
    assert walk("{{ false }}", context) is False

    # Test true
    assert walk("{{ true }}", context) is True

    # Test null
    assert walk("{{ null }}", context) is None

    # Test in dict literal
    result = walk("{{ {'enabled': false, 'value': null, 'active': true} }}", context)
    assert result == {"enabled": False, "value": None, "active": True}

    # Test in list comprehension with dict literal
    result = walk("{{ [{'id': id, 'active': false} for id in ids] }}", context)
    assert result == [{"id": "a", "active": False}, {"id": "b", "active": False}]


def test_large_comprehension_with_function_call():
    """Test that large comprehensions work with function calls in dict literals."""
    # Save original limit and increase it for this test
    original_limit = simpleeval.MAX_COMPREHENSION_LENGTH
    simpleeval.MAX_COMPREHENSION_LENGTH = 50000  # type: ignore[misc]

    try:

        def mac_address():
            return "00:11:22:33:44:55"

        context = {"mac_address": mac_address}

        # Test with dict literal and function call - should work with increased limit
        expr = "{{ [{'deviceId': mac_address(), 'household': False} for _ in range(11000)] }}"
        result = walk(expr, context)

        assert isinstance(result, list)
        assert len(result) == 11000
        assert all(item["deviceId"] == "00:11:22:33:44:55" for item in result)
        assert all(item["household"] is False for item in result)
    finally:
        # Restore original limit
        simpleeval.MAX_COMPREHENSION_LENGTH = original_limit


def test_comprehension_with_underscore_variable():
    """Test that underscore can be used as loop variable in comprehensions."""
    context = {}

    # Test with underscore - common pattern when loop variable is unused
    expr = "{{ [42 for _ in range(5)] }}"
    result = walk(expr, context)

    assert result == [42, 42, 42, 42, 42]
    assert isinstance(result, list)


def test_comprehension_limit_exceeded_error_message():
    """Test that exceeding comprehension limit gives correct error message."""
    import pytest
    from pytest_httpchain_templates.exceptions import TemplatesError

    # Use a context without setting a higher limit
    original_limit = simpleeval.MAX_COMPREHENSION_LENGTH
    simpleeval.MAX_COMPREHENSION_LENGTH = 100  # type: ignore[misc]  # Set a low limit for testing

    try:
        context = {}
        expr = "{{ [i for i in range(200)] }}"  # Exceeds limit of 100

        with pytest.raises(TemplatesError) as exc_info:
            walk(expr, context)

        # Verify we get "Expression too complex" not "Invalid expression"
        assert "Expression too complex" in str(exc_info.value)
        assert "Invalid expression" not in str(exc_info.value)
    finally:
        simpleeval.MAX_COMPREHENSION_LENGTH = original_limit
