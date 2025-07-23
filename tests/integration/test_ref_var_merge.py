"""Test to ensure $ref merging preserves both referenced and local variables."""


def test_ref_merge_preserves_both_variables(pytester):
    """Test that $ref with additional save vars merges both sets of variables."""
    # Create conftest.py
    pytester.makepyfile(conftest="""
from http import HTTPStatus

import pytest
from http_server_mock import HttpServerMock

app = HttpServerMock(__name__)


@app.get("/login")
def login():
    return {
        "Status": {"Text": "ok"},
        "User": {"SessionKey": "abc123"}
    }, HTTPStatus.OK


@app.get("/logout")
def logout():
    return {
        "Status": {"Text": "ok"}
    }, HTTPStatus.OK


@pytest.fixture
def settings():
    return {
        "host": "localhost",
        "port": 5000,
        "username": "test",
        "password": "pass"
    }


@pytest.fixture
def server():
    with app.run("localhost", 5000):
        yield
""")

    # Create common/status.json
    pytester.mkdir("common")
    pytester.makefile(".json", **{
        "common/status": """{
    "ok": {
        "save": {
            "vars": {
                "altec_status_text": "Status.Text"
            }
        },
        "verify": {
            "status": 200,
            "vars": {
                "altec_status_text": "ok"
            }
        }
    }
}"""
    })

    # Create test file
    pytester.makefile(".http.json", test_session_var_merge="""{
    "fixtures": ["settings", "server"],
    "vars": {
        "api_base": "http://localhost:5000"
    },
    "stages": [
        {
            "name": "login_stage",
            "request": {
                "url": "{{ api_base }}/login",
                "method": "GET"
            },
            "response": {
                "$ref": "common/status.json#/ok",
                "save": {
                    "vars": {
                        "session_key": "User.SessionKey"
                    }
                }
            }
        },
        {
            "name": "logout_stage",
            "request": {
                "url": "{{ api_base }}/logout",
                "method": "GET"
            },
            "response": {
                "$ref": "common/status.json#/ok"
            }
        }
    ]
}""")

    result = pytester.runpytest("-v")

    # Both stages should pass - the login stage should save both altec_status_text and session_key
    result.assert_outcomes(passed=2, failed=0)