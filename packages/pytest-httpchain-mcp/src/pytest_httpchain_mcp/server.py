import json
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ValidationError
from pytest_httpchain_jsonref.exceptions import ReferenceResolverError
from pytest_httpchain_jsonref.loader import load_json
from pytest_httpchain_models.entities import Scenario

mcp = FastMCP("pytest-httpchain")


class ValidateResult(BaseModel):
    """Result of scenario validation."""

    valid: bool
    errors: list[str]
    warnings: list[str]
    scenario_info: dict[str, Any]


@mcp.tool(
    title="Validate scenario",
    description="Validate a pytest-httpchain test scenario JSON file for syntax, structure, and common issues",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    structured_output=True,
)
def validate_scenario(path: Path) -> ValidateResult:
    """Validate a pytest-httpchain test scenario file.

    This tool performs comprehensive validation including:
    - JSON syntax validation
    - JSONRef resolution validation
    - Pydantic model validation against the scenario schema
    - Detection of common issues and anti-patterns
    - Variable usage analysis
    - Fixture dependency checking

    Args:
        path: Path to the test scenario JSON file

    Returns:
        ValidateResult containing validation status, errors, warnings, and scenario metadata
    """
    errors = []
    warnings = []
    scenario_info = {}

    # Check if file exists
    if not path.exists():
        return ValidateResult(valid=False, errors=[f"File not found: {path}"], warnings=[], scenario_info={})

    # Check file extension
    if path.suffix != ".json":
        warnings.append(f"File extension is '{path.suffix}', expected '.json'")

    # Step 1: Validate JSON syntax
    try:
        with open(path) as f:
            raw_json = json.load(f)
        scenario_info["has_valid_json"] = True
    except json.JSONDecodeError as e:
        errors.append(f"Invalid JSON syntax: {e}")
        return ValidateResult(valid=False, errors=errors, warnings=warnings, scenario_info=scenario_info)
    except Exception as e:
        errors.append(f"Error reading file: {e}")
        return ValidateResult(valid=False, errors=errors, warnings=warnings, scenario_info=scenario_info)

    # Step 2: Resolve JSONRefs
    try:
        resolved_data = load_json(path, max_parent_traversal_depth=3)
        scenario_info["has_refs"] = "$ref" in str(raw_json)
        scenario_info["refs_resolved"] = True
    except ReferenceResolverError as e:
        errors.append(f"JSONRef resolution error: {e}")
        # Try to continue with raw data for partial validation
        resolved_data = raw_json
        scenario_info["refs_resolved"] = False
    except Exception as e:
        errors.append(f"Unexpected error resolving references: {e}")
        resolved_data = raw_json
        scenario_info["refs_resolved"] = False

    # Step 3: Validate against Pydantic model
    try:
        scenario = Scenario.model_validate(resolved_data)
        scenario_info["model_valid"] = True

        # Extract scenario metadata
        scenario_info["num_stages"] = len(scenario.stages)
        scenario_info["stage_names"] = [stage.name for stage in scenario.stages]
        scenario_info["has_fixtures"] = bool(scenario.fixtures)
        scenario_info["has_marks"] = bool(scenario.marks)
        scenario_info["has_vars"] = bool(scenario.vars)

        # Analyze stages
        always_run_stages = []
        used_fixtures = set()
        saved_vars = set()
        referenced_vars = set()

        for stage in scenario.stages:
            if stage.always_run:
                always_run_stages.append(stage.name)

            used_fixtures.update(stage.fixtures)

            # Analyze variable usage in templates
            stage_dict = stage.model_dump()
            stage_str = str(stage_dict)

            # Find variable references ({{ var_name }})
            import re

            var_refs = re.findall(r"{{\s*(\w+)(?:\.\w+)*.*?}}", stage_str)
            referenced_vars.update(var_refs)

            # Find saved variables
            for step in stage.response:
                if hasattr(step, "save") and step.save and step.save.vars:
                    saved_vars.update(step.save.vars.keys())

        scenario_info["always_run_stages"] = always_run_stages
        scenario_info["fixtures_used"] = list(used_fixtures.union(set(scenario.fixtures)))
        scenario_info["vars_saved"] = list(saved_vars)
        scenario_info["vars_referenced"] = list(referenced_vars)

        # Step 4: Check for common issues

        # Check for undefined variables
        initial_vars = set(scenario.vars.keys()) if scenario.vars else set()
        undefined_vars = referenced_vars - initial_vars - saved_vars - used_fixtures
        if undefined_vars:
            warnings.append(f"Potentially undefined variables: {', '.join(undefined_vars)}")

        # Check for unused saved variables
        unused_vars = saved_vars - referenced_vars
        if unused_vars:
            warnings.append(f"Variables saved but never used: {', '.join(unused_vars)}")

        # Check for duplicate stage names
        stage_names = [stage.name for stage in scenario.stages]
        if len(stage_names) != len(set(stage_names)):
            duplicates = [name for name in stage_names if stage_names.count(name) > 1]
            errors.append(f"Duplicate stage names found: {', '.join(set(duplicates))}")

        # Check for stages with no request URL
        for stage in scenario.stages:
            if not stage.request.url:
                errors.append(f"Stage '{stage.name}' has no request URL")

        # Warn about stages with no response validation
        for stage in scenario.stages:
            if not stage.response.root and not stage.always_run:
                warnings.append(f"Stage '{stage.name}' has no response validation")

        # Check for fixture/variable conflicts
        if scenario.fixtures and scenario.vars:
            conflicts = set(scenario.fixtures) & set(scenario.vars.keys())
            if conflicts:
                errors.append(f"Conflicting fixture and variable names: {', '.join(conflicts)}")

    except ValidationError as e:
        scenario_info["model_valid"] = False
        # Parse validation errors
        for error in e.errors():
            loc = " -> ".join(str(item) for item in error["loc"])
            msg = error["msg"]
            errors.append(f"Validation error at {loc}: {msg}")
    except Exception as e:
        errors.append(f"Unexpected validation error: {e}")
        scenario_info["model_valid"] = False

    # Determine overall validity
    valid = len(errors) == 0

    return ValidateResult(valid=valid, errors=errors, warnings=warnings, scenario_info=scenario_info)
