def test_save_jmespath(run_scenario):
    """Test JMESPath extraction from response"""
    result = run_scenario("save/test_save_jmespath.http.json")
    # 2 stages = 2 test methods
    result.assert_outcomes(errors=0, failed=0, passed=2)


def test_save_substitutions(run_scenario):
    """Test computed substitutions"""
    result = run_scenario("save/test_save_substitutions.http.json")
    # 2 stages = 2 test methods
    result.assert_outcomes(errors=0, failed=0, passed=2)


def test_save_user_function(run_scenario):
    """Test user function returning dict"""
    result = run_scenario("save/test_save_user_function.http.json", "save.py")
    # 2 stages = 2 test methods
    result.assert_outcomes(errors=0, failed=0, passed=2)


def test_save_user_function_raises(run_scenario):
    """A raising save function surfaces as a clean stage failure that aborts
    the chain, mirroring the verify-side twin."""
    result = run_scenario("save/test_save_user_function_raises.http.json", "save.py")
    result.assert_outcomes(errors=0, failed=1, passed=0, skipped=1)
    result.stdout.fnmatch_lines(["*Error calling user function*"])


def test_save_user_function_returns_non_dict(run_scenario):
    """A save function returning a non-dict is rejected, not coerced."""
    result = run_scenario("save/test_save_user_function_non_dict.http.json", "save.py")
    result.assert_outcomes(errors=0, failed=1, passed=0)
    result.stdout.fnmatch_lines(["*must return dict*"])


def test_save_jmespath_runtime_error(run_scenario):
    """A jmespath expression that compiles but errors at search time fails the
    save cleanly instead of escaping as a raw traceback."""
    result = run_scenario("save/test_save_jmespath_runtime_error.http.json")
    result.assert_outcomes(errors=0, failed=1, passed=0)
    result.stdout.fnmatch_lines(["*Error saving variable*"])


def test_save_substitutions_template_error(run_scenario):
    """A save substitution whose template fails becomes a SaveError with the
    'Error processing substitutions' wrapping."""
    result = run_scenario("save/test_save_substitutions_error.http.json")
    result.assert_outcomes(errors=0, failed=1, passed=0)
    result.stdout.fnmatch_lines(["*Error processing substitutions*"])


def test_save_substitutions_sequential_entries(run_scenario):
    """Entries within ONE save step resolve strictly in order, each seeing the
    previous entry's names — the documented semantics shared with scenario- and
    stage-level substitutions (regression: an eager pre-walk evaluated later
    entries before earlier ones' names existed)."""
    result = run_scenario("save/test_save_substitutions_sequential.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=2)


def test_save_preserves_template_literal_values(run_scenario):
    """Server response text that LOOKS like a template ('{{ probe }}') is saved
    literally (regression: a double template pass re-evaluated already-rendered
    values, executing response-derived text as expressions)."""
    result = run_scenario("save/test_save_preserves_template_literals.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=2)
