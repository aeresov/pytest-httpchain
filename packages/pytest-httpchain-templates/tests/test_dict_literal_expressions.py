"""Test dict literal expressions in templates."""

from pytest_httpchain_templates.substitution import walk


def test_dict_literal_in_list_comprehension():
    """Test that dict literals work in list comprehensions."""
    test_device_ids = ["device1", "device2", "device3"]
    context = {"test_device_ids": test_device_ids}

    # Using dict literal syntax
    expr = "{{ [{'kekeke': test_device_id} for test_device_id in test_device_ids] }}"
    result = walk(expr, context)

    expected = [{"kekeke": "device1"}, {"kekeke": "device2"}, {"kekeke": "device3"}]
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
