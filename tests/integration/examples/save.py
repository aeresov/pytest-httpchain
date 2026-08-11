import httpx


def extract_user_info(response: httpx.Response) -> dict:
    """Extract user info from response."""
    data = response.json()
    return {
        "user_id": data.get("id"),
        "user_name": data.get("name"),
    }


def compute_values(value: int) -> dict:
    """Compute derived values."""
    return {
        "doubled": value * 2,
        "squared": value**2,
    }


def save_raises(response: httpx.Response) -> dict:
    """A save function that raises; must surface as a clean save failure."""
    raise ValueError("boom in save function")


def save_returns_non_dict(response: httpx.Response):
    """A save function that returns a non-dict, which must be rejected."""
    return "not a dict"
