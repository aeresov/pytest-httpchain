import json
from typing import Any

import jsonref
from engine.models import Scenario
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ValidationError

mcp = FastMCP("pytest-http")


class VerifyJsonResult(BaseModel):
    valid: bool
    errors: list[str]
    scenario_info: dict[str, Any]


@mcp.tool()
def verify_json(json_data: str, base_uri: str = "") -> VerifyJsonResult:
    """
    Verify user-supplied JSON against Scenario model.

    First resolves jsonref references, then validates against Scenario model.

    Args:
        json_data: JSON string to verify
        base_uri: Base URI for resolving relative references

    Returns:
        VerifyJsonResult
    """
    try:
        # load JSON
        try:
            parsed_data = json.loads(json_data)
        except json.JSONDecodeError as e:
            return VerifyJsonResult(valid=False, errors=[f"JSON parsing error: {e}"], scenario_info={})

        # resolve refs
        processed_data = jsonref.replace_refs(parsed_data, base_uri=base_uri)

        # validate against Scenario model
        try:
            scenario = Scenario.model_validate(processed_data)
        except ValidationError as e:
            return VerifyJsonResult(valid=False, errors=[f"Scenario validation error: {e}"], scenario_info={})

        return VerifyJsonResult(
            valid=True,
            errors=[],
            scenario_info={
                "fixtures": scenario.fixtures,
                "marks": scenario.marks,
                "flow_stages": len(scenario.flow),
                "final_stages": len(scenario.final),
                "has_aws_config": scenario.aws is not None,
            },
        )

    except Exception as e:
        return VerifyJsonResult(valid=False, errors=[f"Unexpected error: {e}"], scenario_info={})


@mcp.resource("schema://scenario")
def get_scenario_schema() -> dict:
    """Export the full JSON schema of the Scenario model"""
    return Scenario.model_json_schema()
