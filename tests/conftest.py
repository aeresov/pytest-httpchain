import pytest
import responses


@pytest.fixture
def mock_response():
    """Fixture to provide a standardized mock response setup."""
    def _mock_response(url: str, json_data: dict = None, status: int = 200, headers: dict = None):
        default_headers = {"Content-Type": "application/json"}
        if headers:
            default_headers.update(headers)
        
        if json_data is None:
            json_data = {"data": "test"}
            
        responses.add(
            responses.GET,
            url,
            json=json_data,
            status=status,
            headers=default_headers
        )
    
    return _mock_response


@pytest.fixture
def create_test_data():
    """Fixture to create standardized test data structures."""
    def _create_test_data(stages: list, fixtures: list = None, marks: list = None):
        test_data = {"stages": stages}
        if fixtures:
            test_data["fixtures"] = fixtures
        if marks:
            test_data["marks"] = marks
        return test_data
    
    return _create_test_data


@pytest.fixture
def assert_response_calls():
    """Fixture to provide standardized response call assertions."""
    def _assert_response_calls(expected_urls: list, expected_count: int = None):
        if expected_count is None:
            expected_count = len(expected_urls)
        
        assert len(responses.calls) == expected_count
        
        for i, expected_url in enumerate(expected_urls):
            assert responses.calls[i].request.url == expected_url
    
    return _assert_response_calls
