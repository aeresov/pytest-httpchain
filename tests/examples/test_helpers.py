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


def extract_custom_data(response, field_path="", default_value="unknown"):
    """Extract custom data from a specific field path."""
    try:
        data = response.json()
        current = data
        for field in field_path.split("."):
            current = current.get(field, {})
        return {
            f"extracted_{field_path.replace('.', '_')}": current if current != {} else default_value,
            "function_called": True
        }
    except Exception:
        return {
            f"extracted_{field_path.replace('.', '_')}": default_value,
            "function_called": True
        }


def extract_multiple_fields(response, fields=None):
    """Extract multiple fields from the response."""
    if fields is None:
        fields = ["slideshow.title", "slideshow.author"]
    
    result = {"function_called": True}
    try:
        data = response.json()
        for field_path in fields:
            current = data
            for field in field_path.split("."):
                current = current.get(field, {})
            result[f"extracted_{field_path.replace('.', '_')}"] = current if current != {} else "unknown"
    except Exception:
        for field_path in fields:
            result[f"extracted_{field_path.replace('.', '_')}"] = "unknown"
    
    return result


def extract_with_filter(response, field_path="", filter_value="", filter_field=""):
    """Extract data with filtering capability."""
    try:
        data = response.json()
        current = data
        for field in field_path.split("."):
            current = current.get(field, [])
        
        if isinstance(current, list):
            filtered_items = [
                item for item in current 
                if isinstance(item, dict) and item.get(filter_field) == filter_value
            ]
            return {
                f"filtered_{field_path.replace('.', '_')}": filtered_items,
                "filter_count": len(filtered_items),
                "function_called": True
            }
        else:
            return {
                f"filtered_{field_path.replace('.', '_')}": [],
                "filter_count": 0,
                "function_called": True
            }
    except Exception:
        return {
            f"filtered_{field_path.replace('.', '_')}": [],
            "filter_count": 0,
            "function_called": True
        }
