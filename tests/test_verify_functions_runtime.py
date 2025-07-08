import pytest
import requests
from unittest.mock import Mock

from pytest_http.pytest_plugin import json_test_function


def test_verify_functions_runtime_behavior():
    """Test that verify functions work correctly at runtime."""
    # Create a test function that works with Response objects
    def verify_response_status_200(response):
        return response.status_code == 200

    def verify_response_has_json(response):
        try:
            response.json()
            return True
        except Exception:
            return False

    # Test data with verify functions
    test_data = {
        "stages": [
            {
                "name": "test_verify_functions",
                "url": "https://httpbin.org/json",
                "verify": {
                    "status": 200,
                    "json": {
                        "json.slideshow.title": "Sample Slide Show"
                    },
                    "functions": ["json:loads"]  # This will fail as expected
                }
            }
        ]
    }

    # This should fail because json:loads expects a string, not a Response object
    with pytest.raises(Exception) as exc_info:
        json_test_function(test_data)
    
    # The error should be about the JSON object type
    assert "JSON object must be str, bytes or bytearray" in str(exc_info.value)


def test_verify_functions_invalid_function():
    """Test that invalid verify functions are properly handled."""
    test_data = {
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

    # This should fail because the function doesn't exist
    with pytest.raises(Exception) as exc_info:
        json_test_function(test_data)
    
    assert "Cannot import module" in str(exc_info.value)


def test_verify_functions_validation_works():
    """Test that verify functions validation works correctly."""
    # Test with a valid built-in function
    test_data = {
        "stages": [
            {
                "name": "test_valid_function",
                "url": "https://httpbin.org/json",
                "verify": {
                    "functions": ["os:getcwd"]  # This is a valid function
                }
            }
        ]
    }

    # This should pass validation but fail at runtime because os:getcwd doesn't work with Response
    with pytest.raises(Exception) as exc_info:
        json_test_function(test_data)
    
    # The error should be about the function not working with Response objects
    assert "Error executing verify function" in str(exc_info.value)