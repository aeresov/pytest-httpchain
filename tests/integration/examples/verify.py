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


def verify_returns_false(response: httpx.Response) -> bool:
    """A verify function that fails by returning False."""
    return False


def verify_returns_non_bool(response: httpx.Response):
    """A verify function that returns a non-bool, which must be rejected."""
    return "not a bool"


def verify_raises(response: httpx.Response) -> bool:
    """A verify function that raises; must surface as a clean verification failure."""
    raise ValueError("boom in verify function")
