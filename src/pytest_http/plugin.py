import json
import re
import warnings
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path
from typing import Any

import jinja2
import jmespath
import jsonref
import pytest
import requests
from _pytest.config import Config
from _pytest.config.argparsing import Parser
from _pytest.nodes import Collector, Item
from _pytest.python import Function
from pydantic import ValidationError
from pytest_http_engine.models import AWSCredentials, AWSProfile, Scenario, Stage, Stages
from pytest_http_engine.user_function import UserFunction
from requests.auth import AuthBase

SUFFIX: str = "suffix"


def pytest_addoption(parser: Parser) -> None:
    parser.addini(
        name=SUFFIX,
        help="File suffix for HTTP test files (default: http). Must contain only alphanumeric characters, underscores, and hyphens, and be 32 characters or less.",
        type="string",
        default="http",
    )


def pytest_configure(config: Config) -> None:
    suffix: str = config.getini(SUFFIX)
    if not re.match(r"^[a-zA-Z0-9_-]{1,32}$", suffix):
        raise ValueError("suffix must contain only alphanumeric characters, underscores, hyphens, and be ≤32 chars")


def get_test_name_pattern(config: Config) -> tuple[re.Pattern[str], str]:
    suffix: str = config.getini(SUFFIX)
    group_name: str = "name"
    return re.compile(rf"^test_(?P<{group_name}>.+)\.{re.escape(suffix)}\.json$"), group_name


class VariableSubstitutionError(Exception):
    pass


def create_aws_auth(aws_config: AWSProfile | AWSCredentials) -> AuthBase:
    try:
        import boto3
        from requests_auth_aws_sigv4 import AWSSigV4  # type: ignore
    except ImportError as e:
        raise ImportError("AWS support requires 'aws' optional dependency") from e

    if isinstance(aws_config, AWSProfile):
        # Profile-based authentication
        session = boto3.Session(profile_name=aws_config.profile)
        credentials = session.get_credentials()
        if not credentials:
            raise ValueError(f"Could not get credentials for AWS profile '{aws_config.profile}'")

        return AWSSigV4(
            service=aws_config.service,
            region=aws_config.region,
            aws_access_key_id=credentials.access_key,
            aws_secret_access_key=credentials.secret_key,
            aws_session_token=credentials.token,
        )
    else:
        # Credential-based authentication
        return AWSSigV4(
            service=aws_config.service,
            region=aws_config.region,
            aws_access_key_id=aws_config.access_key_id,
            aws_secret_access_key=aws_config.secret_access_key,
            aws_session_token=aws_config.session_token,
        )


def substitute_variables(stage: Stage, variables: dict[str, Any]) -> Stage:
    try:
        # Convert stage to dict for template processing
        stage_dict = stage.model_dump()

        def render_recursive(obj: Any) -> Any:
            if isinstance(obj, str):
                # Create Jinja2 environment for each string template
                env = jinja2.Environment(variable_start_string="{{", variable_end_string="}}", undefined=jinja2.StrictUndefined)
                template = env.from_string(obj)
                return template.render(variables)
            elif isinstance(obj, dict):
                return {key: render_recursive(value) for key, value in obj.items()}
            elif isinstance(obj, list):
                return [render_recursive(item) for item in obj]
            else:
                return obj

        # Recursively render all string values in the stage
        rendered_stage_dict = render_recursive(stage_dict)

        return Stage.model_validate(rendered_stage_dict)
    except jinja2.UndefinedError as e:
        raise VariableSubstitutionError(f"Undefined variable in template: {e}") from e
    except jinja2.TemplateError as e:
        raise VariableSubstitutionError(f"Template rendering error: {e}") from e
    except ValidationError as e:
        raise VariableSubstitutionError(f"Stage validation failed after variable substitution: {e}") from e
    except Exception as e:
        raise VariableSubstitutionError(f"Failed to substitute variables in stage: {e}") from e


