import pytest
from pydantic import BaseModel
from pytest_http_engine.substitution import SubstitutionError, walk


class TestWalk:
    """Test the walk() function"""

    class SampleModel(BaseModel):
        name: str
        value: int

    def test_string_with_substitution(self):
        result = walk("Hello {{ name }}", {"name": "World"})
        assert result == "Hello World"

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
        with pytest.raises(SubstitutionError, match="Invalid expression"):
            walk("{{ invalid + }}", {})
