"""Unit tests for Stage and Scenario models."""

from http import HTTPMethod

import pytest
from pydantic import ValidationError

from pytest_httpchain.models.entities import (
    JsonBody,
    Request,
    Scenario,
    SSLConfig,
    Stage,
    UserFunctionKwargs,
    UserFunctionName,
    VarsSubstitution,
)
from tests.unit.models.conftest import make_request, make_stage


class TestStageFields:
    """Per-field defaults and round-trips for Stage's simple fields."""

    @pytest.mark.parametrize(
        "attr, default",
        [
            ("description", None),
            ("marks", []),
            ("fixtures", []),
            ("always_run", False),
        ],
    )
    def test_field_default(self, attr, default):
        assert getattr(make_stage(), attr) == default

    def test_name_defaults_to_empty_string(self):
        # name is the one field make_stage() fills in, so build the stage directly.
        assert Stage(request=make_request()).name == ""

    @pytest.mark.parametrize(
        "field, value",
        [
            pytest.param("name", "get-users", id="name-simple"),
            pytest.param("name", "Create new user account", id="name-descriptive"),
            pytest.param("description", "This stage tests the user creation endpoint", id="description"),
            pytest.param("marks", ["skip"], id="marks-single"),
            pytest.param("marks", ["xfail"], id="marks-xfail"),
            pytest.param("marks", ["slow", "integration", "requires_auth"], id="marks-multiple"),
            pytest.param("fixtures", ["auth_token"], id="fixtures-single"),
            pytest.param("fixtures", ["db_connection", "auth_token", "test_user"], id="fixtures-multiple"),
            pytest.param("always_run", True, id="always_run-true"),
            pytest.param("always_run", "{{ should_always_run }}", id="always_run-template"),
            pytest.param("always_run", "{{ env == 'production' }}", id="always_run-conditional-template"),
        ],
    )
    def test_field_roundtrip(self, field, value):
        assert getattr(make_stage(**{field: value}), field) == value


class TestScenarioSimpleFields:
    """Per-field defaults and round-trips for Scenario's simple fields.

    (auth / stages / substitutions have their own classes below because they
    coerce and discriminate their inputs rather than storing them verbatim.)"""

    @pytest.mark.parametrize(
        "attr, default",
        [
            ("description", None),
            ("marks", []),
        ],
    )
    def test_field_default(self, attr, default):
        assert getattr(Scenario(), attr) == default

    @pytest.mark.parametrize(
        "field, value",
        [
            pytest.param("description", "Test user authentication flow", id="description"),
            pytest.param("marks", ["smoke", "critical"], id="marks-multiple"),
        ],
    )
    def test_field_roundtrip(self, field, value):
        assert getattr(Scenario(**{field: value}), field) == value


class TestScenarioAuth:
    """Tests for Scenario.auth field."""

    def test_scenario_auth_default_none(self):
        """Test default auth is None."""
        scenario = Scenario()
        assert scenario.auth is None

    def test_scenario_auth_simple_function(self):
        """Test scenario with simple auth function."""
        scenario = Scenario(auth=UserFunctionName("auth:get_api_key"))
        assert isinstance(scenario.auth, UserFunctionName)

    def test_scenario_auth_with_kwargs(self):
        """Test scenario with auth function kwargs."""
        scenario = Scenario(
            auth=UserFunctionKwargs(
                name=UserFunctionName("auth:oauth2"),
                kwargs={"client_id": "abc", "client_secret": "xyz"},
            )
        )
        assert isinstance(scenario.auth, UserFunctionKwargs)


