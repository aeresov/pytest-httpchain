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
