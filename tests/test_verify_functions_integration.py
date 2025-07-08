

def test_verify_functions_integration(pytester):
    """Test that verify functions work correctly in integration."""
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


def test_verify_functions_with_invalid_function(pytester):
    """Test that invalid verify functions are properly handled."""
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


def test_verify_functions_returning_non_boolean(pytester):
    """Test that verify functions must return boolean values."""
    # Create a helper function that returns non-boolean
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


def test_verify_functions_failing_verification(pytester):
    """Test that failing verify functions properly fail the test."""
    # Create a helper function that always returns False
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


def test_verify_functions_with_exception(pytester):
    """Test that verify functions that raise exceptions are properly handled."""
    # Create a helper function that raises an exception
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


def test_verify_functions_combined_with_status_and_json(pytester):
    """Test that verify functions work correctly with status and JSON verification."""
    # Create helper functions
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
