"""Test the specific case reported by the user."""

def test_user_reported_type_preservation_case(pytester):
    """Test the exact scenario described by the user."""
    pytester.copy_example("type_preservation/conftest.py")
    pytester.copy_example("type_preservation/test_user_reported_case.http.json")
    
    result = pytester.runpytest("-v")
    
    # This should pass - no more "expected 122358, got 122358" failures
    result.assert_outcomes(passed=2, failed=0)