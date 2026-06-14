"""pytest-httpchain: Declarative HTTP API integration testing.

This pytest plugin enables declarative HTTP API integration testing using JSON files.
Test scenarios are defined with $ref support, template expressions ({{ expr }}),
and multi-stage request chaining.

Example test file (test_api.http.json):
    {
        "description": "API integration test",
        "stages": [
            {
                "name": "Get user",
                "request": {
                    "method": "GET",
                    "url": "https://api.example.com/users/1"
                },
                "response": [
                    {"verify": {"status": 200}}
                ]
            }
        ]
    }
"""

# Re-export the user-facing warning so consumers can filter it without reaching
# into the plugin module. The definition stays in plugin.py because the pytest11
# entry point and the collection hook reference it there directly.
from pytest_httpchain.plugin import ScenarioValidationWarning

__all__ = ["ScenarioValidationWarning"]
