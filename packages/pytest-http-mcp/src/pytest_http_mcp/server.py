import json
from typing import Any

import jsonref
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ValidationError
from pytest_http_engine.models import Scenario

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
                "flow_stages": len(scenario.flow.root),
                "final_stages": len(scenario.final.root),
                "has_aws_config": scenario.aws is not None,
            },
        )

    except Exception as e:
        return VerifyJsonResult(valid=False, errors=[f"Unexpected error: {e}"], scenario_info={})


@mcp.resource("schema://scenario")
def get_scenario_schema() -> dict:
    """Export the full JSON schema of the Scenario model"""
    return Scenario.model_json_schema()


@mcp.resource("example://scenario")
def get_scenario_example() -> str:
    """Complete example of a test scenario JSON file"""
    example = {
        "fixtures": ["user_credentials"],
        "marks": ["integration", "auth"],
        "flow": [
            {
                "name": "authenticate",
                "request": {
                    "url": "https://api.example.com/auth/login",
                    "method": "POST",
                    "headers": {"Content-Type": "application/json"},
                    "json": {"username": "{user_credentials.username}", "password": "{user_credentials.password}"},
                },
                "response": {
                    "save": {
                        "vars": {"auth_token": "data.token", "user_id": "data.user.id", "expires_at": "data.expires_at", "login_success": "data.success"},
                        "functions": [{"function": "auth_utils:validate_token_format", "kwargs": {"token": "{auth_token}"}}],
                    },
                    "verify": {"status": 200, "vars": {"login_success": True}},
                },
            },
            {
                "name": "get_profile",
                "request": {
                    "url": "https://api.example.com/users/{user_id}/profile",
                    "method": "GET",
                    "headers": {"Authorization": "Bearer {auth_token}", "Content-Type": "application/json"},
                },
                "response": {
                    "save": {"vars": {"last_login": "data.profile.last_login", "profile_status": "data.profile.status", "old_theme": "data.settings.theme"}},
                    "verify": {"status": 200, "vars": {"profile_status": "active"}},
                },
            },
            {
                "name": "update_settings",
                "request": {
                    "url": "https://api.example.com/users/{user_id}/settings",
                    "method": "PUT",
                    "headers": {"Authorization": "Bearer {auth_token}", "Content-Type": "application/json"},
                    "json": {"theme": "dark"},
                },
                "response": {
                    "save": {"vars": {"update_success": "data.updated", "new_theme": "data.settings.theme"}},
                    "verify": {
                        "status": 200,
                        "vars": {"update_success": True, "new_notifications": True, "new_theme": "dark"},
                        "functions": [
                            {"function": "settings_utils:validate_settings_update", "kwargs": {"old_settings": {"theme": "{old_theme}"}, "new_settings": {"theme": "{new_theme}"}}}
                        ],
                    },
                },
            },
        ],
        "final": [
            {
                "name": "logout",
                "request": {
                    "url": "https://api.example.com/auth/logout",
                    "method": "POST",
                    "headers": {"Authorization": "Bearer {auth_token}", "Content-Type": "application/json"},
                },
                "response": {
                    "save": {"vars": {"logout_success": "data.logged_out", "token_invalidated": "data.token_invalidated"}},
                    "verify": {"status": 200, "vars": {"logout_success": True, "token_invalidated": True}},
                },
            }
        ],
    }
    return json.dumps(example, indent=2)
