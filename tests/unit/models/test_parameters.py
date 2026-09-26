"""Unit tests for IndividualParameter and CombinationsParameter models."""

import pytest
from pydantic import ValidationError

from pytest_httpchain.models.entities import CombinationsParameter, IndividualParameter, Stage
from tests.unit.models.helpers import assert_error_types, stage_dict


class TestIndividualParameter:
    def test_values_round_trip(self):
        assert IndividualParameter(individual={"user_id": [1, 2, 3]}).individual == {"user_id": [1, 2, 3]}

    def test_ids_matching_values_accepted(self):
        param = IndividualParameter(
            individual={"status": ["active", "inactive", "pending"]},
            ids=["active_user", "inactive_user", "pending_user"],
        )
        assert param.ids == ["active_user", "inactive_user", "pending_user"]

    def test_ids_count_must_match_values(self):
        with pytest.raises(ValidationError, match="Number of ids.*must match number of values"):
            IndividualParameter(individual={"x": [1, 2, 3]}, ids=["one", "two"])

    def test_empty_values_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            IndividualParameter(individual={"x": []})
        assert_error_types(exc_info, "too_short", at="individual")

    def test_multi_key_rejected(self):
        """M22: more than one parameter per step is rejected, not silently truncated."""
        with pytest.raises(ValidationError) as exc_info:
            IndividualParameter(individual={"x": [1, 2], "y": [3, 4]})
        assert_error_types(exc_info, "too_long", at="individual")

    def test_template_values_skip_ids_count_check(self):
        """The value count of a template is unknown until it renders."""
        param = IndividualParameter(individual={"items": "{{ item_list }}"}, ids=["one", "two"])
        assert (param.individual, param.ids) == ({"items": "{{ item_list }}"}, ["one", "two"])


class TestCombinationsParameter:
    def test_combinations_round_trip(self):
        combinations = [{"method": "GET", "path": "/users"}, {"method": "POST", "path": "/users"}, {"method": "DELETE", "path": "/users/1"}]
        assert CombinationsParameter(combinations=combinations).combinations == combinations

    def test_single_combination_accepted(self):
        """No other combination to compare keys against."""
        assert CombinationsParameter(combinations=[{"x": 1, "y": 2}]).combinations == [{"x": 1, "y": 2}]

    def test_ids_matching_combinations_accepted(self):
        param = CombinationsParameter(combinations=[{"a": 1}, {"a": 2}], ids=["first", "second"])
        assert param.ids == ["first", "second"]

    def test_ids_count_must_match(self):
        with pytest.raises(ValidationError, match="Number of ids.*must match number of combinations"):
            CombinationsParameter(combinations=[{"x": 1}, {"x": 2}, {"x": 3}], ids=["one", "two"])

    def test_combinations_must_have_same_keys(self):
        with pytest.raises(ValidationError, match="Combination 1 has different parameters than combination 0"):
            CombinationsParameter(combinations=[{"x": 1, "y": 2}, {"x": 3, "z": 4}])

    def test_empty_combination_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            CombinationsParameter(combinations=[{}])
        assert_error_types(exc_info, "too_short", at="combinations")

    def test_empty_list_rejected(self):
        """An empty combinations list expands to zero iterations at runtime
        (a hard 'produced zero iterations' failure). Reject it at the model layer
        so it is caught at validation/collection instead, matching the runtime."""
        with pytest.raises(ValidationError) as exc_info:
            CombinationsParameter(combinations=[])
        assert_error_types(exc_info, "too_short", at="combinations")

    def test_template_skips_all_checks(self):
        """Keys and count of a template are unknown until it renders."""
        param = CombinationsParameter(combinations="{{ combos }}", ids=["a", "b", "c"])
        assert (param.combinations, param.ids) == ("{{ combos }}", ["a", "b", "c"])


def test_raw_parameter_dicts_select_models_in_order():
    stage = Stage.model_validate(
        stage_dict(parametrize=[{"individual": {"id": [1, 2]}}, {"combinations": [{"a": 1, "b": 2}, {"a": 3, "b": 4}]}]),
    )
    assert stage.parametrize == [
        IndividualParameter(individual={"id": [1, 2]}),
        CombinationsParameter(combinations=[{"a": 1, "b": 2}, {"a": 3, "b": 4}]),
    ]
