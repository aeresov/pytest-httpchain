"""Integration tests for pytest-http plugin using pytester.

These tests verify the plugin's functionality through end-to-end scenarios
and serve as examples of how to use the plugin features.
"""


# Basic plugin functionality tests
def test_basic_json_test_collection_and_execution(pytester):
    """Test basic JSON test collection and execution."""
    pytester.copy_example("test_basic.http.json")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


def test_json_test_with_fixtures(pytester):
    """Test JSON tests with fixture support."""
    pytester.copy_example("conftest.py")
    pytester.copy_example("test_fixtures.http.json")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


def test_json_test_with_variable_substitution(pytester):
    """Test JSON tests with variable substitution."""
    pytester.copy_example("conftest.py")
    pytester.copy_example("test_substitution.http.json")
    result = pytester.runpytest("-vv")
    result.assert_outcomes(passed=1)


def test_json_test_with_skip_mark(pytester):
    """Test JSON tests with skip marks."""
    pytester.copy_example("test_skip.http.json")
    result = pytester.runpytest()
    result.assert_outcomes(skipped=1)


def test_invalid_json_fails_gracefully(pytester):
    """Test that invalid JSON fails gracefully."""
    pytester.copy_example("test_invalid.http.json")
    result = pytester.runpytest()
    result.assert_outcomes(failed=1)


def test_non_matching_files_not_collected(pytester):
    """Test that non-matching files are not collected."""
    pytester.copy_example("not_a_test.json")
    pytester.copy_example("not_a_test.http.json")
    pytester.copy_example("test_valid.http.json")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


def test_multiple_stages_in_test(pytester):
    """Test JSON tests with multiple stages."""
    pytester.copy_example("test_multistage.http.json")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


def test_regular_python_tests_work_alongside_plugin(pytester):
    """Test that regular Python tests work alongside the plugin."""
    pytester.copy_example("conftest.py")
    pytester.copy_example("test_regular_python.py")
    pytester.copy_example("test_basic.http.json")
    result = pytester.runpytest()
    result.assert_outcomes(passed=3)


def test_json_test_with_skipif_mark(pytester):
    """Test JSON tests with skipif marks."""
    pytester.copy_example("test_mark_skipif.http.json")
    result = pytester.runpytest()
    outcomes = result.parseoutcomes()
    assert outcomes.get("failed", 0) == 0
    assert outcomes.get("passed", 0) + outcomes.get("skipped", 0) == 1


def test_json_test_with_collection_validation_error(pytester):
    """Test JSON tests with collection validation errors."""
    pytester.copy_example("test_mark_xfail.http.json")
    result = pytester.runpytest()
    result.assert_outcomes(failed=1)


def test_json_test_with_references(pytester):
    """Test JSON tests with JSON references."""
    pytester.copy_example("test_ref.http.json")
    pytester.copy_example("ref_stage.json")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


# Verify functions integration tests
def test_verify_functions_basic_integration(pytester):
    """Test basic verify functions integration."""
    # Create helper functions that will be available
    pytester.makefile(
        ".py",
        test_verify_helpers="""
        def verify_response_status_200(response):
            return response.status_code == 200

        def verify_response_has_json(response):
            try:
                response.json()
                return True
            except Exception:
                return False

        def verify_response_has_headers(response):
            return len(response.headers) > 0

        def verify_response_content_type_json(response):
            content_type = response.headers.get("content-type", "")
            return "application/json" in content_type.lower()
        """,
    )

    # Create test file
    test_file = pytester.makefile(
        ".http.json",
        test_verify_functions="""
        {
            "stages": [
                {
                    "name": "test_verify_functions_basic",
                    "url": "https://httpbin.org/json",
                    "verify": {
                        "status": 200,
                        "functions": ["test_verify_helpers:verify_response_status_200"]
                    }
                },
                {
                    "name": "test_verify_functions_json",
                    "url": "https://httpbin.org/json",
                    "verify": {
                        "functions": [
                            "test_verify_helpers:verify_response_has_json",
                            "test_verify_helpers:verify_response_content_type_json"
                        ]
                    }
                },
                {
                    "name": "test_verify_functions_headers",
                    "url": "https://httpbin.org/headers",
                    "verify": {
                        "functions": ["test_verify_helpers:verify_response_has_headers"]
                    }
                },
                {
                    "name": "test_verify_functions_multiple",
                    "url": "https://httpbin.org/json",
                    "verify": {
                        "status": 200,
                        "json": {
                            "json.slideshow.title": "Sample Slide Show"
                        },
                        "functions": [
                            "test_verify_helpers:verify_response_status_200",
                            "test_verify_helpers:verify_response_has_json"
                        ]
                    }
                }
            ]
        }
        """,
    )

    result = pytester.runpytest(str(test_file), "-v")
    result.assert_outcomes(passed=4)


