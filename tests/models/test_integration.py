from pytest_http.models import Scenario


def test_complete_test_structure():
    test_data = {
        "fixtures": ["user_id", "api_key"],
        "marks": ["slow", "integration"],
        "stages": [
            {"name": "login", "data": {"username": "test", "password": "secret"}, "save": {"token": "response.token", "profile_id": "response.user.id"}},
            {"name": "get_profile", "data": {"user_id": "$user_id"}, "save": {"profile": "response.profile"}},
        ],
    }

    test_spec = Scenario.model_validate(test_data)
    assert test_spec.fixtures == ["user_id", "api_key"]
    assert test_spec.marks == ["slow", "integration"]
    assert len(test_spec.stages) == 2
    assert test_spec.stages[0].name == "login"
    assert test_spec.stages[0].save == {"token": "response.token", "profile_id": "response.user.id"}
    assert test_spec.stages[1].name == "get_profile"
    assert test_spec.stages[1].save == {"profile": "response.profile"}


def test_test_definition_save_variables_not_conflicting_with_fixtures():
    test_data = {
        "fixtures": ["user_id", "api_key"],
        "stages": [{"name": "test_stage", "data": {"test": "data"}, "save": {"token": "response.token", "profile": "response.profile"}}],
    }

    test_spec = Scenario.model_validate(test_data)
    assert test_spec.fixtures == ["user_id", "api_key"]
    assert len(test_spec.stages) == 1
    assert test_spec.stages[0].save == {"token": "response.token", "profile": "response.profile"}


def test_test_definition_save_variables_conflicting_with_fixtures():
    test_data = {
        "fixtures": ["user_id", "api_key"],
        "stages": [{"name": "test_stage", "data": {"test": "data"}, "save": {"user_id": "response.user.id", "token": "response.token"}}],
    }

    try:
        Scenario.model_validate(test_data)
        raise AssertionError("Expected ValidationError")
    except Exception as e:
        assert "Variable name 'user_id' conflicts with fixture name" in str(e)


def test_test_definition_multiple_stages_with_fixture_conflicts():
    test_data = {
        "fixtures": ["user_id", "api_key"],
        "stages": [
            {"name": "stage1", "data": {"test": "data"}, "save": {"token": "response.token"}},
            {"name": "stage2", "data": {"test": "data"}, "save": {"api_key": "response.key", "profile": "response.profile"}},
        ],
    }

    try:
        Scenario.model_validate(test_data)
        raise AssertionError("Expected ValidationError")
    except Exception as e:
        assert "Variable name 'api_key' conflicts with fixture name" in str(e)


def test_test_definition_no_fixtures_no_validation():
    test_data = {"stages": [{"name": "test_stage", "data": {"test": "data"}, "save": {"user_id": "response.user.id", "token": "response.token"}}]}

    test_spec = Scenario.model_validate(test_data)
    assert test_spec.fixtures == []
    assert len(test_spec.stages) == 1
    assert test_spec.stages[0].save == {"user_id": "response.user.id", "token": "response.token"}


def test_test_definition_stages_without_save_field():
    test_data = {"fixtures": ["user_id", "api_key"], "stages": [{"name": "test_stage", "data": {"test": "data"}}]}

    test_spec = Scenario.model_validate(test_data)
    assert test_spec.fixtures == ["user_id", "api_key"]
    assert len(test_spec.stages) == 1
    assert test_spec.stages[0].save is None
