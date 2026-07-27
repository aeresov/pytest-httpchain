import pytest


@pytest.fixture
def run_scenario(pytester):
    """Factory fixture: copy conftest.py (+ any aux example files like
    'auth.py') and the scenario into the pytester dir, then run it."""

    def _run(scenario, *aux):
        for f in ("conftest.py", *aux):
            pytester.copy_example(f)
        pytester.copy_example(scenario)
        return pytester.runpytest("-s")

    return _run