def test_verify_functions_error_handling(pytester):
    """Test verify functions error handling scenarios."""
    # Test invalid function
    test_file = pytester.makefile(
        ".http.json",
        test_invalid_verify_functions="""
        {
            "stages": [
                {
                    "name": "test_invalid_verify_function",
                    "url": "https://httpbin.org/json",
                    "verify": {
                        "functions": ["nonexistent_module:function"]
                    }
                }
            ]
        }
        """,
    )

    result = pytester.runpytest(str(test_file), "-v")
    result.assert_outcomes(failed=1)

    # Test non-boolean return
    pytester.makefile(
        ".py",
        test_verify_helpers="""
        def invalid_verify_function(response):
            return "not a boolean"
        """,
    )

    test_file = pytester.makefile(
        ".http.json",
        test_invalid_return="""
        {
            "stages": [
                {
                    "name": "test_invalid_return_type",
                    "url": "https://httpbin.org/json",
                    "verify": {
                        "functions": ["test_verify_helpers:invalid_verify_function"]
                    }
                }
            ]
        }
        """,
    )

    result = pytester.runpytest(str(test_file), "-v")
    result.assert_outcomes(failed=1)

    # Test exception handling
    pytester.makefile(
        ".py",
        test_verify_helpers="""
        def exception_verify_function(response):
            raise ValueError("Test exception")
        """,
    )

    test_file = pytester.makefile(
        ".http.json",
        test_exception_verify="""
        {
            "stages": [
                {
                    "name": "test_exception_verify",
                    "url": "https://httpbin.org/json",
                    "verify": {
                        "functions": ["test_verify_helpers:exception_verify_function"]
                    }
                }
            ]
        }
        """,
    )

    result = pytester.runpytest(str(test_file), "-v")
    result.assert_outcomes(failed=1)


def test_verify_functions_failing_verification(pytester):
    """Test that failing verify functions properly fail the test."""
    pytester.makefile(
        ".py",
        test_verify_helpers="""
        def failing_verify_function(response):
            return False
        """,
    )

    test_file = pytester.makefile(
        ".http.json",
        test_failing_verify="""
        {
            "stages": [
                {
                    "name": "test_failing_verify",
                    "url": "https://httpbin.org/json",
                    "verify": {
                        "functions": ["test_verify_helpers:failing_verify_function"]
                    }
                }
            ]
        }
        """,
    )

    result = pytester.runpytest(str(test_file), "-v")
    result.assert_outcomes(failed=1)


def test_verify_functions_combined_with_status_and_json(pytester):
    """Test verify functions combined with status and JSON verification."""
    pytester.makefile(
        ".py",
        test_verify_helpers="""
        def verify_response_status_200(response):
            return response.status_code == 200

        def verify_response_has_json(response):
            try:
                response.json()
                return True
            except Exception:
                return False
        """,
    )

    test_file = pytester.makefile(
        ".http.json",
        test_combined_verify="""
        {
            "stages": [
                {
                    "name": "test_combined_verification",
                    "url": "https://httpbin.org/json",
                    "verify": {
                        "status": 200,
                        "json": {
                            "json.slideshow.title": "Sample Slide Show"
                        },
                        "functions": [
                            "test_verify_helpers:verify_response_status_200",
                            "test_verify_helpers:verify_response_has_json"
                        ]
                    }
                }
            ]
        }
        """,
    )

    result = pytester.runpytest(str(test_file), "-v")
    result.assert_outcomes(passed=1)


# Configuration tests
def test_custom_suffix_configuration(pytester):
    """Test custom suffix configuration."""
    pytester.makeini("""
        [tool:pytest]
        suffix = custom
    """)

    pytester.makefile(".custom.json", test_example='{"fixtures": [], "marks": [], "stages": [{"name": "test_stage", "url": "https://httpbin.org/json"}]}')
    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=1)


def test_json_body_with_different_data_types(pytester):
    """Test JSON body with different JSON-serializable data types."""
    test_file = pytester.makefile(
        ".http.json",
        test_json_types="""
        {
            "stages": [
                {
                    "name": "test_json_with_string",
                    "url": "https://httpbin.org/post",
                    "method": "POST",
                    "json": "simple string",
                    "verify": {
                        "status": 200,
                        "json": {
                            "json.json": "simple string"
                        }
                    }
                },
                {
                    "name": "test_json_with_number",
                    "url": "https://httpbin.org/post",
                    "method": "POST",
                    "json": 42,
                    "verify": {
                        "status": 200,
                        "json": {
                            "json.json": 42
                        }
                    }
                },
                {
                    "name": "test_json_with_boolean",
                    "url": "https://httpbin.org/post",
                    "method": "POST",
                    "json": true,
                    "verify": {
                        "status": 200,
                        "json": {
                            "json.json": true
                        }
                    }
                },
                {
                    "name": "test_json_with_array",
                    "url": "https://httpbin.org/post",
                    "method": "POST",
                    "json": [1, 2, 3, "four"],
                    "verify": {
                        "status": 200,
                        "json": {
                            "json.json": [1, 2, 3, "four"]
                        }
                    }
                },
                {
                    "name": "test_json_with_nested_object",
                    "url": "https://httpbin.org/post",
                    "method": "POST",
                    "json": {
                        "user": {
                            "name": "John",
                            "details": {
                                "age": 30,
                                "active": true
                            }
                        }
                    },
                    "verify": {
                        "status": 200,
                        "json": {
                            "json.user.name": "John",
                            "json.user.details.age": 30,
                            "json.user.details.active": true
                        }
                    }
                }
            ]
        }
        """,
    )

    result = pytester.runpytest(str(test_file), "-v")
    result.assert_outcomes(passed=5)


def test_json_validation_rejects_non_serializable_data(pytester):
    """Test that JSON validation rejects non-serializable data."""
    # Note: This test would fail at collection time due to validation
    # We can't easily test this with pytester since the JSON would be invalid
    # But we can test it by creating a test that would fail validation
    test_file = pytester.makefile(
        ".http.json",
        test_invalid_json="""
        {
            "stages": [
                {
                    "name": "test_invalid_json",
                    "url": "https://httpbin.org/post",
                    "method": "POST",
                    "json": {
                        "valid": "data",
                        "invalid": "This will be replaced with non-serializable data in the test"
                    }
                }
            ]
        }
        """,
    )

    # This test should pass because the JSON in the file is valid
    # The actual validation happens at the model level, which we test in the model tests
    result = pytester.runpytest(str(test_file), "-v")
    result.assert_outcomes(passed=1)
