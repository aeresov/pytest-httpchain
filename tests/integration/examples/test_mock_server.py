from http import HTTPStatus

import requests


def test_ok(server):
    r = requests.get("http://localhost:5000/ok")
    assert r.status_code == HTTPStatus.OK


def test_bad(server):
    r = requests.get("http://localhost:5000/bad")
    assert r.status_code == HTTPStatus.BAD_REQUEST
