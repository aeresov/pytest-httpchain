import pytest


@pytest.fixture
def string_value():
    return "test_value"


@pytest.fixture
def number_value():
    return 123


@pytest.fixture
def dict_value():
    return {"key": "value", "number": 42}


@pytest.fixture
def base_url():
    return "https://jsonplaceholder.typicode.com"


# Define functions for pytest-http plugin
def extract_test_data(response):
    """Extract test data - this function is called even for stages without HTTP requests."""
    # For stages without HTTP requests, this demonstrates the functions feature
    # In a real scenario, this would extract data from the response
    return {
        "extracted_value": "test_extracted",
        "function_called": True
    }