def execute_single_stage(stage: Stage, variable_context: dict[str, Any], session: requests.Session) -> None:
    stage_name: str = stage.name
    try:
        stage: Stage = substitute_variables(stage, variable_context)
    except VariableSubstitutionError as e:
        pytest.fail(f"Stage '{stage_name}' - {e}")

    request_params: dict[str, Any] = {}
    if stage.request.params:
        request_params["params"] = stage.request.params
    if stage.request.headers:
        request_params["headers"] = stage.request.headers
    if stage.request.json is not None:
        request_params["json"] = stage.request.json

    try:
        call_response: requests.Response = session.request(stage.request.method.value, stage.request.url, **request_params)
    except requests.Timeout:
        pytest.fail(f"HTTP request timed out for stage '{stage_name}' to URL: {stage.request.url}")
    except requests.ConnectionError as e:
        pytest.fail(f"HTTP connection error for stage '{stage_name}' to URL: {stage.request.url} - {e}")
    except requests.RequestException as e:
        pytest.fail(f"HTTP request failed for stage '{stage_name}' to URL: {stage.request.url} - {e}")

    response_json: dict[str, Any] | None = call_response.json() if call_response.headers.get("content-type", "").startswith("application/json") else None

    if stage.response and stage.response.save and stage.response.save.vars:
        if response_json is None:
            pytest.fail(f"Cannot save variables from JSON for stage '{stage_name}': response is not valid JSON")

        for var_name, jmespath_expr in stage.response.save.vars.items():
            try:
                saved_value = jmespath.search(jmespath_expr, response_json)
                variable_context[var_name] = saved_value
            except Exception as e:
                pytest.fail(f"Error saving variable '{var_name}': {e}")

        # run variable substitution again to update the stage with the saved variables
        try:
            stage: Stage = substitute_variables(stage, variable_context)
        except VariableSubstitutionError as e:
            pytest.fail(f"Stage '{stage_name}' - {e}")

    if stage.response and stage.response.save and stage.response.save.functions:
        for func_item in stage.response.save.functions:
            try:
                if isinstance(func_item, str):
                    func_name = func_item
                    kwargs = None
                else:
                    func_name = func_item.function
                    kwargs = func_item.kwargs

                returned_vars = UserFunction.call_with_kwargs(func_name, call_response, kwargs)

                if not isinstance(returned_vars, dict):
                    pytest.fail(f"Function '{func_name}' must return a dictionary of variables, got {type(returned_vars)} for stage '{stage_name}'")

                for var_name, var_value in returned_vars.items():
                    if not var_name.isidentifier():
                        pytest.fail(f"Function '{func_name}' returned invalid variable name '{var_name}' for stage '{stage_name}'")

                    variable_context[var_name] = var_value

            except Exception as e:
                pytest.fail(f"Error executing function '{func_name}' for stage '{stage_name}': {e}")

        # run variable substitution again to update the stage with the saved variables
        try:
            stage: Stage = substitute_variables(stage, variable_context)
        except VariableSubstitutionError as e:
            pytest.fail(f"Stage '{stage_name}' - {e}")

    if stage.response:
        if stage.response.verify:
            if stage.response.verify.status:
                expected_status = stage.response.verify.status.value
                actual_status = call_response.status_code
                if actual_status != expected_status:
                    pytest.fail(f"Status code verification failed for stage '{stage_name}': expected {expected_status}, got {actual_status}")
            if stage.response.verify.vars:
                for var_name, expected_value in stage.response.verify.vars.items():
                    if var_name not in variable_context:
                        pytest.fail(f"Variable '{var_name}' not found in variable_context for stage '{stage_name}'")
                    var_value = variable_context[var_name]
                    if var_value != expected_value:
                        pytest.fail(f"Variable '{var_name}' verification failed for stage '{stage_name}': expected {expected_value}, got {var_value}")
            if stage.response.verify.functions:
                for func_item in stage.response.verify.functions:
                    try:
                        if isinstance(func_item, str):
                            func_name = func_item
                            kwargs = None
                        else:
                            func_name = func_item.function
                            kwargs = func_item.kwargs

                        verification_result = UserFunction.call_with_kwargs(func_name, call_response, kwargs)

                        if not isinstance(verification_result, bool):
                            pytest.fail(f"Verify function '{func_name}' must return a boolean, got {type(verification_result)} for stage '{stage_name}'")

                        if not verification_result:
                            pytest.fail(f"Verify function '{func_name}' failed for stage '{stage_name}'")

                    except Exception as e:
                        pytest.fail(f"Error executing verify function '{func_name}' for stage '{stage_name}': {e}")


class StageType(StrEnum):
    FLOW = "flow"
    FINAL = "final"


