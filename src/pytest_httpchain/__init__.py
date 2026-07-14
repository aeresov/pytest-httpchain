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

# Re-export the user-facing warning so consumers can filter it from the package
# root. It lives in the leaf module ``warnings`` so this import (which runs on
# any subpackage import) does not load the plugin/execution machinery.
from pytest_httpchain.warnings import ScenarioValidationWarning

__all__ = ["ScenarioValidationWarning"]
