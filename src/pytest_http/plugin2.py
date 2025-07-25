"""
Simple draft implementation of pytest-http plugin using pytest's collection hierarchy.

Following Package-Module-Class-Function pattern:
- Package: Directory containing JSON test files
- Module: Individual JSON test file (test_*.http.json)
- Class: Scenario within the JSON file (one per file)
- Function: Individual stage within the scenario
"""

import json

import pytest
import requests
from _pytest.nodes import Item
from _pytest.python import Class, Function, Module
from pytest_http_engine.models import Scenario, Stage


class JSONModule(Module):
    """Module collector for individual JSON test files."""

    def collect(self):
        """Parse JSON file and create scenario class."""
        try:
            with open(self.path) as f:
                json_data = json.load(f)

            # Validate against pydantic model
            scenario = Scenario(**json_data)

            # Create one class per JSON file containing the scenario
            yield JSONScenarioClass.from_parent(self, name=f"{self.path.stem}_scenario", scenario=scenario)

        except (json.JSONDecodeError, Exception) as e:
            # Create a failed validation item for invalid JSON
            yield JSONValidationError.from_parent(self, name=f"{self.path.stem}_validation_error", error=str(e))


class JSONScenarioClass(Class):
    """Class collector representing a scenario from JSON file."""

    def __init__(self, scenario: Scenario, **kwargs):
        super().__init__(**kwargs)
        self.scenario = scenario
        self._variable_context = {}
        self._http_session = None

    def collect(self):
        """Create function items for each stage in the scenario."""
        for i, stage in enumerate(self.scenario.stages):
            yield JSONStageFunction.from_parent(self, name=f"stage_{i}_{stage.name}", stage=stage, stage_index=i)

    def setup_class(cls):
        """Initialize scenario context."""
        cls._variable_context = cls.scenario.vars.copy() if cls.scenario.vars else {}
        cls._http_session = requests.Session()

    def teardown_class(cls):
        """Clean up scenario resources."""
        if cls._http_session:
            cls._http_session.close()


class JSONStageFunction(Function):
    """Function item representing an individual stage."""

    def __init__(self, stage: Stage, stage_index: int, **kwargs):
        super().__init__(**kwargs)
        self.stage = stage
        self.stage_index = stage_index

    def runtest(self):
        """Execute the HTTP stage."""
        scenario_class = self.parent

        # Skip if previous stage failed and this isn't always_run
        if scenario_class._flow_failed and not getattr(self.stage, "always_run", False):
            pytest.skip(f"Skipping stage '{self.stage.name}' due to previous failure")

        try:
            # Execute HTTP request
            self._execute_stage(scenario_class)
        except Exception:
            scenario_class._flow_failed = True
            raise

    def _execute_stage(self, scenario_class):
        """Execute the actual HTTP stage logic."""
        # Simplified stage execution - just a placeholder
        # Real implementation would handle request/response processing
        print(f"Executing stage: {self.stage.name}")

        if self.stage.request:
            print(f"  Request: {self.stage.request.method} {self.stage.request.url}")

        # This is where actual HTTP request would be made
        # and response validation would occur


class JSONValidationError(Item):
    """Item for JSON files that failed validation."""

    def __init__(self, error: str, **kwargs):
        super().__init__(**kwargs)
        self.error = error

    def runtest(self):
        """Always fail with validation error."""
        pytest.fail(f"JSON validation failed: {self.error}")


def pytest_collect_file(file_path, parent):
    """Hook to collect JSON test files."""
    if file_path.suffix == ".json" and file_path.name.startswith("test_") and ".http." in file_path.name:
        return JSONModule.from_parent(parent, fspath=file_path)
