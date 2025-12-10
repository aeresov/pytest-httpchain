import httpx


def basic(username: str, password: str) -> httpx.BasicAuth:
    """Create basic authentication."""
    return httpx.BasicAuth(username, password)
