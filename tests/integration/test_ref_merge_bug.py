"""Test to demonstrate and verify the fix for $ref merge bug."""


def test_ref_merge_bug_reproduction(pytester):
    """Test that $ref merging preserves stage isolation for variables."""
    pytester.copy_example("ref_merge_bug/conftest.py")
    pytester.copy_example("ref_merge_bug/common_response.json")
    pytester.copy_example("ref_merge_bug/test_ref_merge_bug.http.json")

    result = pytester.runpytest("-v")

    # The test should pass - stages should be isolated and not share variables
    result.assert_outcomes(passed=2, failed=0)
