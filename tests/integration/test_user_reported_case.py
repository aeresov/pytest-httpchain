"""Test for user reported type preservation issue."""


def test_user_reported_type_preservation_case(pytester):
    """Test the exact scenario the user reported with stage variables triggering type conversion."""
    pytester.copy_example("type_preservation/conftest.py")
    pytester.copy_example("type_preservation/test_user_reported_case.http.json")

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=2, failed=0)


def test_stage_vars_causing_type_preservation_issue(pytester):
    """Test that shows the issue was specifically with stage vars processing."""

    pytester.copy_example("type_preservation/conftest.py")

    # Create test case that would trigger the original issue
    pytester.makefile(
        ".http.json",
        test_stage_vars_type_issue="""{
        "fixtures": ["server"],
        "vars": {
            "api_base": "http://localhost:5000"
        },
        "stages": [
            {
                "name": "Save integer variable A",
                "request": {
                    "url": "{{ api_base }}/get_number",
                    "method": "GET"
                },
                "response": {
                    "verify": {
                        "status": 200
                    },
                    "save": {
                        "vars": {
                            "A": "id"
                        }
                    }
                }
            },
            {
                "name": "Use A in stage vars then verify against it",
                "vars": {
                    "temp_var": "{{ A }}",
                    "another_var": "Value is {{ A }}"
                },
                "request": {
                    "url": "{{ api_base }}/get_another_number",
                    "method": "GET"
                },
                "response": {
                    "save": {
                        "vars": {
                            "B": "result"
                        }
                    },
                    "verify": {
                        "status": 200,
                        "vars": {
                            "B": "{{ A }}"
                        }
                    }
                }
            }
        ]
    }""",
    )

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=2, failed=0)
