from pytest_http.models import Request, Stage
from pytest_http.pytest_plugin import substitute_variables


def test_url_substitution():
    stage = Stage(
        name="stage1",
        request=Request(
            url="http://localhost:5000/path_param/{number_value}",
        ),
    )
    stage = substitute_variables(stage, {"number_value": 123})
    assert stage.request.url == "http://localhost:5000/path_param/123"
