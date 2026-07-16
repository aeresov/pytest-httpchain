"""Declarative response metadata (task 25).

Response steps see a ``response`` namespace (status, reason, headers,
elapsed_ms) in their template context — usable in ``verify.expressions`` and
as a header save source via substitution saves — and ``verify.headers``
accepts matcher objects (contains/not_contains/matches/not_matches) besides
exact-match strings.
"""

import json


def _write_scenario(pytester, name, scenario):
    (pytester.path / name).write_text(json.dumps(scenario))


def test_response_namespace_in_verify_expressions(pytester):
    pytester.copy_example("conftest.py")
    _write_scenario(
        pytester,
        "test_meta.http.json",
        {
            "stages": [
                {
                    "name": "meta",
                    "fixtures": ["server"],
                    "request": {"url": "{{ server }}/headers"},
                    "response": [
                        {
                            "verify": {
                                "expressions": [
                                    "{{ response.status == 200 }}",
                                    "{{ 'json' in response.headers['content-type'] }}",
                                    "{{ response.headers['x-custom-header'] == 'test-value' }}",
                                    "{{ response.elapsed_ms >= 0 }}",
                                ]
                            }
                        }
                    ],
                }
            ]
        },
    )
    result = pytester.runpytest("-s")
    result.assert_outcomes(passed=1)


def test_save_header_via_substitutions(pytester):
    """The header save source: a substitutions save reads response.headers;
    the saved value is visible to the next stage."""
    pytester.copy_example("conftest.py")
    _write_scenario(
        pytester,
        "test_save_header.http.json",
        {
            "stages": [
                {
                    "name": "capture",
                    "fixtures": ["server"],
                    "request": {"url": "{{ server }}/headers"},
                    "response": [
                        {"save": {"substitutions": [{"vars": {"req_id": "{{ response.headers['x-request-id'] }}"}}]}},
                        {"verify": {"status": 200}},
                    ],
                },
                {
                    "name": "use",
                    "fixtures": ["server"],
                    "request": {"url": "{{ server }}/ok"},
                    "response": [{"verify": {"expressions": ["{{ req_id == '12345' }}"]}}],
                },
            ]
        },
    )
    result = pytester.runpytest("-s")
    result.assert_outcomes(passed=2)


def test_header_matchers(pytester):
    pytester.copy_example("conftest.py")
    _write_scenario(
        pytester,
        "test_matchers.http.json",
        {
            "stages": [
                {
                    "name": "matchers",
                    "fixtures": ["server"],
                    "request": {"url": "{{ server }}/headers"},
                    "response": [
                        {
                            "verify": {
                                "headers": {
                                    "content-type": {"contains": "json"},
                                    "x-request-id": {"matches": "^[0-9]+$"},
                                    "x-custom-header": "test-value",
                                    "x-absent": {"not_contains": "anything"},
                                }
                            }
                        }
                    ],
                }
            ]
        },
    )
    result = pytester.runpytest("-s")
    result.assert_outcomes(passed=1)


def test_header_matcher_failure_names_the_header(pytester):
    pytester.copy_example("conftest.py")
    _write_scenario(
        pytester,
        "test_matcher_fail.http.json",
        {
            "stages": [
                {
                    "name": "fails",
                    "fixtures": ["server"],
                    "request": {"url": "{{ server }}/headers"},
                    "response": [{"verify": {"headers": {"x-custom-header": {"contains": "nope"}}}}],
                }
            ]
        },
    )
    result = pytester.runpytest("-s")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*x-custom-header*does not contain*nope*"])


def test_response_namespace_not_visible_in_request(pytester):
    """The namespace exists for response steps only: referencing it in a
    request template is still an error at runtime."""
    pytester.copy_example("conftest.py")
    _write_scenario(
        pytester,
        "test_meta_request.http.json",
        {
            "stages": [
                {
                    "name": "bad",
                    "fixtures": ["server"],
                    "request": {"url": "{{ server }}/ok", "headers": {"x-echo": "{{ response.status }}"}},
                    "response": [{"verify": {"status": 200}}],
                }
            ]
        },
    )
    result = pytester.runpytest("-s")
    result.assert_outcomes(failed=1)
