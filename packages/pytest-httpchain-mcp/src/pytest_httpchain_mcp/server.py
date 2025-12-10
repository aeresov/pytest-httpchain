import json
import re
from pathlib import Path
from typing import Any

import pytest_httpchain_jsonref.loader
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ValidationError
from pytest_httpchain_jsonref.exceptions import ReferenceResolverError
from pytest_httpchain_models.entities import Scenario

mcp = FastMCP("pytest-httpchain")

# Regex pattern for Jinja2 variable references: {{ var_name }} or {{ var.attr }}
JINJA_VAR_PATTERN = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)")


def extract_jinja_variables(obj: Any, variables: set[str] | None = None) -> set[str]:
    """Recursively extract Jinja2 variable names from a data structure."""
    if variables is None:
        variables = set()

    if isinstance(obj, str):
        for match in JINJA_VAR_PATTERN.finditer(obj):
            variables.add(match.group(1))
    elif isinstance(obj, dict):
        for value in obj.values():
            extract_jinja_variables(value, variables)
    elif isinstance(obj, list):
        for item in obj:
            extract_jinja_variables(item, variables)

    return variables


def extract_saved_variables(scenario: Scenario) -> set[str]:
    """Extract variable names that are saved in response steps."""
    saved_vars: set[str] = set()

    for stage in scenario.stages:
        for response_step in stage.response:
            # Check for save steps with jmespath
            if hasattr(response_step, "save"):
                save = response_step.save
                if hasattr(save, "jmespath"):
                    saved_vars.update(save.jmespath.keys())

    return saved_vars


def extract_defined_variables(scenario: Scenario, test_data: dict[str, Any]) -> set[str]:
    """Extract variable names that are defined in vars or substitutions."""
    defined_vars: set[str] = set()

    # Top-level vars
    if "vars" in test_data and isinstance(test_data["vars"], dict):
        defined_vars.update(test_data["vars"].keys())

    # Top-level substitutions
    for sub in scenario.substitutions:
        if hasattr(sub, "vars"):
            defined_vars.update(sub.vars.keys())

    # Stage-level substitutions
    for stage in scenario.stages:
        for sub in stage.substitutions:
            if hasattr(sub, "vars"):
                defined_vars.update(sub.vars.keys())

    return defined_vars


class ScenarioInfo(BaseModel):
    """Detailed information about the scenario structure."""

    num_stages: int = 0
    stage_names: list[str] = []
    vars_referenced: list[str] = []
    vars_saved: list[str] = []
    vars_defined: list[str] = []
    fixtures: list[str] = []


class ValidateResult(BaseModel):
    """Result of scenario validation."""

    valid: bool
    errors: list[str] = []
    warnings: list[str] = []
    scenario_info: ScenarioInfo | None = None


