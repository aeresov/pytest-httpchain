import pytest

from tests.integration.helpers import write_scenario


@pytest.fixture
def run_scenario(pytester):
    """Factory fixture: copy the example conftest, the scenario and any aux
    files into the pytester dir, then run pytest there.

    Every path is copied the same way, so a run over several scenarios is just
    ``run_scenario(first, second, third)``. Aux files are the non-scenario ones
    a scenario needs — ``auth.py``, an upload, a schema. A scenario given as a
    dict instead of a path is one the test built inline (see
    ``tests.integration.helpers.stage``); it is written as
    ``test_inline.http.json``.

    ``args`` replaces the default argv (``-s``, which lets a scenario's own
    output through): pass ``args=()`` for a bare run, or add ``-k`` / ``-o`` /
    ``--collect-only`` as needed. A lone flag may be given as a plain string —
    ``args="--collect-only"`` — because the one-element-tuple spelling it saves
    you from, ``("--collect-only")``, is a string that would splat into
    single-character arguments. ``subprocess=True`` switches to
    ``runpytest_subprocess``, for the cases that need a real process — xdist,
    ini files pytest only reads at startup, plugin load order.

    A test needing a second run over the same tree calls ``pytester.runpytest``
    itself afterwards; the files are already in place.
    """

    def _run(scenario, *aux, args=("-s",), subprocess=False):
        for f in ("conftest.py", *aux):
            pytester.copy_example(f)
        if isinstance(scenario, dict):
            write_scenario(pytester.path, scenario)
        else:
            pytester.copy_example(scenario)
        argv = (args,) if isinstance(args, str) else tuple(args)
        runner = pytester.runpytest_subprocess if subprocess else pytester.runpytest
        return runner(*argv)

    return _run
