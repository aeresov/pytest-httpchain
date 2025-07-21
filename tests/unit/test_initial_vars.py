"""Tests for the initial vars functionality in Scenario model."""

import pytest
from pytest_http_engine.models import Request, Response, Save, Scenario, Stage


def test_scenario_with_initial_vars():
    """Test that a scenario can be created with initial vars."""
    scenario = Scenario(
        vars={"api_key": "test-key", "base_url": "https://api.example.com"},
        stages=[
            Stage(
                name="get_users",
                request=Request(
                    url="{{ base_url }}/users",
                    headers={"Authorization": "Bearer {{ api_key }}"},
                ),
            )
        ],
    )
    assert scenario.vars == {"api_key": "test-key", "base_url": "https://api.example.com"}


def test_scenario_with_empty_vars():
    """Test that vars defaults to None when not specified."""
    scenario = Scenario(
        stages=[
            Stage(
                name="get_users",
                request=Request(url="https://api.example.com/users"),
            )
        ],
    )
    assert scenario.vars is None


def test_scenario_without_vars_field():
    """Test that scenarios work fine without any vars field."""
    # This should work fine with fixtures and saved vars, but no initial vars
    scenario = Scenario(
        fixtures=["auth_token"],
        stages=[
            Stage(
                name="get_user",
                request=Request(
                    url="https://api.example.com/users/123",
                    headers={"Authorization": "Bearer {{ auth_token }}"},
                ),
                response=Response(save=Save(vars={"user_name": "data.name"})),
            ),
            Stage(
                name="update_user",
                request=Request(
                    url="https://api.example.com/users/123",
                    json={"name": "{{ user_name }}_updated"},
                ),
            ),
        ],
    )
    assert scenario.vars is None
    assert scenario.fixtures == ["auth_token"]
    assert len(scenario.stages) == 2


def test_fixture_shadows_initial_var_raises_error():
    """Test that fixtures shadowing initial vars raises an error."""
    with pytest.raises(ValueError, match="Variable name 'api_key' conflicts with fixture name"):
        Scenario(
            fixtures=["api_key", "other_fixture"],
            vars={"api_key": "test-key", "base_url": "https://api.example.com"},
            stages=[
                Stage(
                    name="get_users",
                    request=Request(url="https://api.example.com/users"),
                )
            ],
        )


def test_saved_var_can_overwrite_initial_var():
    """Test that saved variables can overwrite initial vars (this is now allowed)."""
    scenario = Scenario(
        vars={"api_key": "initial-key"},
        stages=[
            Stage(
                name="get_users",
                request=Request(url="https://api.example.com/users"),
                response=Response(
                    save=Save(
                        vars={"api_key": "data.new_key"}  # Overwriting initial var is now allowed
                    )
                ),
            )
        ],
    )
    # Should not raise an error
    assert scenario.vars["api_key"] == "initial-key"
    assert scenario.stages[0].response.save.vars["api_key"] == "data.new_key"


def test_saved_var_conflicts_with_fixture_raises_error():
    """Test that saved variables conflicting with fixtures still raises an error."""
    with pytest.raises(ValueError, match="Variable name 'api_key' conflicts with fixture name"):
        Scenario(
            fixtures=["api_key"],
            stages=[
                Stage(
                    name="get_users",
                    request=Request(url="https://api.example.com/users"),
                    response=Response(
                        save=Save(
                            vars={"api_key": "data.new_key"}  # Trying to save over fixture
                        )
                    ),
                )
            ],
        )


def test_no_conflicts_allows_scenario_creation():
    """Test that scenario with no conflicts between fixtures, vars, and saved vars works."""
    scenario = Scenario(
        fixtures=["auth_token"],
        vars={"base_url": "https://api.example.com"},
        stages=[
            Stage(
                name="create_user",
                request=Request(
                    url="{{ base_url }}/users",
                    headers={"Authorization": "Bearer {{ auth_token }}"},
                ),
                response=Response(save=Save(vars={"user_id": "data.id", "user_name": "data.name"})),
            ),
            Stage(
                name="get_user",
                request=Request(
                    url="{{ base_url }}/users/{{ user_id }}",
                ),
            ),
        ],
    )
    assert scenario.fixtures == ["auth_token"]
    assert scenario.vars == {"base_url": "https://api.example.com"}
    assert len(scenario.stages) == 2
