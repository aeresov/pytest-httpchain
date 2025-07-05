import pytest
from pydantic import ValidationError

from pytest_http.models import Structure


def test_structure_with_fixtures_and_marks():
    data = {"fixtures": ["fixture1", "fixture2"], "marks": ["mark1", "mark2"]}
    structure = Structure.model_validate(data)
    assert structure.fixtures == ["fixture1", "fixture2"]
    assert structure.marks == ["mark1", "mark2"]


def test_structure_empty():
    data = {}
    structure = Structure.model_validate(data)
    assert structure.fixtures == []
    assert structure.marks == []


def test_structure_with_extra_fields():
    data = {"fixtures": ["fixture1"], "marks": ["mark1"], "unknown_field": "should_be_ignored"}
    structure = Structure.model_validate(data)
    assert structure.fixtures == ["fixture1"]
    assert structure.marks == ["mark1"]
    assert not hasattr(structure, "unknown_field")


def test_structure_with_none_values():
    data = {"fixtures": None, "marks": None}
    with pytest.raises(ValidationError):
        Structure.model_validate(data)


def test_structure_invalid_fixtures_type():
    data = {"fixtures": "not_a_list"}
    with pytest.raises(ValidationError):
        Structure.model_validate(data)


def test_structure_invalid_marks_type():
    data = {"marks": 123}
    with pytest.raises(ValidationError):
        Structure.model_validate(data)
