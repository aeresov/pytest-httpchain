"""User functions that record their invocation via sentinel files.

The files are created in the pytest run's CWD (the pytester tmp dir), so a
test can assert exactly which phase — collection vs execution — invoked them.
"""

from pathlib import Path

import httpx


def auth() -> httpx.Auth:
    Path("auth_called.txt").touch()
    return httpx.BasicAuth("user", "pass")


def broken_auth() -> httpx.Auth:
    raise RuntimeError("token service unreachable")


def token() -> str:
    Path("token_called.txt").touch()
    return "sesame"


def mk_envs() -> list[str]:
    Path("mk_envs_called.txt").touch()
    return ["a", "b", "c"]


def count() -> str:
    """Append one line per invocation so a test can assert exact call counts."""
    with Path("count_calls.txt").open("a") as f:
        f.write("called\n")
    return "counted"
