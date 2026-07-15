"""Dialect file paths resolve against the scenario file's directory.

``body.binary``, ``body.files`` values, and ``verify.body.schema`` behave like
``$ref``: a relative path is relative to the scenario file, not to wherever
pytest was invoked. The scenario here lives in a subdirectory together with its
data files, while pytest runs from the pytester root — under the old
CWD-relative rule every stage below would fail with file-not-found.
"""

import json

SCENARIO = {
    "stages": [
        {
            "name": "upload_binary",
            "fixtures": ["server"],
            "request": {
                "url": "{{ server }}/echo/binary",
                "method": "POST",
                "body": {"binary": "payload.bin"},
            },
            "response": [
                {"save": {"jmespath": {"echoed_size": "size"}}},
                {"verify": {"status": 200, "expressions": ["{{ echoed_size == 9 }}"]}},
            ],
        },
        {
            "name": "verify_with_schema_file",
            "fixtures": ["server"],
            "request": {"url": "{{ server }}/users"},
            "response": [{"verify": {"status": 200, "body": {"schema": "users.schema.json"}}}],
        },
    ]
}


def test_relative_paths_resolve_against_scenario_dir(pytester):
    pytester.copy_example("conftest.py")
    sub = pytester.mkdir("sub")
    (sub / "payload.bin").write_bytes(b"\x00\x01payload")
    (sub / "users.schema.json").write_text(json.dumps({"type": "object", "required": ["users"]}))
    (sub / "test_relative.http.json").write_text(json.dumps(SCENARIO))

    result = pytester.runpytest("sub/test_relative.http.json", "-s")
    result.assert_outcomes(passed=2)


def test_relative_path_missing_fails_with_scenario_relative_name(pytester):
    pytester.copy_example("conftest.py")
    sub = pytester.mkdir("sub")
    (sub / "test_relative.http.json").write_text(
        json.dumps(
            {
                "stages": [
                    {
                        "name": "upload_binary",
                        "fixtures": ["server"],
                        "request": {
                            "url": "{{ server }}/echo/binary",
                            "method": "POST",
                            "body": {"binary": "nope.bin"},
                        },
                        "response": [{"verify": {"status": 200}}],
                    }
                ]
            }
        )
    )

    result = pytester.runpytest("sub/test_relative.http.json", "-s")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*Binary file not found: nope.bin*"])
