import json
from typing import Any

import jsonref
from pydantic import ValidationError

from pytest_http.models import Scenario


def is_mcp_available() -> bool:
    try:
        import mcp.server.fastmcp  # noqa: F401
    except ImportError:
        return False
    return True


def create_mcp_server():
    if not is_mcp_available():
        raise ImportError("MCP is not available. Install with: pip install pytest-http[mcp]")

    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("pytest-http")

    @mcp.tool()
    def verify_json(json_data: str, base_uri: str = "") -> dict[str, Any]:
        """
        Verify user-supplied JSON against Scenario model.

        First resolves jsonref references, then validates against Scenario model.

        Args:
            json_data: JSON string to verify
            base_uri: Base URI for resolving relative references (optional)

        Returns:
            Dictionary with validation results including success status and errors
        """
        result = {"valid": False, "errors": [], "stage": "parsing"}

        try:
            # Parse JSON
            parsed_data = json.loads(json_data)
            result["stage"] = "jsonref_resolving"

            # Resolve jsonref references
            if base_uri:
                processed_data = jsonref.replace_refs(parsed_data, base_uri=base_uri)
            else:
                processed_data = jsonref.replace_refs(parsed_data)

            result["stage"] = "scenario_validation"

            # Validate against Scenario model
            scenario = Scenario.model_validate(processed_data)

            result["valid"] = True
            result["stage"] = "completed"
            result["scenario_info"] = {
                "fixtures": scenario.fixtures,
                "marks": scenario.marks,
                "flow_stages": len(scenario.flow),
                "final_stages": len(scenario.final),
                "has_aws_config": scenario.aws is not None,
            }

        except json.JSONDecodeError as e:
            result["errors"].append(f"JSON parsing error: {e}")
        except Exception as e:
            if result["stage"] == "jsonref_resolving":
                result["errors"].append(f"JSONRef error: {e}")
            elif result["stage"] == "scenario_validation":
                if isinstance(e, ValidationError):
                    result["errors"].append(f"Scenario validation error: {e}")
                else:
                    result["errors"].append(f"Unexpected validation error: {e}")
            else:
                result["errors"].append(f"Unexpected error: {e}")

        return result

    @mcp.resource("schema://scenario")
    def get_scenario_schema() -> dict:
        """Export the full JSON schema of the Scenario model"""
        return Scenario.model_json_schema()

    return mcp


# Create the MCP server instance if available
mcp = create_mcp_server() if is_mcp_available() else None
