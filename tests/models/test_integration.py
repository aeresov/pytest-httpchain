from pytest_http.models import Scenario, Structure, TestDefinition


def test_complete_test_structure():
    data = {
        "fixtures": ["user_data", "config"],
        "marks": ["slow", "integration"],
        "stages": [
            {"name": "setup_user", "data": {"username": "testuser", "email": "test@example.com"}},
            {"name": "perform_action", "data": ["action1", "action2"]},
            {"name": "verify_result", "data": 42},
        ],
    }

    structure = Structure.model_validate(data)
    scenario = Scenario.model_validate(data)

    assert structure.fixtures == ["user_data", "config"]
    assert structure.marks == ["slow", "integration"]
    
    assert len(scenario.stages) == 3
    assert scenario.stages[0].name == "setup_user"
    assert scenario.stages[2].data == 42


def test_test_definition_save_variables_not_conflicting_with_fixtures():
    data = {
        "fixtures": ["user_data", "config"],
        "stages": [
            {"name": "test", "data": "data", "save": {"result": "user.id", "status": "response.status"}},
        ],
    }
    
    test_def = TestDefinition.model_validate(data)
    assert test_def.fixtures == ["user_data", "config"]
    assert len(test_def.stages) == 1
    assert test_def.stages[0].save["result"] == "user.id"
    assert test_def.stages[0].save["status"] == "response.status"


def test_test_definition_save_variables_conflicting_with_fixtures():
    from pydantic import ValidationError
    import pytest
    
    data = {
        "fixtures": ["user_data", "config"],
        "stages": [
            {"name": "test", "data": "data", "save": {"user_data": "user.id"}},
        ],
    }
    
    with pytest.raises(ValidationError) as exc_info:
        TestDefinition.model_validate(data)
    assert "Variable name 'user_data' conflicts with fixture name" in str(exc_info.value)


def test_test_definition_multiple_stages_with_fixture_conflicts():
    from pydantic import ValidationError
    import pytest
    
    data = {
        "fixtures": ["user_data", "config"],
        "stages": [
            {"name": "test1", "data": "data1", "save": {"result": "user.id"}},
            {"name": "test2", "data": "data2", "save": {"config": "app.config"}},
        ],
    }
    
    with pytest.raises(ValidationError) as exc_info:
        TestDefinition.model_validate(data)
    assert "Variable name 'config' conflicts with fixture name" in str(exc_info.value)


def test_test_definition_no_fixtures_no_validation():
    data = {
        "stages": [
            {"name": "test", "data": "data", "save": {"anything": "user.id"}},
        ],
    }
    
    test_def = TestDefinition.model_validate(data)
    assert test_def.fixtures == []
    assert len(test_def.stages) == 1
    assert test_def.stages[0].save["anything"] == "user.id"


def test_test_definition_stages_without_save_field():
    data = {
        "fixtures": ["user_data", "config"],
        "stages": [
            {"name": "test", "data": "data"},
        ],
    }
    
    test_def = TestDefinition.model_validate(data)
    assert test_def.fixtures == ["user_data", "config"]
    assert len(test_def.stages) == 1
    assert test_def.stages[0].save is None
