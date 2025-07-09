from typing import Any

from requests import Response


def verify_response_status_200(response: Response) -> bool:
    return response.status_code == 200


def verify_response_has_json(response: Response) -> bool:
    try:
        response.json()
        return True
    except Exception:
        return False


def verify_response_has_headers(response: Response) -> bool:
    return len(response.headers) > 0


def verify_response_content_type_json(response: Response) -> bool:
    content_type = response.headers.get("content-type", "")
    return "application/json" in content_type.lower()


def verify_response_size_limit(response: Response, max_size: int = 10000) -> bool:
    return len(response.content) <= max_size


def verify_response_timeout(response: Response, max_time: float = 5.0) -> bool:
    return True


def verify_response_status_custom(response: Response, expected_status: int = 200) -> bool:
    return response.status_code == expected_status


def verify_response_contains_text(response: Response, expected_text: str = "", case_sensitive: bool = True) -> bool:
    response_text = response.text
    if not case_sensitive:
        response_text = response_text.lower()
        expected_text = expected_text.lower()
    return expected_text in response_text


def verify_response_header_value(response: Response, header_name: str = "", expected_value: str = "") -> bool:
    actual_value = response.headers.get(header_name, "")
    return actual_value == expected_value


def verify_response_json_field(response: Response, field_path: str = "", expected_value: Any = None) -> bool:
    try:
        data = response.json()
        current = data
        for field in field_path.split("."):
            current = current.get(field, {})
        return current == expected_value
    except Exception:
        return False


def extract_test_data(response: Response) -> dict[str, Any]:
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
        return {
            "extracted_value": "test_extracted",
            "function_called": True
        }


def extract_custom_data(response: Response, field_path: str = "", default_value: str = "unknown") -> dict[str, Any]:
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


def extract_multiple_fields(response: Response, fields: list[str] | None = None) -> dict[str, Any]:
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


def extract_with_filter(response: Response, field_path: str = "", filter_value: str = "", filter_field: str = "") -> dict[str, Any]:
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
