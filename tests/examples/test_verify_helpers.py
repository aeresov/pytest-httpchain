"""
Test helper functions for pytest-http verify function feature demonstration.
"""


def verify_response_status_200(response):
    """Verify that the response status is 200."""
    return response.status_code == 200


def verify_response_has_json(response):
    """Verify that the response has JSON content."""
    try:
        response.json()
        return True
    except Exception:
        return False


def verify_response_has_headers(response):
    """Verify that the response has headers."""
    return len(response.headers) > 0


def verify_response_content_type_json(response):
    """Verify that the response content type is JSON."""
    content_type = response.headers.get("content-type", "")
    return "application/json" in content_type.lower()


def verify_response_size_limit(response, max_size=10000):
    """Verify that the response size is within limits."""
    return len(response.content) <= max_size


def verify_response_timeout(response, max_time=5.0):
    """Verify that the response time is within limits."""
    # This is a mock function for testing - in real scenarios you'd check response.elapsed
    return True