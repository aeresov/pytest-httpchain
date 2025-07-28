import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import ParseResult, parse_qsl, urlencode, urlparse, urlunparse

import pytest
import requests
from requests.auth import AuthBase

METHOD = "check"
TEST_KEY = "d28f61a6cb5dbc130c7d856179387627"
TEST_SECRET = "0e3cfc69357e61408fc9652e725b4ef2"
SKIPPED_PARAMS = ["sig", "sk"]


def calc(
    query_params: Mapping[str, str],
    skipped_params: Sequence[str] = SKIPPED_PARAMS,
    method: str = METHOD,
    secret: str = TEST_SECRET,
    session_key: str = "",
) -> str:
    params_filtered = {k: v for k, v in query_params.items() if k not in skipped_params}
    keys_sorted = sorted(params_filtered.keys())
    comp_sig = "".join([k + params_filtered[k] for k in keys_sorted])
    hash = hashlib.sha1()
    hash.update(str.encode(method + comp_sig + secret + session_key))
    hashed_comp_sig = hash.hexdigest()
    return hashed_comp_sig


def pwdhash(password: str) -> str:
    hash = hashlib.sha1()
    hash.update(str.encode(password))
    return hash.hexdigest()


class AltecAuth(AuthBase):
    def __init__(self, method: str, session_key: str = ""):
        self.method = method
        self.session_key = session_key

    def __call__(self, r: requests.PreparedRequest) -> requests.PreparedRequest:
        parsed: ParseResult = urlparse(r.url)
        query_params_list = parse_qsl(parsed.query, keep_blank_values=True)
        query_params_dict = {item[0]: item[1] for item in query_params_list}

        if not query_params_dict:
            return r

        query_params_dict["key"] = TEST_KEY
        query_params_dict["sig"] = calc(query_params=query_params_dict, method=self.method, session_key=self.session_key)

        r.url = urlunparse(parsed._replace(query=urlencode(query_params_dict, doseq=True)))
        return r


def altec_auth(method: str, session_key: str = ""):
    return AltecAuth(method, session_key)


@dataclass(frozen=True)
class Settings:
    host: str = "localhost"
    port: int = 18902
    username: str = "Alexander.Eresov@soundunited.com"
    password: str = pwdhash("Frozen130183")


@pytest.fixture
def settings():
    return Settings()
