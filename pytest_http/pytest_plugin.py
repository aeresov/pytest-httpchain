import inspect
import json
import re
import warnings
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import jmespath
import jsonref
import pytest
import requests
from _pytest.config import Config
from _pytest.config.argparsing import Parser
from _pytest.nodes import Collector, Item
from _pytest.outcomes import Failed
from _pytest.python import Function
from pydantic import ValidationError
from requests.auth import AuthBase

from pytest_http.models import AWSCredentials, AWSProfile, Scenario, Stage, Stages
from pytest_http.user_function import UserFunction

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
        json_text: str = stage.model_dump_json(by_alias=True)

        # Sort by length (longest first) to avoid partial replacements
        for name, value in sorted(variables.items(), key=lambda x: len(x[0]), reverse=True):
            placeholder: str = f"{{{name}}}"
            # Check if placeholder is surrounded by quotes (inside a JSON string)
            quoted_placeholder: str = f'"{placeholder}"'
            if quoted_placeholder in json_text:
                # Full JSON value replacement
                json_value: str = json.dumps(value)
                json_text = json_text.replace(quoted_placeholder, json_value)
            else:
                # String interpolation within JSON string
                json_text = json_text.replace(placeholder, str(value))

        return Stage.model_validate_json(json_text)
    except ValidationError as e:
        raise VariableSubstitutionError(f"Stage validation failed after variable substitution: {e}") from e
    except Exception as e:
        raise VariableSubstitutionError(f"Failed to substitute variables in stage: {e}") from e


def execute_stages(stages: Stages, variable_context: dict[str, Any], session: requests.Session) -> None:
    for stage in stages:
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

                    returned_vars = UserFunction.call_function_with_kwargs(func_name, call_response, kwargs)

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

                            verification_result = UserFunction.call_function_with_kwargs(func_name, call_response, kwargs)

                            if not isinstance(verification_result, bool):
                                pytest.fail(f"Verify function '{func_name}' must return a boolean, got {type(verification_result)} for stage '{stage_name}'")

                            if not verification_result:
                                pytest.fail(f"Verify function '{func_name}' failed for stage '{stage_name}'")

                        except Exception as e:
                            pytest.fail(f"Error executing verify function '{func_name}' for stage '{stage_name}': {e}")


def json_test_function(stages: Stages, final: Stages, aws_config: AWSProfile | AWSCredentials | None = None, **fixtures: Any) -> None:
    variable_context: dict[str, Any] = dict(fixtures)
    session: requests.Session = requests.Session()

    if aws_config:
        try:
            aws_auth = create_aws_auth(aws_config)
            session.auth = aws_auth
        except Exception as e:
            pytest.fail(f"Failed to setup AWS authentication: {e}")

    main_flow_error: Failed | None = None
    try:
        execute_stages(stages, variable_context, session)
    except Failed as e:
        main_flow_error = e
    execute_stages(final, variable_context, session)
    if main_flow_error:
        raise main_flow_error


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
            fixtures: list[str] = list(scenario.fixtures)
            marks: list[str] = list(scenario.marks)
            [warnings.warn("skipif marker is not supported", SyntaxWarning, stacklevel=2) for mark in marks if mark.startswith("skipif(")]
        except Exception as e:
            yield self._failed_validation_item(f"Error extracting fixtures/marks: {e}")
            return

        try:

            def test_func(**kwargs: Any) -> None:
                return json_test_function(scenario.flow, scenario.final, scenario.aws, **{name: kwargs[name] for name in fixtures if name in kwargs})

            test_func.__signature__ = inspect.Signature([inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD) for name in fixtures])  # type: ignore
            test_func.__name__ = f"test_{self.name}"

            for mark in marks:
                try:
                    test_func = eval(f"pytest.mark.{mark}")(test_func)
                except Exception as e:
                    yield self._failed_validation_item(f"Failed to apply mark '{mark}': {e}")
                    return

            yield Function.from_parent(self, name=self.name, callobj=test_func)
        except (SyntaxError, NameError, AttributeError) as e:
            yield self._failed_validation_item(f"Failed to apply marker: {e}")
        except Exception as e:
            yield self._failed_validation_item(f"Error creating test function: {e}")


def pytest_collect_file(file_path: Path, parent: Collector) -> JSONFile | None:
    pattern, group_name = get_test_name_pattern(parent.config)
    match: re.Match[str] | None = pattern.match(file_path.name)
    if match:
        return JSONFile.from_parent(parent, path=file_path, name=match.group(group_name))
    return None
