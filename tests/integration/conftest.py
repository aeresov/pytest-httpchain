def run_scenario(pytester, scenario, *aux):
    """Copy conftest.py (+ any aux example files like 'auth.py') and the scenario, then run."""
    for f in ("conftest.py", *aux):
        pytester.copy_example(f)
    pytester.copy_example(scenario)
    return pytester.runpytest("-s")