class JSONScenario(Collector):
    def __init__(self, model: Scenario, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._model = model
        self._variable_context: dict[str, Any] = {}
        self._http_session: requests.Session | None = None
        self._flow_failed: bool = False
        self._final_failed: bool = False

    @property
    def model(self) -> Scenario:
        """Pydantic model"""
        return self._model

    @property
    def variable_context(self) -> dict[str, Any]:
        """Variable context for sharing data between stages"""
        return self._variable_context

    @variable_context.setter
    def variable_context(self, value: dict[str, Any]) -> None:
        self._variable_context = value

    @property
    def http_session(self) -> requests.Session | None:
        """HTTP session for making requests"""
        return self._http_session

    @property
    def flow_failed(self) -> bool:
        """Flag indicating if any flow stage has failed"""
        return self._flow_failed

    @flow_failed.setter
    def flow_failed(self, value: bool) -> None:
        self._flow_failed = value

    @property
    def final_failed(self) -> bool:
        """Flag indicating if any final stage has failed"""
        return self._final_failed

    @final_failed.setter
    def final_failed(self, value: bool) -> None:
        self._final_failed = value

    def setup(self) -> None:
        """Initialize session and variable context for the scenario"""
        self.variable_context = {}
        self._http_session = requests.Session()

        if self.model.aws:
            try:
                aws_auth = create_aws_auth(self.model.aws)
                self._http_session.auth = aws_auth
            except Exception as e:
                pytest.fail(f"Failed to setup AWS authentication: {e}")

    def teardown(self) -> None:
        """Cleanup session"""
        if self.http_session:
            self.http_session.close()

    def collect(self) -> Iterable[Item | Collector]:
        """Collect stage functions for both flow and final stages"""

        def collect_stages(stages: Stages, type: StageType) -> Iterable[JSONStage]:
            for stage in stages:
                # Combine scenario fixtures with stage-specific fixtures
                combined_fixtures = list(set(self._model.fixtures + stage.fixtures))
                if combined_fixtures != stage.fixtures:
                    # Only modify stage if fixtures changed
                    stage_dict = stage.model_dump()
                    stage_dict["fixtures"] = combined_fixtures
                    stage = Stage.model_validate(stage_dict)

                yield JSONStage.from_parent(self, name=stage.name, model=stage, scenario=self, type=type)

        # Collect flow stages first, then final stages
        # pytest will execute them in the order we yield them
        yield from collect_stages(self._model.flow, StageType.FLOW)
        yield from collect_stages(self._model.final, StageType.FINAL)


class JSONStage(Function):
    def __init__(self, model: Stage, scenario: JSONScenario, type: StageType, **kwargs: Any) -> None:
        self._model = model
        self._scenario = scenario
        self._type = type

        def execute_stage(**fixture_kwargs: Any) -> None:
            # Check if we should execute this stage
            if not self._should_execute_stage():
                pytest.skip(f"Skipping {self._type} stage '{model.name}' due to flow failure")

            # Add fixtures to variable context
            for fixture_name in model.fixtures:
                if fixture_name in fixture_kwargs:
                    scenario.variable_context[fixture_name] = fixture_kwargs[fixture_name]

            # Execute the stage and handle failures
            try:
                execute_single_stage(model, scenario.variable_context, scenario.http_session)
            except Exception as e:
                match self._type:
                    case StageType.FLOW:
                        self._scenario.flow_failed = True
                    case StageType.FINAL:
                        self._scenario.final_failed = True
                raise e

        # Set up function signature for fixtures if needed
        if model.fixtures:
            import inspect

            execute_stage.__signature__ = inspect.Signature([inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD) for name in model.fixtures])

        kwargs["callobj"] = execute_stage
        super().__init__(**kwargs)

        # Apply marks from parent scenario after initialization
        if hasattr(scenario, "pytestmark"):
            self.pytestmark = getattr(self, "pytestmark", []) + scenario.pytestmark

    @property
    def model(self) -> Stage:
        """Pydantic model"""
        return self._model

    def _should_execute_stage(self) -> bool:
        """Check if this stage should execute based on scenario failures"""
        match self._type:
            case StageType.FLOW:
                return not self._scenario.flow_failed
            case StageType.FINAL:
                return not self._scenario.final_failed
            case _:
                return True


class FailedValidationItem(pytest.Item):
    def __init__(self, error: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.error: str = error

    def runtest(self) -> None:
        pytest.fail(self.error)

    def reportinfo(self) -> tuple[Path, int, str]:
        return self.path, 0, self.name


class JSONFile(pytest.File):
    def _failed_validation_item(self, error: str) -> FailedValidationItem:
        return FailedValidationItem.from_parent(self, name=self.name, error=error)

    def collect(self) -> Iterable[Item | Collector]:
        try:
            test_text: str = self.path.read_text()
            test_data: dict[str, Any] = json.loads(test_text)
        except json.JSONDecodeError as e:
            yield self._failed_validation_item(f"Invalid JSON: {e}")
            return
        except Exception as e:
            yield self._failed_validation_item(f"Error reading file: {e}")
            return

        try:
            processed_data: dict[str, Any] = jsonref.replace_refs(test_data, base_uri=self.path.as_uri())
        except Exception as e:
            yield self._failed_validation_item(f"JSONRef error: {e}")
            return

        try:
            scenario: Scenario = Scenario.model_validate(processed_data)
        except ValidationError as e:
            yield self._failed_validation_item(f"Validation error: {e}")
            return
        except Exception as e:
            yield self._failed_validation_item(f"Unexpected validation error: {e}")
            return

        try:
            marks: list[str] = list(scenario.marks)
            [warnings.warn("skipif marker is not supported", SyntaxWarning, stacklevel=2) for mark in marks if mark.startswith("skipif(")]
        except Exception as e:
            yield self._failed_validation_item(f"Error extracting marks: {e}")
            return

        try:
            # Create the scenario class
            scenario_class = JSONScenario.from_parent(self, name=f"Test{self.name.title().replace('_', '')}", model=scenario)

            # Apply marks to the class
            for mark in marks:
                try:
                    mark_obj = eval(f"pytest.mark.{mark}")
                    scenario_class.pytestmark = getattr(scenario_class, "pytestmark", []) + [mark_obj]
                except Exception as e:
                    yield self._failed_validation_item(f"Failed to apply mark '{mark}': {e}")
                    return

            yield scenario_class
        except Exception as e:
            yield self._failed_validation_item(f"Error creating scenario class: {e}")


def pytest_collect_file(file_path: Path, parent: Collector) -> JSONFile | None:
    pattern, group_name = get_test_name_pattern(parent.config)
    match: re.Match[str] | None = pattern.match(file_path.name)
    if match:
        return JSONFile.from_parent(parent, path=file_path, name=match.group(group_name))
    return None
