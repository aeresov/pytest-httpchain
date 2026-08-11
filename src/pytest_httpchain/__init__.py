"""pytest-httpchain: declarative HTTP API integration testing.

Test scenarios are JSON files (``test_<name>.http.json``) with ``$ref`` support,
``{{ expr }}`` templates, and multi-stage request chaining.
"""

from pytest_httpchain.warnings import ScenarioValidationWarning

__all__ = ["ScenarioValidationWarning"]
