"""Test cases for overwriting initial variables with saved variables."""

from pytest_http_engine.models import Request, Response, Save, Scenario, Stage


def test_progressive_api_key_refinement():
    """Test a realistic scenario where initial vars are refined during execution."""
    scenario = Scenario(
        vars={
            "api_key": "default-key",
            "base_url": "https://api.example.com",
            "user_id": None,
        },
        flow=[
            Stage(
                name="authenticate",
                request=Request(
                    url="{{ base_url }}/auth",
                    json={"api_key": "{{ api_key }}"},
                ),
                response=Response(
                    save=Save(
                        vars={
                            "api_key": "data.access_token",  # Upgrade from default to real token
                            "user_id": "data.user.id",  # Set user_id from null to actual value
                        }
                    )
                ),
            ),
            Stage(
                name="get_profile",
                request=Request(
                    url="{{ base_url }}/users/{{ user_id }}",
                    headers={"Authorization": "Bearer {{ api_key }}"},  # Uses updated token
                ),
                response=Response(
                    save=Save(
                        vars={
                            "base_url": "data.preferred_api_endpoint",  # Switch to user's preferred endpoint
                        }
                    )
                ),
            ),
            Stage(
                name="update_settings",
                request=Request(
                    url="{{ base_url }}/users/{{ user_id }}/settings",  # Uses updated endpoint
                    headers={"Authorization": "Bearer {{ api_key }}"},
                ),
            ),
        ],
    )

    # Verify the scenario structure
    assert scenario.vars["api_key"] == "default-key"
    assert scenario.vars["user_id"] is None

    # Check that the saved variables can overwrite initial vars
    auth_stage = scenario.flow.root[0]
    assert auth_stage.response.save.vars["api_key"] == "data.access_token"
    assert auth_stage.response.save.vars["user_id"] == "data.user.id"

    profile_stage = scenario.flow.root[1]
    assert profile_stage.response.save.vars["base_url"] == "data.preferred_api_endpoint"
