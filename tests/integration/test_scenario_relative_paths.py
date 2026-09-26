"""Dialect file paths resolve against the scenario file's directory.

``body.binary``, ``body.files`` values, and ``verify.body.schema`` behave like
``$ref``: a relative path is relative to the scenario file, not to wherever
pytest was invoked. The scenario here lives in a subdirectory together with its
data files, while pytest runs from the pytester root — under the old
CWD-relative rule every stage below would fail with file-not-found.
"""

import json

from tests.integration.helpers import stage, write_scenario


def _upload(binary, **fields):
    return stage("upload_binary", "/echo/binary", request={"method": "POST", "body": {"binary": binary}}, **fields)


def test_relative_paths_resolve_against_scenario_dir(pytester):
    pytester.copy_example("conftest.py")
    sub = pytester.mkdir("sub")
    (sub / "payload.bin").write_bytes(b"\x00\x01payload")
    (sub / "users.schema.json").write_text(json.dumps({"type": "object", "required": ["users"]}))
    upload = _upload("payload.bin", response=[{"save": {"jmespath": {"echoed_size": "size"}}}, {"verify": {"status": 200, "expressions": ["{{ echoed_size == 9 }}"]}}])
    schema_check = stage("verify_with_schema_file", "/users", response=[{"verify": {"status": 200, "body": {"schema": "users.schema.json"}}}])
    write_scenario(sub, {"stages": [upload, schema_check]})

    result = pytester.runpytest("sub", "-s")
    result.assert_outcomes(passed=2)


def test_relative_path_missing_fails_with_scenario_relative_name(pytester):
    pytester.copy_example("conftest.py")
    write_scenario(pytester.mkdir("sub"), {"stages": [_upload("nope.bin")]})

    result = pytester.runpytest("sub", "-s")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*Binary file not found: nope.bin*"])