@mcp.tool(
    title="Validate scenario",
    description="Validate a pytest-httpchain test scenario JSON file for syntax, structure, and common issues",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    structured_output=True,
)
def validate_scenario(
    path: Path,
    ref_parent_traversal_depth: int = 3,
    root_path: Path | None = None,
) -> ValidateResult:
    """Validate a pytest-httpchain test scenario file.

    This tool performs comprehensive validation including:
    - File existence and accessibility
    - JSON syntax and structure
    - Schema validation against Scenario model
    - JSONRef resolution
    - Duplicate stage name detection
    - Undefined variable detection
    - Fixture/variable conflict detection

    Args:
        path: Path to the test scenario JSON file
        ref_parent_traversal_depth: Maximum depth for $ref parent directory traversals (default: 3)
        root_path: Root path for resolving references (default: tests directory or file parent)

    Returns:
        ValidateResult containing validation status, errors, warnings, and scenario info
    """
    errors: list[str] = []
    warnings: list[str] = []

    # Check file exists
    if not path.exists():
        return ValidateResult(
            valid=False,
            errors=[f"File not found: {path}"],
            warnings=[],
            scenario_info=None,
        )

    # Check file is readable
    if not path.is_file():
        return ValidateResult(
            valid=False,
            errors=[f"Path is not a file: {path}"],
            warnings=[],
            scenario_info=None,
        )

    # Check file extension
    if path.suffix.lower() not in (".json",):
        warnings.append(f"File has extension '{path.suffix}' but expected '.json'. Consider renaming to use .json extension.")

    # Determine root path
    if root_path is None:
        # Try to find a 'tests' directory, otherwise use file parent
        potential_root = path.parent
        while potential_root.parent != potential_root:
            if potential_root.name == "tests":
                root_path = potential_root
                break
            potential_root = potential_root.parent
        else:
            root_path = path.parent

    # Try to load JSON with JSONRef resolution
    try:
        test_data = pytest_httpchain_jsonref.loader.load_json(
            path,
            max_parent_traversal_depth=ref_parent_traversal_depth,
            root_path=root_path,
        )
    except ReferenceResolverError as e:
        return ValidateResult(
            valid=False,
            errors=[f"JSON reference resolution error: {str(e)}"],
            warnings=warnings,
            scenario_info=None,
        )
    except json.JSONDecodeError as e:
        return ValidateResult(
            valid=False,
            errors=[f"Invalid JSON syntax: {str(e)}"],
            warnings=warnings,
            scenario_info=None,
        )
    except Exception as e:
        return ValidateResult(
            valid=False,
            errors=[f"Failed to parse JSON file: {str(e)}"],
            warnings=warnings,
            scenario_info=None,
        )

    # Validate against Scenario schema
    try:
        scenario = Scenario.model_validate(test_data)
    except ValidationError as e:
        error_details = []
        for error in e.errors():
            loc = " -> ".join(str(x) for x in error["loc"])
            msg = error["msg"]
            error_details.append(f"{loc}: {msg}")

        return ValidateResult(
            valid=False,
            errors=["Schema validation failed:"] + error_details,
            warnings=warnings,
            scenario_info=None,
        )

    # Extract stage names and check for duplicates
    stage_names = [stage.name for stage in scenario.stages]
    seen_names: set[str] = set()
    duplicate_names: set[str] = set()
    for name in stage_names:
        if name in seen_names:
            duplicate_names.add(name)
        seen_names.add(name)

    if duplicate_names:
        errors.append(f"Duplicate stage names found: {sorted(duplicate_names)}")

    # Extract fixtures
    fixtures: list[str] = []
    if "fixtures" in test_data and isinstance(test_data["fixtures"], list):
        fixtures = test_data["fixtures"]

    # Also collect fixtures from stages
    for stage in scenario.stages:
        fixtures.extend(stage.fixtures)
    fixtures = list(set(fixtures))  # deduplicate

    # Extract defined variables (from vars and substitutions)
    vars_defined = extract_defined_variables(scenario, test_data)

    # Extract saved variables
    vars_saved = extract_saved_variables(scenario)

    # Extract referenced variables from templates
    vars_referenced = extract_jinja_variables(test_data)

    # Check for fixture/variable name conflicts
    fixture_set = set(fixtures)
    var_conflicts = fixture_set & vars_defined
    if var_conflicts:
        errors.append(f"Conflicting fixtures and vars with same names: {sorted(var_conflicts)}")

    # Check for undefined variables (warning, not error)
    # A variable is "defined" if it's in vars, saved by a previous stage, or is a fixture
    all_available_vars = vars_defined | vars_saved | fixture_set
    # Also add common built-in variables
    builtin_vars = {"response", "status_code", "headers", "body", "text", "json", "cookies"}
    all_available_vars |= builtin_vars

    undefined_vars = vars_referenced - all_available_vars
    if undefined_vars:
        warnings.append(f"Potentially undefined variables referenced: {sorted(undefined_vars)}")

    # Check for stages with no response validation
    for stage in scenario.stages:
        has_verify = False
        for response_step in stage.response:
            if hasattr(response_step, "verify"):
                has_verify = True
                break
        if not has_verify:
            warnings.append(f"Stage '{stage.name}' has no response validation (no verify step)")

    # Build scenario info
    scenario_info = ScenarioInfo(
        num_stages=len(scenario.stages),
        stage_names=stage_names,
        vars_referenced=sorted(vars_referenced),
        vars_saved=sorted(vars_saved),
        vars_defined=sorted(vars_defined),
        fixtures=sorted(fixtures),
    )

    # Determine overall validity
    valid = len(errors) == 0

    return ValidateResult(
        valid=valid,
        errors=errors,
        warnings=warnings,
        scenario_info=scenario_info,
    )
