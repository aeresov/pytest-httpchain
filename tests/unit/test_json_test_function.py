import responses
from pytest_http_engine.models import Request, Scenario, Stage, Stages

from pytest_http.plugin import json_test_function


@responses.activate
def test_url_substitution():
    responses.add(
        responses.GET,
        "http://localhost:5000/path_param/123",
        status=200,
    )

    scenario = Scenario(
        fixtures=["number_value"],
        flow=Stages(
            [
                Stage(
                    name="stage1",
                    request=Request(
                        url="http://localhost:5000/path_param/{{ number_value }}",
                    ),
                )
            ]
        ),
    )
    json_test_function(scenario.flow, Stages(), number_value=123)
