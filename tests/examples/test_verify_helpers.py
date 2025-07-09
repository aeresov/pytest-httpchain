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


def verify_response_status_custom(response, expected_status=200):
    """Verify that the response status matches the expected status."""
    return response.status_code == expected_status


def verify_response_contains_text(response, expected_text="", case_sensitive=True):
    """Verify that the response text contains the expected text."""
    response_text = response.text
    if not case_sensitive:
        response_text = response_text.lower()
        expected_text = expected_text.lower()
    return expected_text in response_text


def verify_response_header_value(response, header_name="", expected_value=""):
    """Verify that a specific header has the expected value."""
    actual_value = response.headers.get(header_name, "")
    return actual_value == expected_value


def verify_response_json_field(response, field_path="", expected_value=None):
    """Verify that a JSON field has the expected value."""
    try:
        data = response.json()
        # Simple field path support (e.g., "slideshow.title")
        current = data
        for field in field_path.split("."):
            current = current.get(field, {})
        return current == expected_value
    except Exception:
        return False
