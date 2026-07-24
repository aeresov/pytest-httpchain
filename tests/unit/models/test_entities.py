"""Unit tests for module-level helpers in models/entities.py.

Model classes themselves are covered by the sibling test modules
(test_parameters.py, test_request.py, test_stage_scenario.py, ...); this file
covers the free functions that live alongside them.
"""

import pytest

from pytest_httpchain.models.entities import (
    CombinationsParameter,
    IndividualParameter,
    parametrize_values_contain_template,
)


class TestParametrizeValuesContainTemplate:
    """Truth table for parametrize_values_contain_template().

    Contract (per its docstring): return True iff any parametrize VALUE holds a
    ``{{ }}`` template; ``ids`` are never inspected. Both parameter shapes
    (individual and combinations) must be walked — the combinations branch has
    no other test exercising it.
    """

    @pytest.mark.parametrize(
        "parametrize, expected",
        [
            pytest.param(None, False, id="none"),
            pytest.param([], False, id="empty-list"),
            pytest.param(
                [IndividualParameter(individual={"x": [1, 2]})],
                False,
                id="individual-no-template",
            ),
            pytest.param(
                [IndividualParameter(individual={"x": ["{{ a }}", 2]})],
                True,
                id="individual-template-in-value",
            ),
            pytest.param(
                [IndividualParameter(individual={"x": [1, 2]}, ids=["{{ a }}", "b"])],
                False,
                id="individual-template-in-ids-only",
            ),
            pytest.param(
                [CombinationsParameter(combinations=[{"x": 1, "y": 2}])],
                False,
                id="combinations-no-template",
            ),
            pytest.param(
                [CombinationsParameter(combinations=[{"x": "{{ a }}", "y": 2}])],
                True,
                id="combinations-template-in-value",
            ),
            pytest.param(
                [
                    IndividualParameter(individual={"x": [1]}),
                    CombinationsParameter(combinations=[{"y": "{{ b }}"}]),
                ],
                True,
                id="mixed-template-in-second",
            ),
        ],
    )
    def test_truth_table(self, parametrize, expected):
        assert parametrize_values_contain_template(parametrize) is expected
