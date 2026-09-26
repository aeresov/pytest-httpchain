"""``$include`` / ``$merge`` through a real collected scenario.

The jsonref unit tests cover resolution semantics exhaustively, and
``test_primer`` exercises ``$ref``. This is the end-to-end pin for the two
spellings the docs recommend for IDE support: a fragment pulled in verbatim,
and one deep-merged with local overrides — both reaching the wire.
"""


def test_include_and_merge_resolve_into_a_running_scenario(run_scenario):
    """Stage 1 ``$include``s its whole request and response subtrees; stage 2
    ``$merge``s a shared header set with a stage-local one.

    Both stages assert on what came back, so the fragments' contents are what
    passes them: the included request reaches the user endpoint and its checks
    run, and the merged headers arrive at the server from both sides of the
    merge rather than one overwriting the other.
    """
    result = run_scenario("refs/test_include_merge.http.json", "refs/fragments.json")

    result.assert_outcomes(passed=2)
