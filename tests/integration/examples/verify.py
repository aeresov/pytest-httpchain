import httpx


def verify_has_id(response: httpx.Response) -> bool:
    """Verify that response JSON has an 'id' field."""
    data = response.json()
    return "id" in data


def verify_status_ok(response: httpx.Response) -> bool:
    """Verify that response has 200 status."""
    return response.status_code == 200


def verify_json_field(response: httpx.Response, field: str, expected: str) -> bool:
    """Verify a specific JSON field has expected value."""
    data = response.json()
    return data.get(field) == expected
