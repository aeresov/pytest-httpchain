import pytest
from pydantic import BaseModel
from pytest_httpchain_templates.exceptions import TemplatesError
from pytest_httpchain_templates.substitution import walk


class TestWalk:
    """Test the walk() function"""

    class SampleModel(BaseModel):
        name: str
        value: int

    def test_string_with_substitution(self):
        result = walk("{{ greeting }} {{ name }}", {"greeting": "hello", "name": "World"})
        assert result == "hello World"

    def test_string_single_expression(self):
        result = walk("{{ value }}", {"value": 42})
        assert result == 42  # Type preserved

    def test_dict(self):
        input_dict = {"greeting": "Hello {{ name }}", "count": "{{ num }}"}
        result = walk(input_dict, {"name": "Alice", "num": 5})
        assert result == {"greeting": "Hello Alice", "count": 5}

    def test_list(self):
        input_list = ["{{ a }}", "Value: {{ b }}", "{{ c }}"]
        result = walk(input_list, {"a": 1, "b": 2, "c": 3})
        assert result == [1, "Value: 2", 3]

    def test_pydantic_model(self):
        model = TestWalk.SampleModel(name="User {{ id }}", value=100)
        result = walk(model, {"id": "123"})
        assert isinstance(result, TestWalk.SampleModel)
        assert result.name == "User 123"
        assert result.value == 100

    def test_other_types(self):
        assert walk(42, {}) == 42
        assert walk(None, {}) is None
        assert walk(True, {}) is True

    def test_invalid_expression(self):
        with pytest.raises(TemplatesError, match="Invalid expression"):
            walk("{{ invalid + }}", {})

    # New tests for compound types support
    def test_list_creation(self):
        result = walk("{{ [1, 2, 3, 4] }}", {})
        assert result == [1, 2, 3, 4]

    def test_dict_creation(self):
        # Using dict() constructor which is supported by simpleeval
        result = walk("{{ dict(key='value', number=42) }}", {})
        assert result == {"key": "value", "number": 42}

    def test_list_comprehension(self):
        result = walk("{{ [x * 2 for x in items] }}", {"items": [1, 2, 3]})
        assert result == [2, 4, 6]

    def test_dict_comprehension(self):
        # Dictionary comprehensions need to be tested differently
        # simpleeval may not support dict comprehensions directly
        result = walk("{{ dict([(k, v * 2) for k, v in data.items()]) }}", {"data": {"a": 1, "b": 2}})
        assert result == {"a": 2, "b": 4}

    def test_nested_structures(self):
        context = {"users": [{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}], "index": 0}
        result = walk("{{ users[index]['name'] }}", context)
        assert result == "Alice"

    def test_builtin_functions(self):
        # Test various built-in functions
        assert walk("{{ str(123) }}", {}) == "123"
        assert walk("{{ int('42') }}", {}) == 42
        assert walk("{{ len([1, 2, 3]) }}", {}) == 3
        assert walk("{{ max([1, 5, 3]) }}", {}) == 5
        assert walk("{{ sum([1, 2, 3]) }}", {}) == 6
        assert walk("{{ sorted([3, 1, 2]) }}", {}) == [1, 2, 3]

    def test_combined_operations(self):
        context = {"numbers": [1, 2, 3, 4, 5], "multiplier": 2}
        result = walk("{{ sum([x * multiplier for x in numbers]) }}", context)
        assert result == 30

    def test_undefined_variable(self):
        with pytest.raises(TemplatesError, match="Undefined variable"):
            walk("{{ undefined_var }}", {})

    def test_no_dangerous_operations(self):
        # Verify that dangerous operations are blocked
        with pytest.raises(TemplatesError):
            walk("{{ __import__('os').system('ls') }}", {})

        with pytest.raises(TemplatesError):
            walk("{{ open('/etc/passwd', 'r').read() }}", {})

    def test_specific_error_messages(self):
        # Test that specific error types produce appropriate messages

        # Undefined variable
        with pytest.raises(TemplatesError, match="Undefined variable"):
            walk("{{ missing_var }}", {})

        # Unknown function
        with pytest.raises(TemplatesError, match="Unknown function"):
            walk("{{ unknown_func() }}", {})

        # Attribute error
        with pytest.raises(TemplatesError, match="Attribute error"):
            walk("{{ x.nonexistent }}", {"x": {"existing": 1}})

        # Syntax error
        with pytest.raises(TemplatesError, match="Invalid expression"):
            walk("{{ 1 + }}", {})

        # Division by zero
        with pytest.raises(TemplatesError, match="ZeroDivisionError"):
            walk("{{ 1 / 0 }}", {})

        # Type error
        with pytest.raises(TemplatesError, match="TypeError"):
            walk("{{ 'text' + 5 }}", {})

        # Index error
        with pytest.raises(TemplatesError, match="IndexError"):
            walk("{{ [1, 2][10] }}", {})

        # Key error - using dict() function since literal syntax doesn't work
        with pytest.raises(TemplatesError, match="KeyError"):
            walk("{{ dict(a=1)['b'] }}", {})


    def test_get_function(self):
        """Test the get() function for dict-like access with defaults."""
        # Variable doesn't exist - use default
        result = walk("{{ get('missing_var', 'default_value') }}", {})
        assert result == "default_value"

        # Variable exists - use actual value
        result = walk("{{ get('existing_var', 'default_value') }}", {"existing_var": "actual"})
        assert result == "actual"

        # No default provided - returns None
        result = walk("{{ get('missing_var') }}", {})
        assert result is None

    def test_exists_function(self):
        """Test the exists() function for checking variable existence."""
        # Variable exists
        result = walk("{{ exists('my_var') }}", {"my_var": "value"})
        assert result is True

        # Variable doesn't exist
        result = walk("{{ exists('missing_var') }}", {})
        assert result is False

        # Can be used in conditionals
        result = walk("{{ 'found' if exists('var') else 'not found' }}", {"var": 1})
        assert result == "found"

        result = walk("{{ 'found' if exists('var') else 'not found' }}", {})
        assert result == "not found"

    def test_get_with_complex_expressions(self):
        """Test get() with complex expressions."""

        # Using get() for safe dict access
        context = {"config": {"key1": "value1"}}
        result = walk("{{ get('missing_key', 'default') }}", context)
        assert result == "default"

        # Using get with nested dict access
        # Note: {} is not a valid literal in simpleeval, use dict() instead
        result = walk("{{ get('config', dict()).get('missing', 'not found') }}", {})
        assert result == "not found"

        # Using exists() to safely check before accessing
        context = {"items": [1, 2, 3]}
        result = walk("{{ items[2] if exists('items') else 'no items' }}", context)
        assert result == 3

        result = walk("{{ items[0] if exists('items') else 'no items' }}", {})
        assert result == "no items"

    def test_combining_get_with_other_operations(self):
        """Test combining get with other operations."""
        # Chain with string operations
        result = walk("{{ get('name', 'Guest').upper() }}", {})
        assert result == "GUEST"

        result = walk("{{ get('name', 'Guest').upper() }}", {"name": "alice"})
        assert result == "ALICE"

        # Use in list comprehension
        context = {"items": ["a", "b", "c"]}
        result = walk("{{ [x.upper() for x in get('items', [])] }}", context)
        assert result == ["A", "B", "C"]

        result = walk("{{ [x.upper() for x in get('items', [])] }}", {})
        assert result == []
