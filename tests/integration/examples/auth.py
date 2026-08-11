import httpx


def basic(username: str, password: str) -> httpx.BasicAuth:
    """Create basic authentication."""
    return httpx.BasicAuth(username, password)


def raising_auth() -> httpx.BasicAuth:
    """An auth factory that raises (e.g. a failed token fetch); must surface as
    a clean request-configuration failure."""
    raise RuntimeError("token service unavailable")
