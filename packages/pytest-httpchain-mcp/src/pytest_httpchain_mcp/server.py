from pathlib import Path

import pytest_httpchain_jsonref.loader
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ValidationError
from pytest_httpchain_jsonref.exceptions import ReferenceResolverError
from pytest_httpchain_models.entities import Scenario

mcp = FastMCP("pytest-httpchain")


class ScenarioMetadata(BaseModel):
    """Metadata about the scenario."""

    name: str | None = None
    description: str | None = None
    num_stages: int = 0


class ValidateResult(BaseModel):
    """Result of scenario validation."""

    valid: bool
    errors: list[str] = []
    warnings: list[str] = []
    metadata: ScenarioMetadata | None = None


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

    Args:
        path: Path to the test scenario JSON file
        ref_parent_traversal_depth: Maximum depth for $ref parent directory traversals (default: 3)
        root_path: Root path for resolving references (default: tests directory or file parent)

    Returns:
        ValidateResult containing validation status, errors, warnings, and scenario metadata
    """
    errors: list[str] = []
    warnings: list[str] = []
    metadata: ScenarioMetadata | None = None

    # Check file exists
    if not path.exists():
        return ValidateResult(
            valid=False,
            errors=[f"File does not exist: {path}"],
            warnings=[],
            metadata=None,
        )

    # Check file is readable
    if not path.is_file():
        return ValidateResult(
            valid=False,
            errors=[f"Path is not a file: {path}"],
            warnings=[],
            metadata=None,
        )

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
            warnings=[],
            metadata=None,
        )
    except Exception as e:
        return ValidateResult(
            valid=False,
            errors=[f"Failed to parse JSON file: {str(e)}"],
            warnings=[],
            metadata=None,
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
            warnings=[],
            metadata=None,
        )

    # Extract metadata
    # Note: Scenario doesn't have a name field, name comes from the file
    file_name = path.stem.replace(".http", "").replace("test_", "")
    metadata = ScenarioMetadata(
        name=file_name,
        description=scenario.description,
        num_stages=len(scenario.stages),
    )

    # Determine overall validity
    valid = len(errors) == 0

    return ValidateResult(
        valid=valid,
        errors=errors,
        warnings=warnings,
        metadata=metadata,
    )
