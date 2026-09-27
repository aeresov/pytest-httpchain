"""factory.py: a validated scenario becomes a Carrier subclass, with each stage's
``parametrize`` steps resolved into pytest parametrize marks at collection.

Collecting and running the generated class is the integration suite's
(test_parametrize.py); this pins how the steps resolve.
"""

import pytest

from pytest_httpchain.errors import StageExecutionError
from pytest_httpchain.factory import create_test_class
from pytest_httpchain.models import Scenario

COMBOS = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]


def _parametrize_args(parametrize: list, substitutions: list) -> list[tuple]:
    """The ``(argnames, argvalues)`` of each parametrize mark on the one stage."""
    scenario = Scenario.model_validate({"substitutions": substitutions, "stages": [{"name": "s", "request": {"url": "http://x"}, "parametrize": parametrize}]})
    method = getattr(create_test_class(scenario, "_Scenario"), "test 0 - s")
    return [mark.args for mark in method.pytestmark if mark.name == "parametrize"]


@pytest.mark.parametrize("template", ["{{ combos }}", "{{ tuple(combos) }}", "{{ (combos[0], combos[1]) }}"])
def test_combinations_template_over_vars(template):
    """``vars`` objects are the combinations they were declared as, whatever
    sequence the template renders them in. A guard for the model's rule, which
    replaced the factory's own conversion: that one iterated any iterable, and a
    rule taking only a list would fail these tuples on "Input should be a valid
    dictionary"."""
    args = _parametrize_args([{"combinations": template}], [{"vars": {"combos": COMBOS}}])
    assert args == [("a,b", [(1, "x"), (2, "y")])]


@pytest.mark.parametrize(
    ("step", "field"),
    [
        # Parametrized over the rendered text's characters, one test each.
        pytest.param({"individual": {"v": "{{ nested }}"}}, "individual 'v'", id="individual"),
        # Failed collection with a pydantic error per character, as the
        # factory's own conversion iterated the text; without that conversion,
        # reading the first combination's keys would fail with a bare
        # AttributeError.
        pytest.param({"combinations": "{{ nested }}"}, "combinations", id="combinations"),
    ],
)
def test_template_rendering_a_template_fails_by_name(step, field):
    """Both step kinds also accept a template string, so one rendering to
    another passes re-validation as text. Refused with the step and stage
    named, as ``parallel.foreach`` refuses it at run time."""
    nested = "{{ '{' + '{ x }' + '}' }}"
    with pytest.raises(StageExecutionError, match=f"^parametrize {field} on stage 's' must resolve to a list, got '{{{{ x }}}}'$"):
        _parametrize_args([step], [{"vars": {"nested": nested}}])
