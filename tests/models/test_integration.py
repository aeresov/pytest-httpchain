from pytest_http.models import Scenario, Structure


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
