import requests.auth


def basic(username: str, password: str) -> requests.auth.AuthBase:
    return requests.auth.HTTPBasicAuth(username, password)
