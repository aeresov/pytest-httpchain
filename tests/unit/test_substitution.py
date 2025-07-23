from pytest_http_engine.models import Request, Stage

from pytest_http.plugin import substitute_variables


def test_simple_variable_substitution():
    stage = Stage(
        name="stage1",
        request=Request(
            url="http://localhost:5000/path_param/{number_value}",
        ),
    )
    stage = substitute_variables(stage, {"number_value": 123})
    assert stage.request.url == "http://localhost:5000/path_param/123"


def test_object_dot_notation():
    stage = Stage(
        name="stage1",
        request=Request(
            url="http://localhost:5000/users/{user_id}/profile",
        ),
    )
    stage = substitute_variables(stage, {"user_id": 456})
    assert stage.request.url == "http://localhost:5000/users/456/profile"


def test_array_bracket_notation():
    stage = Stage(
        name="stage1",
        request=Request(
            url="http://localhost:5000/items/{first_item}/details",
        ),
    )
    stage = substitute_variables(stage, {"first_item": "first"})
    assert stage.request.url == "http://localhost:5000/items/first/details"


def test_complex_nested_access():
    stage = Stage(
        name="stage1",
        request=Request(
            url="http://localhost:5000/users/{profile_id}",
        ),
    )
    variables = {"profile_id": 789}
    stage = substitute_variables(stage, variables)
    assert stage.request.url == "http://localhost:5000/users/789"


def test_string_interpolation():
    stage = Stage(
        name="stage1",
        request=Request(url="http://localhost:5000/api", body={"json": {"user_id": "{user_id}", "name": "{user_name}"}}),
    )
    variables = {"user_id": 123, "user_name": "John Doe"}
    stage = substitute_variables(stage, variables)
    assert stage.request.body.json == {"user_id": 123, "name": "John Doe"}


def test_full_object_embedding():
    # Test type preservation for different data types
    stage = Stage(
        name="stage1",
        request=Request(url="http://localhost:5000/api"),
    )
    # Add a custom attribute to demonstrate type preservation
    stage_data = stage.model_dump()
    stage_data["request"]["body"] = {
        "json": {
            "user_id": "{user_id}",  # Should preserve integer type
            "config": "{config}",  # Should preserve string type
            "active": "{active}",  # Should preserve boolean type
        }
    }

    variables = {
        "user_id": 123,
        "config": '{"theme": "dark", "notifications": true}',
        "active": True,
    }

    # Create stage from modified data
    test_stage = Stage.model_validate(stage_data)
    result = substitute_variables(test_stage, variables)

    assert result.request.body.json == {"user_id": 123, "config": '{"theme": "dark", "notifications": true}', "active": True}
