"""Unit tests for Stage and Scenario models."""

from http import HTTPMethod

import pytest
from pydantic import ValidationError
from pytest_httpchain_models.entities import (
    JsonBody,
    Request,
    Scenario,
    SSLConfig,
    Stage,
    UserFunctionKwargs,
    UserFunctionName,
    VarsSubstitution,
)


class TestStageName:
    """Tests for Stage.name field."""

    def test_stage_name_default_empty(self):
        """Test that stage name defaults to empty string when not provided."""
        stage = Stage(request=Request(url="https://example.com"))
        assert stage.name == ""

    def test_stage_name_simple(self):
        """Test simple stage name."""
        stage = Stage(name="get-users", request=Request(url="https://example.com"))
        assert stage.name == "get-users"

    def test_stage_name_descriptive(self):
        """Test descriptive stage name."""
        stage = Stage(
            name="Create new user account",
            request=Request(url="https://example.com"),
        )
        assert stage.name == "Create new user account"


class TestStageDescription:
    """Tests for Stage.description field."""

    def test_stage_description_default_none(self):
        """Test default description is None."""
        stage = Stage(name="test", request=Request(url="https://example.com"))
        assert stage.description is None

    def test_stage_description_custom(self):
        """Test custom description."""
        stage = Stage(
            name="test",
            request=Request(url="https://example.com"),
            description="This stage tests the user creation endpoint",
        )
        assert stage.description == "This stage tests the user creation endpoint"


class TestStageMarks:
    """Tests for Stage.marks field."""

    def test_stage_marks_default_empty(self):
        """Test default marks is empty list."""
        stage = Stage(name="test", request=Request(url="https://example.com"))
        assert stage.marks == []

    def test_stage_marks_skip(self):
        """Test stage with skip marker."""
        stage = Stage(
            name="test",
            request=Request(url="https://example.com"),
            marks=["skip"],
        )
        assert "skip" in stage.marks

    def test_stage_marks_xfail(self):
        """Test stage with xfail marker."""
        stage = Stage(
            name="test",
            request=Request(url="https://example.com"),
            marks=["xfail"],
        )
        assert "xfail" in stage.marks

    def test_stage_marks_multiple(self):
        """Test stage with multiple markers."""
        stage = Stage(
            name="test",
            request=Request(url="https://example.com"),
            marks=["slow", "integration", "requires_auth"],
        )
        assert len(stage.marks) == 3


class TestStageFixtures:
    """Tests for Stage.fixtures field."""

    def test_stage_fixtures_default_empty(self):
        """Test default fixtures is empty list."""
        stage = Stage(name="test", request=Request(url="https://example.com"))
        assert stage.fixtures == []

    def test_stage_fixtures_single(self):
        """Test stage with single fixture."""
        stage = Stage(
            name="test",
            request=Request(url="https://example.com"),
            fixtures=["auth_token"],
        )
        assert "auth_token" in stage.fixtures

    def test_stage_fixtures_multiple(self):
        """Test stage with multiple fixtures."""
        stage = Stage(
            name="test",
            request=Request(url="https://example.com"),
            fixtures=["db_connection", "auth_token", "test_user"],
        )
        assert len(stage.fixtures) == 3


class TestStageAlwaysRun:
    """Tests for Stage.always_run field."""

    def test_stage_always_run_default_false(self):
        """Test default always_run is False."""
        stage = Stage(name="test", request=Request(url="https://example.com"))
        assert stage.always_run is False

    def test_stage_always_run_true(self):
        """Test always_run set to True."""
        stage = Stage(
            name="cleanup",
            request=Request(url="https://example.com/cleanup"),
            always_run=True,
        )
        assert stage.always_run is True

    def test_stage_always_run_template(self):
        """Test always_run with template expression."""
        stage = Stage(
            name="conditional",
            request=Request(url="https://example.com"),
            always_run="{{ should_always_run }}",
        )
        assert stage.always_run == "{{ should_always_run }}"

    def test_stage_always_run_conditional_template(self):
        """Test always_run with conditional template."""
        stage = Stage(
            name="test",
            request=Request(url="https://example.com"),
            always_run="{{ env == 'production' }}",
        )
        assert stage.always_run == "{{ env == 'production' }}"


class TestScenarioDescription:
    """Tests for Scenario.description field."""

    def test_scenario_description_default_none(self):
        """Test default description is None."""
        scenario = Scenario()
        assert scenario.description is None

    def test_scenario_description_custom(self):
        """Test custom description."""
        scenario = Scenario(
            description="Test user authentication flow",
        )
        assert scenario.description == "Test user authentication flow"


class TestScenarioMarks:
    """Tests for Scenario.marks field."""

    def test_scenario_marks_default_empty(self):
        """Test default marks is empty list."""
        scenario = Scenario()
        assert scenario.marks == []

    def test_scenario_marks_multiple(self):
        """Test scenario with multiple markers."""
        scenario = Scenario(marks=["smoke", "critical"])
        assert "smoke" in scenario.marks
        assert "critical" in scenario.marks


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
        scenario = Scenario(stages=[Stage(name="get-users", request=Request(url="https://example.com/users"))])
        assert len(scenario.stages) == 1
        assert isinstance(scenario.stages[0], Stage)

    def test_scenario_multiple_stages(self):
        """Test scenario with multiple stages."""
        scenario = Scenario(
            stages=[
                Stage(name="login", request=Request(url="https://example.com/login")),
                Stage(name="get-profile", request=Request(url="https://example.com/profile")),
                Stage(name="logout", request=Request(url="https://example.com/logout")),
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
            Stage(name="test")  # type: ignore

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