class TestScenarioStages:
    """Tests for Scenario.stages field."""

    def test_scenario_stages_default_empty(self):
        """Test default stages is empty list."""
        scenario = Scenario()
        assert scenario.stages == []

    def test_scenario_single_stage(self):
        """Test scenario with single stage."""
        scenario = Scenario(stages=[make_stage(name="get-users")])
        assert len(scenario.stages) == 1
        assert isinstance(scenario.stages[0], Stage)

    def test_scenario_multiple_stages(self):
        """Test scenario with multiple stages."""
        scenario = Scenario(
            stages=[
                make_stage(name="login"),
                make_stage(name="get-profile"),
                make_stage(name="logout"),
            ]
        )
        assert len(scenario.stages) == 3

    def test_scenario_stages_dict_format(self):
        """Test scenario with dict format stages where key becomes name."""
        scenario = Scenario.model_validate(
            {
                "stages": {
                    "login": {"request": {"url": "https://example.com/login"}},
                    "get-profile": {"request": {"url": "https://example.com/profile"}},
                }
            }
        )
        assert len(scenario.stages) == 2
        stage_names = [s.name for s in scenario.stages]
        assert "login" in stage_names
        assert "get-profile" in stage_names

    def test_scenario_stages_dict_format_preserves_other_fields(self):
        """Test that dict format preserves all stage fields."""
        scenario = Scenario.model_validate(
            {
                "stages": {
                    "create-user": {
                        "description": "Create a new user",
                        "marks": ["critical"],
                        "fixtures": ["db_session"],
                        "always_run": True,
                        "request": {"url": "https://example.com/users", "method": "POST"},
                        "response": [{"verify": {"status": 201}}],
                    }
                }
            }
        )
        stage = scenario.stages[0]
        assert stage.name == "create-user"
        assert stage.description == "Create a new user"
        assert stage.marks == ["critical"]
        assert stage.fixtures == ["db_session"]
        assert stage.always_run is True

    def test_scenario_stages_dict_format_overrides_explicit_name(self):
        """Test that dict key takes precedence over explicit name in value."""
        scenario = Scenario.model_validate(
            {
                "stages": {
                    "dict-key-name": {
                        "name": "explicit-name",
                        "request": {"url": "https://example.com"},
                    }
                }
            }
        )
        # Dict key should override explicit name
        assert scenario.stages[0].name == "dict-key-name"


class TestScenarioSubstitutions:
    """Tests for Scenario.substitutions field."""

    def test_scenario_substitutions_default_empty(self):
        """Test default substitutions is empty list."""
        scenario = Scenario()
        assert scenario.substitutions == []

    def test_scenario_substitutions_vars(self):
        """Test scenario with vars substitution."""
        scenario = Scenario(substitutions=[VarsSubstitution(vars={"base_url": "https://api.example.com", "api_version": "v1"})])
        assert len(scenario.substitutions) == 1
        assert isinstance(scenario.substitutions[0], VarsSubstitution)

    def test_scenario_substitutions_dict_format(self):
        """Test scenario with dict format substitutions."""
        scenario = Scenario.model_validate(
            {
                "substitutions": {
                    "config": {"vars": {"base_url": "https://example.com"}},
                    "auth": {"functions": {"token": "auth:get_token"}},
                }
            }
        )
        assert len(scenario.substitutions) == 2


class TestScenarioComplete:
    """Tests for complete Scenario configurations."""

    def test_scenario_full_config(self):
        """Test Scenario with full configuration."""
        scenario = Scenario(
            description="Complete user workflow test",
            marks=["integration", "slow"],
            auth=UserFunctionName("auth:api_key"),
            ssl=SSLConfig(verify=True),
            substitutions=[VarsSubstitution(vars={"base_url": "https://api.example.com"})],
            stages=[
                Stage(
                    name="create-user",
                    description="Create a new user",
                    marks=["critical"],
                    fixtures=["db_session"],
                    request=Request(
                        url="{{ base_url }}/users",
                        method=HTTPMethod.POST,
                        body=JsonBody(json={"name": "Test User"}),
                    ),
                ),
                Stage(
                    name="delete-user",
                    always_run=True,
                    request=Request(
                        url="{{ base_url }}/users/{{ user_id }}",
                        method=HTTPMethod.DELETE,
                    ),
                ),
            ],
        )
        assert scenario.description == "Complete user workflow test"
        assert len(scenario.marks) == 2
        assert len(scenario.stages) == 2
        assert scenario.stages[1].always_run is True


class TestStageRequest:
    """Tests for Stage.request field."""

    def test_stage_request_required(self):
        """Test that request is required."""
        with pytest.raises(ValidationError):
            Stage(name="test")

    def test_stage_request_minimal(self):
        """Test stage with minimal request."""
        stage = Stage(
            name="test",
            request=Request(url="https://example.com"),
        )
        assert str(stage.request.url) == "https://example.com/"

    def test_stage_request_full(self):
        """Test stage with full request configuration."""
        stage = Stage(
            name="test",
            request=Request(
                url="https://example.com/api",
                method=HTTPMethod.POST,
                headers={"Content-Type": "application/json"},
                body=JsonBody(json={"key": "value"}),
            ),
        )
        assert stage.request.method == HTTPMethod.POST
