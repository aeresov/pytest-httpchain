"""Unit tests for Stage and Scenario models."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from pytest_httpchain.models.entities import (
    FunctionsSubstitution,
    Scenario,
    SSLConfig,
    Stage,
    UserFunctionName,
    VarsSubstitution,
)
from tests.unit.models.helpers import assert_error_types, make_request, make_stage


class TestStageFields:
    """Per-field defaults and round-trips for Stage's simple fields."""

    @pytest.mark.parametrize(
        ("attr", "default"),
        [
            ("description", None),
            ("marks", []),
            ("fixtures", []),
            ("always_run", False),
            ("substitutions", []),
            ("parametrize", None),
            ("parallel", None),
            ("response", []),
        ],
    )
    def test_field_default(self, attr, default):
        assert getattr(make_stage(), attr) == default

    def test_name_defaults_to_empty_string(self):
        # name is the one field make_stage() fills in, so build the stage directly.
        assert Stage(request=make_request()).name == ""

    def test_request_is_required(self):
        with pytest.raises(ValidationError) as exc_info:
            Stage(name="test")
        assert_error_types(exc_info, "missing", at="request")

    @pytest.mark.parametrize(
        ("field", "value"),
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
            # An empty list stays distinct from the None default.
            pytest.param("parametrize", [], id="parametrize-empty"),
        ],
    )
    def test_field_roundtrip(self, field, value):
        assert getattr(make_stage(**{field: value}), field) == value


class TestScenarioSimpleFields:
    """Per-field defaults and round-trips for Scenario's simple fields.

    (stages / substitutions have their own classes below because they coerce
    their inputs rather than storing them verbatim; auth is the same
    Authenticated field Request has, covered in test_request.py.)"""

    @pytest.mark.parametrize(
        ("attr", "default"),
        [
            ("description", None),
            ("marks", []),
            ("fixtures", []),
            ("auth", None),
            ("ssl", SSLConfig()),
            ("stages", []),
            ("substitutions", []),
        ],
    )
    def test_field_default(self, attr, default):
        assert getattr(Scenario(), attr) == default

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            pytest.param("description", "Test user authentication flow", id="description"),
            pytest.param("marks", ["smoke", "critical"], id="marks-multiple"),
            pytest.param("ssl", SSLConfig(verify=False, cert=Path("/path/to/cert.pem")), id="ssl"),
        ],
    )
    def test_field_roundtrip(self, field, value):
        assert getattr(Scenario(**{field: value}), field) == value


class TestScenarioStages:
    def test_list_form_keeps_order(self):
        scenario = Scenario(stages=[make_stage(name="login"), make_stage(name="get-profile"), make_stage(name="logout")])
        assert [s.name for s in scenario.stages] == ["login", "get-profile", "logout"]

    def test_mapping_form_keys_become_names_in_order(self):
        scenario = Scenario.model_validate(
            {
                "stages": {
                    "login": {"request": {"url": "https://example.com/login"}},
                    "get-profile": {"request": {"url": "https://example.com/profile"}},
                }
            }
        )
        assert [s.name for s in scenario.stages] == ["login", "get-profile"]

    def test_mapping_form_preserves_other_fields(self):
        scenario = Scenario.model_validate(
            {
                "stages": {
                    "create-user": {
                        "description": "Create a new user",
                        "marks": ["critical"],
                        "fixtures": ["db_session"],
                        "always_run": True,
                        "request": {"url": "https://example.com/users", "method": "POST"},
                    }
                }
            }
        )
        assert scenario.stages[0].model_dump(include={"name", "description", "marks", "fixtures", "always_run"}) == {
            "name": "create-user",
            "description": "Create a new user",
            "marks": ["critical"],
            "fixtures": ["db_session"],
            "always_run": True,
        }

    @pytest.mark.parametrize(
        "stage",
        [
            pytest.param({"name": "explicit-name", "request": {"url": "https://example.com"}}, id="dict"),
            # A Stage instance (Python API only) used to keep its own name.
            pytest.param(make_stage(name="explicit-name"), id="instance"),
        ],
    )
    def test_mapping_key_overrides_explicit_name(self, stage):
        assert Scenario.model_validate({"stages": {"key-name": stage}}).stages[0].name == "key-name"

    @pytest.mark.parametrize(
        ("stages", "error_type"),
        [
            pytest.param("oops", "list_type", id="not-list-or-mapping"),
            pytest.param({"login": "not-a-stage"}, "model_type", id="non-dict-mapping-value"),
        ],
    )
    def test_malformed_stages_rejected(self, stages, error_type):
        """Normalization passes shapes it cannot flatten through, so the
        ordinary list/model validation reports them."""
        with pytest.raises(ValidationError) as exc_info:
            Scenario.model_validate({"stages": stages})
        assert_error_types(exc_info, error_type, at="stages")


def test_scenario_substitutions_mapping_form():
    """Same ``Substitutions`` type as Stage's (see test_substitutions_responses.py)."""
    scenario = Scenario.model_validate(
        {
            "substitutions": {
                "config": {"vars": {"base_url": "https://example.com"}},
                "auth": {"functions": {"token": "auth:get_token"}},
            }
        }
    )
    assert scenario.substitutions == [
        VarsSubstitution(vars={"base_url": "https://example.com"}),
        FunctionsSubstitution(functions={"token": UserFunctionName("auth:get_token")}),
    ]
