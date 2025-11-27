import httpx


def basic(username: str, password: str) -> httpx.BasicAuth:
    return httpx.BasicAuth(username, password)
