"""Test the combined validate_variable_naming_conflicts validator."""

import pytest
from pytest_http_engine.models import Request, Response, Save, Scenario, Stage


def test_combined_validator_checks_fixture_shadows_var():
    """Test that the combined validator catches fixtures shadowing vars."""
    with pytest.raises(ValueError, match="Variable name 'api_key' conflicts with fixture name"):
        Scenario(
            fixtures=["api_key"],
            vars={"api_key": "test-key"},
            flow=[Stage(name="test", request=Request(url="https://api.example.com"))],
        )


def test_combined_validator_checks_saved_var_conflicts_with_fixture():
    """Test that the combined validator catches saved vars conflicting with fixtures."""
    with pytest.raises(ValueError, match="Variable name 'api_key' conflicts with fixture name"):
        Scenario(
            fixtures=["api_key"],
            flow=[
                Stage(
                    name="test",
                    request=Request(url="https://api.example.com"),
                    response=Response(save=Save(vars={"api_key": "data.key"})),
                )
            ],
        )


def test_combined_validator_allows_saved_var_to_overwrite_initial_var():
    """Test that the combined validator allows saved vars to overwrite initial vars."""
    scenario = Scenario(
        vars={"base_url": "https://api.example.com"},
        flow=[
            Stage(
                name="test",
                request=Request(url="{{ base_url }}"),
                response=Response(save=Save(vars={"base_url": "data.url"})),
            )
        ],
    )
    # Should not raise an error
    assert scenario.vars["base_url"] == "https://api.example.com"
    assert scenario.flow.root[0].response.save.vars["base_url"] == "data.url"


def test_combined_validator_allows_non_conflicting_names():
    """Test that the combined validator allows scenarios without naming conflicts."""
    scenario = Scenario(
        fixtures=["auth_token"],
        vars={"base_url": "https://api.example.com"},
        flow=[
            Stage(
                name="test",
                request=Request(url="{{ base_url }}", headers={"Authorization": "{{ auth_token }}"}),
                response=Response(save=Save(vars={"user_id": "data.id"})),
            )
        ],
    )
    assert "auth_token" in scenario.fixtures
    assert "base_url" in scenario.vars
    assert scenario.flow.root[0].response.save.vars["user_id"] == "data.id"
