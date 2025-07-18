from pytest_http_engine.models import Request, Stage

from pytest_http.plugin import substitute_variables


def test_simple_variable_substitution():
    stage = Stage(
        name="stage1",
        request=Request(
            url="http://localhost:5000/path_param/{{ number_value }}",
        ),
    )
    stage = substitute_variables(stage, {"number_value": 123})
    assert stage.request.url == "http://localhost:5000/path_param/123"


def test_object_dot_notation():
    stage = Stage(
        name="stage1",
        request=Request(
            url="http://localhost:5000/users/{{ user.id }}/profile",
        ),
    )
    stage = substitute_variables(stage, {"user": {"id": 456, "name": "John"}})
    assert stage.request.url == "http://localhost:5000/users/456/profile"


def test_array_bracket_notation():
    stage = Stage(
        name="stage1",
        request=Request(
            url="http://localhost:5000/items/{{ items[0] }}/details",
        ),
    )
    stage = substitute_variables(stage, {"items": ["first", "second", "third"]})
    assert stage.request.url == "http://localhost:5000/items/first/details"


def test_complex_nested_access():
    stage = Stage(
        name="stage1",
        request=Request(
            url="http://localhost:5000/users/{{ data.users[0].profile.id }}",
        ),
    )
    variables = {"data": {"users": [{"profile": {"id": 789, "active": True}}, {"profile": {"id": 790, "active": False}}]}}
    stage = substitute_variables(stage, variables)
    assert stage.request.url == "http://localhost:5000/users/789"


def test_string_interpolation():
    stage = Stage(
        name="stage1",
        request=Request(url="http://localhost:5000/api", json={"user_id": "{{ user.id }}", "name": "{{ user.name }}"}),
    )
    variables = {"user": {"id": 123, "name": "John Doe"}}
    stage = substitute_variables(stage, variables)
    assert stage.request.json == {"user_id": "123", "name": "John Doe"}


def test_full_object_embedding():
    # For full JSON objects, we need to create a custom template that represents the entire value
    stage = Stage(
        name="stage1",
        request=Request(url="http://localhost:5000/api"),
    )
    # Add a custom attribute to demonstrate object embedding
    stage_data = stage.model_dump()
    stage_data["request"]["json"] = {
        "user_id": "{{ user.id }}",
        "config": "{{ config }}",  # This will be rendered as a string
    }

    variables = {
        "user": {"id": 123},
        "config": '{"theme": "dark", "notifications": true}',  # JSON as string
    }

    # Create stage from modified data
    test_stage = Stage.model_validate(stage_data)
    result = substitute_variables(test_stage, variables)

    assert result.request.json == {"user_id": "123", "config": '{"theme": "dark", "notifications": true}'}
