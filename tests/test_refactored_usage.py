import pytest
import responses

from pytest_http.pytest_plugin import json_test_function


@responses.activate
def test_http_request_with_refactored_fixtures(mock_response, create_test_data, assert_response_calls):
    """Example of using the new fixtures to reduce test boilerplate."""
    
    # Setup mock response using the fixture
    mock_response(
        url="https://api.example.com/users",
        json_data={"users": [{"id": 1, "name": "John"}]},
        status=200
    )
    
    # Create test data using the fixture
    test_data = create_test_data(
        stages=[{"name": "get_users", "url": "https://api.example.com/users"}]
    )
    
    # Execute the test
    json_test_function(test_data)
    
    # Assert using the fixture
    assert_response_calls(["https://api.example.com/users"])


@responses.activate
def test_multiple_stages_with_fixtures(mock_response, create_test_data, assert_response_calls):
    """Example of testing multiple stages with reduced boilerplate."""
    
    # Setup multiple mock responses
    mock_response(
        url="https://api.example.com/users",
        json_data={"users": [{"id": 1, "name": "John"}]},
        status=200
    )
    mock_response(
        url="https://api.example.com/user/1",
        json_data={"id": 1, "name": "John", "details": "Additional info"},
        status=200
    )
    
    # Create test data with multiple stages
    test_data = create_test_data(
        stages=[
            {"name": "get_users", "url": "https://api.example.com/users"},
            {"name": "get_user_details", "url": "https://api.example.com/user/1"}
        ]
    )
    
    # Execute the test
    json_test_function(test_data)
    
    # Assert multiple calls
    assert_response_calls([
        "https://api.example.com/users",
        "https://api.example.com/user/1"
    ])


@responses.activate
def test_verification_with_fixtures(mock_response, create_test_data, assert_response_calls):
    """Example of testing verification with reduced boilerplate."""
    
    mock_response(
        url="https://api.example.com/test",
        json_data={"id": 1, "name": "test", "status": "active"},
        status=200
    )
    
    test_data = create_test_data(
        stages=[{
            "name": "test_stage",
            "url": "https://api.example.com/test",
            "verify": {
                "status": 200,
                "json": {
                    "json.id": 1,
                    "json.name": "test",
                    "json.status": "active"
                }
            }
        }]
    )
    
    # This should pass without any exceptions
    json_test_function(test_data)
    
    assert_response_calls(["https://api.example.com/test"])


@responses.activate
def test_variable_saving_with_fixtures(mock_response, create_test_data, assert_response_calls):
    """Example of testing variable saving with reduced boilerplate."""
    
    mock_response(
        url="https://api.example.com/user/1",
        json_data={"id": 1, "name": "John", "email": "john@example.com"},
        status=200
    )
    
    test_data = create_test_data(
        stages=[{
            "name": "get_user",
            "url": "https://api.example.com/user/1",
            "save": {
                "vars": {
                    "user_id": "json.id",
                    "user_name": "json.name",
                    "status": "status_code"
                }
            }
        }]
    )
    
    # This should pass and save variables
    json_test_function(test_data)
    
    assert_response_calls(["https://api.example.com/user/1"])


def test_fixture_usage_examples():
    """Examples of how the fixtures can be used in different scenarios."""
    
    # Example 1: Creating test data with fixtures and marks
    test_data = create_test_data(
        stages=[{"name": "test", "url": "https://api.example.com/test"}],
        fixtures=["user_id", "api_key"],
        marks=["slow", "integration"]
    )
    
    assert "stages" in test_data
    assert "fixtures" in test_data
    assert "marks" in test_data
    assert test_data["fixtures"] == ["user_id", "api_key"]
    assert test_data["marks"] == ["slow", "integration"]
    
    # Example 2: Creating test data with only stages
    simple_test_data = create_test_data(
        stages=[{"name": "simple", "url": "https://api.example.com/simple"}]
    )
    
    assert "stages" in simple_test_data
    assert "fixtures" not in simple_test_data
    assert "marks" not in simple_test_data


# This demonstrates how the refactored code reduces repetition
# compared to the original test files. The fixtures provide:
# 1. Standardized mock response setup
# 2. Consistent test data creation
# 3. Reusable assertion patterns
# 4. Reduced boilerplate in individual tests