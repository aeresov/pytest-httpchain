"""
Test helper functions for pytest-http function feature demonstration.
"""


def extract_test_data(response):
    """Extract test data - demonstrates the functions feature."""
    try:
        data = response.json()
        slideshow = data.get("slideshow", {})
        return {
            "extracted_author": slideshow.get("author", "unknown"),
            "extracted_date": slideshow.get("date", "unknown"),
            "slide_count": len(slideshow.get("slides", [])),
            "function_called": True
        }
    except Exception:
        # Fallback for when no actual HTTP response
        return {
            "extracted_value": "test_extracted",
            "function_called": True
        }