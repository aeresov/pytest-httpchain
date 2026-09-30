"""Unit tests for IndividualParameter and CombinationsParameter models."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from pytest_httpchain.models.entities import CombinationsParameter, IndividualParameter, Stage
from pytest_httpchain.models.types import convert_dict_to_namespace
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

    @pytest.mark.parametrize("ids", [["one", "two"], []], ids=["too-few", "empty"])
    def test_ids_count_must_match_values(self, ids):
        with pytest.raises(ValidationError, match="Number of ids.*must match number of values"):
            IndividualParameter(individual={"x": [1, 2, 3]}, ids=ids)

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

    def test_namespace_values_kept(self):
        """Unlike a combination, a value is not a set of parameters: an object
        from ``vars`` stays a namespace, so ``{{ user.id }}`` keeps working."""
        users = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
        assert IndividualParameter(individual={"user": users}).individual == {"user": users}

    def test_one_vars_object_is_no_list_of_values(self):
        """A template rendering one object where the values go is refused, as
        a dict is, not parametrized over the object's keys: a `vars` object
        iterates over them, and pydantic's lax list takes any iterable but a
        mapping."""
        with pytest.raises(ValidationError) as exc_info:
            IndividualParameter(individual={"user": convert_dict_to_namespace({"id": 1, "name": "a"})})
        assert_error_types(exc_info, "list_type", at="list[any]")


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

    @pytest.mark.parametrize("ids", [["one", "two"], []], ids=["too-few", "empty"])
    def test_ids_count_must_match(self, ids):
        with pytest.raises(ValidationError, match="Number of ids.*must match number of combinations"):
            CombinationsParameter(combinations=[{"x": 1}, {"x": 2}, {"x": 3}], ids=ids)

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

    def test_namespace_combinations_become_dicts(self):
        """A template over ``vars`` renders its objects as SimpleNamespace, and
        the rendered list is re-validated here: each one is the combination
        dict it was declared as. Refused as "not a dict", ``parallel.foreach``
        failed the stage while stage ``parametrize`` had a conversion of its
        own. One level only: a nested object keeps its attribute access."""
        param = CombinationsParameter(combinations=[SimpleNamespace(id=1, owner=SimpleNamespace(name="a"))])
        assert param.combinations == [{"id": 1, "owner": SimpleNamespace(name="a")}]

    @pytest.mark.parametrize("container", [tuple, iter], ids=["tuple", "iterator"])
    def test_namespace_combinations_in_any_sequence(self, container):
        """pydantic takes any iterable but text or a mapping as the list, so
        the conversion does too: a template renders a tuple
        (``{{ tuple(combos) }}``), a user function may return an iterator, and
        a conversion taking only a list would leave either one's namespaces to
        fail on "Input should be a valid dictionary"."""
        param = CombinationsParameter(combinations=container([SimpleNamespace(x=1), SimpleNamespace(x=2)]))
        assert param.combinations == [{"x": 1}, {"x": 2}]

    @pytest.mark.parametrize("combinations", [{"x": 1}, convert_dict_to_namespace({"x": 1})], ids=["dict", "vars-object"])
    def test_mapping_is_not_a_sequence_of_combinations(self, combinations):
        """A mapping is not iterated for its keys: it is refused as a whole.
        A `vars` object, iterable over its keys, is one."""
        with pytest.raises(ValidationError) as exc_info:
            CombinationsParameter(combinations=combinations)
        assert_error_types(exc_info, "list_type", at="combinations")

    def test_namespace_combinations_must_have_same_keys(self):
        """Converted before the model's own checks, which then apply as usual."""
        with pytest.raises(ValidationError, match="Combination 1 has different parameters than combination 0"):
            CombinationsParameter(combinations=[SimpleNamespace(x=1), SimpleNamespace(y=2)])


def test_raw_parameter_dicts_select_models_in_order():
    stage = Stage.model_validate(
        stage_dict(parametrize=[{"individual": {"id": [1, 2]}}, {"combinations": [{"a": 1, "b": 2}, {"a": 3, "b": 4}]}]),
    )
    assert stage.parametrize == [
        IndividualParameter(individual={"id": [1, 2]}),
        CombinationsParameter(combinations=[{"a": 1, "b": 2}, {"a": 3, "b": 4}]),
    ]
