"""Unit tests for IndividualParameter and CombinationsParameter models."""

import pytest
from pydantic import ValidationError
from pytest_httpchain_models.entities import (
    CombinationsParameter,
    IndividualParameter,
    Stage,
)


class TestIndividualParameter:
    """Tests for IndividualParameter model."""

    def test_individual_single_parameter(self):
        """Test IndividualParameter with single parameter."""
        param = IndividualParameter(individual={"user_id": [1, 2, 3]})
        assert param.individual == {"user_id": [1, 2, 3]}

    def test_individual_string_values(self):
        """Test IndividualParameter with string values."""
        param = IndividualParameter(individual={"name": ["alice", "bob", "charlie"]})
        assert param.individual["name"] == ["alice", "bob", "charlie"]

    def test_individual_with_ids(self):
        """Test IndividualParameter with custom IDs."""
        param = IndividualParameter(
            individual={"status": ["active", "inactive", "pending"]},
            ids=["active_user", "inactive_user", "pending_user"],
        )
        assert param.ids == ["active_user", "inactive_user", "pending_user"]

    def test_individual_ids_count_must_match_values(self):
        """Test that IDs count must match values count."""
        with pytest.raises(ValidationError, match="Number of ids.*must match number of values"):
            IndividualParameter(
                individual={"x": [1, 2, 3]},
                ids=["one", "two"],  # Only 2 IDs for 3 values
            )

    def test_individual_empty_values_rejected(self):
        """Test that empty values list is rejected."""
        with pytest.raises(ValidationError, match="at least 1"):
            IndividualParameter(individual={"x": []})

    def test_individual_with_template_expression(self):
        """Test IndividualParameter with template expression."""
        param = IndividualParameter(individual={"items": "{{ item_list }}"})
        assert param.individual["items"] == "{{ item_list }}"

    def test_individual_template_skips_ids_validation(self):
        """Test that template values skip IDs count validation."""
        # Should not raise even though ids count doesn't match template
        param = IndividualParameter(
            individual={"items": "{{ item_list }}"},
            ids=["one", "two"],
        )
        assert param.ids == ["one", "two"]


class TestCombinationsParameter:
    """Tests for CombinationsParameter model."""

    def test_combinations_single_combination(self):
        """Test CombinationsParameter with single combination."""
        param = CombinationsParameter(combinations=[{"x": 1, "y": 2}])
        assert param.combinations == [{"x": 1, "y": 2}]

    def test_combinations_multiple(self):
        """Test CombinationsParameter with multiple combinations."""
        param = CombinationsParameter(
            combinations=[
                {"method": "GET", "path": "/users"},
                {"method": "POST", "path": "/users"},
                {"method": "DELETE", "path": "/users/1"},
            ]
        )
        assert len(param.combinations) == 3

    def test_combinations_with_ids(self):
        """Test CombinationsParameter with custom IDs."""
        param = CombinationsParameter(
            combinations=[{"a": 1}, {"a": 2}],
            ids=["first", "second"],
        )
        assert param.ids == ["first", "second"]

    def test_combinations_ids_count_must_match(self):
        """Test that IDs count must match combinations count."""
        with pytest.raises(ValidationError, match="Number of ids.*must match number of combinations"):
            CombinationsParameter(
                combinations=[{"x": 1}, {"x": 2}, {"x": 3}],
                ids=["one", "two"],  # Only 2 IDs for 3 combinations
            )

    def test_combinations_must_have_same_keys(self):
        """Test that all combinations must have the same keys."""
        with pytest.raises(ValidationError, match="different parameters"):
            CombinationsParameter(
                combinations=[
                    {"x": 1, "y": 2},
                    {"x": 3, "z": 4},  # Different keys
                ]
            )

    def test_combinations_single_allowed_different_structure(self):
        """Test that single combination doesn't need key consistency check."""
        # Single combination doesn't trigger consistency check
        param = CombinationsParameter(combinations=[{"x": 1}])
        assert len(param.combinations) == 1

    def test_combinations_empty_dict_rejected(self):
        """Test that empty combination dict is rejected."""
        with pytest.raises(ValidationError, match="at least 1"):
            CombinationsParameter(combinations=[{}])

    def test_combinations_with_template_expression(self):
        """Test CombinationsParameter with template expression."""
        param = CombinationsParameter(combinations="{{ test_combinations }}")
        assert param.combinations == "{{ test_combinations }}"

    def test_combinations_template_skips_validation(self):
        """Test that template skips all validations."""
        # Should not raise even though ids don't match
        param = CombinationsParameter(
            combinations="{{ combos }}",
            ids=["a", "b", "c"],
        )
        assert param.ids == ["a", "b", "c"]


class TestParameterDiscriminator:
    """Tests for Parameter discriminated union."""

    def test_discriminator_individual(self):
        """Test discriminator identifies IndividualParameter."""
        stage = Stage(
            name="test",
            request={"url": "https://example.com"},
            parametrize=[{"individual": {"x": [1, 2, 3]}}],
        )
        assert len(stage.parametrize) == 1
        assert isinstance(stage.parametrize[0], IndividualParameter)

    def test_discriminator_combinations(self):
        """Test discriminator identifies CombinationsParameter."""
        stage = Stage(
            name="test",
            request={"url": "https://example.com"},
            parametrize=[{"combinations": [{"x": 1}, {"x": 2}]}],
        )
        assert len(stage.parametrize) == 1
        assert isinstance(stage.parametrize[0], CombinationsParameter)

    def test_discriminator_mixed_parameters(self):
        """Test discriminator with mixed parameter types."""
        stage = Stage(
            name="test",
            request={"url": "https://example.com"},
            parametrize=[
                {"individual": {"id": [1, 2]}},
                {"combinations": [{"a": 1, "b": 2}, {"a": 3, "b": 4}]},
            ],
        )
        assert isinstance(stage.parametrize[0], IndividualParameter)
        assert isinstance(stage.parametrize[1], CombinationsParameter)

    def test_discriminator_invalid_type_rejected(self):
        """Test that invalid parameter type is rejected."""
        with pytest.raises(ValueError, match="Unable to determine parameter step type"):
            Stage(
                name="test",
                request={"url": "https://example.com"},
                parametrize=[{"invalid": "value"}],
            )


class TestParametersInStage:
    """Tests for Parameters in Stage model."""

    def test_stage_without_parametrize(self):
        """Test Stage without parametrize field."""
        stage = Stage(name="test", request={"url": "https://example.com"})
        assert stage.parametrize is None

    def test_stage_with_empty_parametrize(self):
        """Test Stage with empty parametrize list."""
        stage = Stage(
            name="test",
            request={"url": "https://example.com"},
            parametrize=[],
        )
        assert stage.parametrize == []

    def test_stage_parametrize_with_template_url(self):
        """Test Stage with parametrize and template URL."""
        stage = Stage(
            name="test",
            request={"url": "https://example.com/users/{{ user_id }}"},
            parametrize=[{"individual": {"user_id": [1, 2, 3]}}],
        )
        assert len(stage.parametrize) == 1
