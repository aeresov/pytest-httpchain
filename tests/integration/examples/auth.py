import httpx


def basic(username: str, password: str) -> httpx.BasicAuth:
    """Create basic authentication."""
    return httpx.BasicAuth(username, password)


def raising_auth() -> httpx.BasicAuth:
    """An auth factory that raises (e.g. a failed token fetch); must surface as
    a clean request-configuration failure."""
    raise RuntimeError("token service unavailable")


def request_body_was(response: httpx.Response, expected: str) -> bool:
    """The body of the request each response in the exchange answered, read as
    a user's own verify function reads it, e.g. to check a signature."""
    return all(hop.request.content == expected.encode() for hop in [*response.history, response])
