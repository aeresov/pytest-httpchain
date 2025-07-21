import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import jinja2
import jmespath
import jsonref
import jsonschema
import pytest
import requests
from _pytest.config import Config
from _pytest.config.argparsing import Parser
from _pytest.nodes import Collector, Item
from _pytest.python import Function
from _pytest.reports import TestReport
from _pytest.stash import StashKey
from pydantic import ValidationError
from pytest_http_engine.models import Scenario, Stage
from pytest_http_engine.user_function import UserFunction
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

SUFFIX: str = "suffix"

# Stash key for storing HTTP request/response data
http_details_key = StashKey[list[dict[str, Any]]]()


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


def substitute_variables_in_auth(auth_spec: str | Any, variables: dict[str, Any]) -> str | Any:
    """Apply variable substitution to auth specification."""
    if isinstance(auth_spec, str):
        # Simple function name string
        env = jinja2.Environment(variable_start_string="{{", variable_end_string="}}", undefined=jinja2.StrictUndefined)
        template = env.from_string(auth_spec)
        return template.render(variables)
    else:
        # FunctionCall object - need to substitute in kwargs
        from pytest_http_engine.models import FunctionCall

        auth_dict = auth_spec.model_dump()

        def render_recursive(obj: Any) -> Any:
            if isinstance(obj, str):
                env = jinja2.Environment(variable_start_string="{{", variable_end_string="}}", undefined=jinja2.StrictUndefined)
                template = env.from_string(obj)
                return template.render(variables)
            elif isinstance(obj, dict):
                return {key: render_recursive(value) for key, value in obj.items()}
            elif isinstance(obj, list):
                return [render_recursive(item) for item in obj]
            else:
                return obj

        rendered_auth_dict = render_recursive(auth_dict)
        return FunctionCall.model_validate(rendered_auth_dict)


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


def _format_request_response_details(stage: Stage, request_params: dict[str, Any], response: requests.Response) -> str:
    """Format detailed request and response information using Rich and return as string."""
    console = Console(record=True, width=120, force_terminal=False)

    # Create request details table
    request_table = Table(title=f"🔄 HTTP Request - {stage.name}", show_header=True, header_style="bold blue")
    request_table.add_column("Property", style="cyan", no_wrap=True)
    request_table.add_column("Value", style="white")

    # Add basic request info
    request_table.add_row("Method", stage.request.method.value)
    request_table.add_row("URL", stage.request.url)

    # Add query parameters if present
    if request_params.get("params"):
        params_str = json.dumps(request_params["params"], indent=2)
        request_table.add_row("Query Params", Syntax(params_str, "json", theme="github-dark"))

    # Add headers if present
    if request_params.get("headers"):
        headers_str = json.dumps(request_params["headers"], indent=2)
        request_table.add_row("Headers", Syntax(headers_str, "json", theme="github-dark"))

    # Add request body if present
    if "json" in request_params:
        body_str = json.dumps(request_params["json"], indent=2)
        request_table.add_row("Body (JSON)", Syntax(body_str, "json", theme="github-dark"))
    elif "data" in request_params:
        if isinstance(request_params["data"], str):
            request_table.add_row("Body (Text)", request_params["data"])
        else:
            request_table.add_row("Body (Form)", str(request_params["data"]))
    elif "files" in request_params:
        files_info = ", ".join(request_params["files"].keys())
        request_table.add_row("Files", files_info)

    # Add timeout if present
    if request_params.get("timeout"):
        request_table.add_row("Timeout", f"{request_params['timeout']}s")

    console.print(request_table)

    # Create response details table
    response_table = Table(title="📥 HTTP Response", show_header=True, header_style="bold green")
    response_table.add_column("Property", style="cyan", no_wrap=True)
    response_table.add_column("Value", style="white")

    # Status code with color
    status_color = "green" if 200 <= response.status_code < 300 else "yellow" if 300 <= response.status_code < 400 else "red"
    status_text = Text(str(response.status_code), style=f"bold {status_color}")
    response_table.add_row("Status Code", status_text)

    # Response headers
    if response.headers:
        headers_dict = dict(response.headers)
        headers_str = json.dumps(headers_dict, indent=2)
        response_table.add_row("Headers", Syntax(headers_str, "json", theme="github-dark"))

    # Response body
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        try:
            response_json = response.json()
            body_str = json.dumps(response_json, indent=2)
            response_table.add_row("Body (JSON)", Syntax(body_str, "json", theme="github-dark"))
        except Exception:
            response_table.add_row("Body (Text)", response.text[:500] + "..." if len(response.text) > 500 else response.text)
    elif content_type.startswith("text/"):
        response_table.add_row("Body (Text)", response.text[:500] + "..." if len(response.text) > 500 else response.text)
    elif response.content:
        response_table.add_row("Body Size", f"{len(response.content)} bytes")

    # Response time
    if hasattr(response, "elapsed"):
        response_table.add_row("Response Time", f"{response.elapsed.total_seconds():.3f}s")

    console.print(response_table)
    console.print()  # Add spacing

    # Export the rich output as text
    return console.export_text()


def _store_http_details(item: Item, stage: Stage, request_params: dict[str, Any], response: requests.Response) -> None:
    """Store HTTP request/response details in the test item stash."""
    if not hasattr(item, "stash"):
        return

    formatted_output = _format_request_response_details(stage, request_params, response)

    # Initialize the list if it doesn't exist
    if http_details_key not in item.stash:
        item.stash[http_details_key] = []

    # Store the formatted output
    item.stash[http_details_key].append({"stage_name": stage.name, "formatted_output": formatted_output})


def execute_single_stage(stage: Stage, variable_context: dict[str, Any], session: requests.Session, item: Item | None = None) -> None:
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
    if stage.request.timeout:
        request_params["timeout"] = stage.request.timeout

    # Set allow_redirects based on request configuration
    request_params["allow_redirects"] = stage.request.allow_redirects

    # Add SSL configuration for this specific request (overrides session SSL)
    if stage.request.ssl:
        if stage.request.ssl.verify is not None:
            request_params["verify"] = stage.request.ssl.verify
        if stage.request.ssl.cert is not None:
            request_params["cert"] = stage.request.ssl.cert

    # Add authentication for this specific request (overrides session auth)
    if stage.request.auth:
        try:
            auth_instance = UserFunction.call_auth_function_from_spec(stage.request.auth)
            request_params["auth"] = auth_instance
        except Exception as e:
            pytest.fail(f"Failed to configure stage authentication '{stage.request.auth}' for stage '{stage_name}': {e}")

    # Handle different body types
    if stage.request.body:
        from pytest_http_engine.models import FilesBody, FormBody, JsonBody, RawBody, XmlBody

        match stage.request.body:
            case JsonBody(json=data):
                request_params["json"] = data
            case FormBody(form=data):
                request_params["data"] = data
            case XmlBody(xml=data):
                request_params["data"] = data
            case RawBody(raw=data):
                request_params["data"] = data
            case FilesBody(files=files_dict):
                # Process files - all values are file paths
                files = {}
                for field_name, file_path in files_dict.items():
                    try:
                        files[field_name] = open(file_path, "rb")
                    except FileNotFoundError:
                        pytest.fail(f"File not found for upload: {file_path}")
                    except Exception as e:
                        pytest.fail(f"Error opening file {file_path}: {e}")

                request_params["files"] = files

    try:
        call_response: requests.Response = session.request(stage.request.method.value, stage.request.url, **request_params)

        # Store request and response details for pytest reporting
        if item is not None:
            _store_http_details(item, stage, request_params, call_response)

    except requests.Timeout:
        pytest.fail(f"HTTP request timed out for stage '{stage_name}' to URL: {stage.request.url}")
    except requests.ConnectionError as e:
        pytest.fail(f"HTTP connection error for stage '{stage_name}' to URL: {stage.request.url} - {e}")
    except requests.RequestException as e:
        pytest.fail(f"HTTP request failed for stage '{stage_name}' to URL: {stage.request.url} - {e}")
    finally:
        # Clean up opened files
        if "files" in request_params:
            for _, file_obj in request_params["files"].items():
                if hasattr(file_obj, "close"):
                    file_obj.close()

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
            if stage.response.verify.headers:
                for header_name, expected_value in stage.response.verify.headers.items():
                    actual_value = call_response.headers.get(header_name)
                    if actual_value != expected_value:
                        pytest.fail(f"Header '{header_name}' verification failed for stage '{stage_name}': expected '{expected_value}', got '{actual_value}'")
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

            if stage.response.verify.body:
                # Handle schema validation
                if stage.response.verify.body.schema:
                    # Get the response body as JSON
                    try:
                        response_json = call_response.json()
                    except Exception as e:
                        pytest.fail(f"Failed to parse response body as JSON for stage '{stage_name}': {e}")

                    # Get the schema
                    schema_config = stage.response.verify.body.schema

                    if isinstance(schema_config, str | Path):
                        # Load schema from file path
                        schema_path = Path(str(schema_config))
                        if not schema_path.is_absolute():
                            # Make it relative to current working directory
                            schema_path = Path.cwd() / schema_path

                        try:
                            with open(schema_path) as f:
                                schema = json.load(f)
                        except Exception as e:
                            pytest.fail(f"Failed to load schema file '{schema_path}' for stage '{stage_name}': {e}")
                    else:
                        # Use inline schema
                        schema = schema_config

                    # Validate the response against the schema
                    try:
                        jsonschema.validate(instance=response_json, schema=schema)
                    except jsonschema.ValidationError as e:
                        pytest.fail(f"Response body schema validation failed for stage '{stage_name}': {e.message}")
                    except jsonschema.SchemaError as e:
                        pytest.fail(f"Invalid JSON schema for stage '{stage_name}': {e.message}")

                # Handle substring validation
                if stage.response.verify.body.contains or stage.response.verify.body.not_contains:
                    # Get response body as text
                    response_text = call_response.text

                    # Check substrings that must be present
                    if stage.response.verify.body.contains:
                        for substring in stage.response.verify.body.contains:
                            if substring not in response_text:
                                pytest.fail(f"Body substring verification failed for stage '{stage_name}': substring '{substring}' not found in response body")

                    # Check substrings that must not be present
                    if stage.response.verify.body.not_contains:
                        for substring in stage.response.verify.body.not_contains:
                            if substring in response_text:
                                pytest.fail(f"Body substring verification failed for stage '{stage_name}': substring '{substring}' found in response body")

                # Handle regex validation
                if stage.response.verify.body.matches or stage.response.verify.body.not_matches:
                    # Get response body as text if not already retrieved
                    if "response_text" not in locals():
                        response_text = call_response.text

                    # Check patterns that must match
                    if stage.response.verify.body.matches:
                        for pattern in stage.response.verify.body.matches:
                            try:
                                if not re.search(pattern, response_text):
                                    pytest.fail(f"Body regex verification failed for stage '{stage_name}': pattern '{pattern}' did not match response body")
                            except re.error as e:
                                pytest.fail(f"Invalid regex pattern '{pattern}' for stage '{stage_name}': {e}")

                    # Check patterns that must not match
                    if stage.response.verify.body.not_matches:
                        for pattern in stage.response.verify.body.not_matches:
                            try:
                                if re.search(pattern, response_text):
                                    pytest.fail(f"Body regex verification failed for stage '{stage_name}': pattern '{pattern}' matched response body but should not have")
                            except re.error as e:
                                pytest.fail(f"Invalid regex pattern '{pattern}' for stage '{stage_name}': {e}")


class JSONScenario(Collector):
    def __init__(self, model: Scenario, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._model = model
        self._variable_context: dict[str, Any] = {}
        self._http_session: requests.Session | None = None
        self._flow_failed: bool = False

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

    def setup(self) -> None:
        """Initialize session and variable context for the scenario"""
        self.variable_context = self.model.vars.copy() if self.model.vars else {}
        self._http_session = requests.Session()

        # Configure SSL settings for the session
        if self.model.ssl:
            if self.model.ssl.verify is not None:
                self._http_session.verify = self.model.ssl.verify
            if self.model.ssl.cert is not None:
                self._http_session.cert = self.model.ssl.cert

        # Configure authentication for the session
        if self.model.auth:
            try:
                # Apply variable substitution to auth spec
                resolved_auth = substitute_variables_in_auth(self.model.auth, self.variable_context)
                auth_instance = UserFunction.call_auth_function_from_spec(resolved_auth)
                self._http_session.auth = auth_instance
            except Exception as e:
                import pytest

                pytest.fail(f"Failed to configure scenario authentication '{self.model.auth}': {e}")

    def teardown(self) -> None:
        """Cleanup session"""
        if self.http_session:
            self.http_session.close()

    def collect(self) -> Iterable[Item | Collector]:
        """Collect stage functions from unified stages collection"""

        def collect_stages(stages: list[Stage]) -> Iterable[JSONStage]:
            for stage in stages:
                combined_fixtures = list(set(self._model.fixtures + stage.fixtures))
                if combined_fixtures != stage.fixtures:
                    stage_dict = stage.model_dump()
                    stage_dict["fixtures"] = combined_fixtures
                    stage = Stage.model_validate(stage_dict)

                yield JSONStage.from_parent(self, name=stage.name, model=stage, scenario=self)

        # Collect all stages from unified collection
        yield from collect_stages(self._model.stages)


class JSONStage(Function):
    def __init__(self, model: Stage, scenario: JSONScenario, **kwargs: Any) -> None:
        self._model = model
        self._scenario = scenario

        def execute_stage(**fixture_kwargs: Any) -> None:
            # Check if we should execute this stage
            if not self._should_execute_stage():
                pytest.skip(f"Skipping stage '{model.name}' due to flow failure")

            # Add fixtures to variable context
            for fixture_name in model.fixtures:
                if fixture_name in fixture_kwargs:
                    scenario.variable_context[fixture_name] = fixture_kwargs[fixture_name]

            # Execute the stage and handle failures
            try:
                execute_single_stage(model, scenario.variable_context, scenario.http_session, self)
            except Exception as e:
                # Only set flow_failed if this is not an always_run stage
                if not model.always_run:
                    self._scenario.flow_failed = True
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
        # If stage has always_run=True, it always executes
        if self._model.always_run:
            return True

        # Otherwise, only execute if no previous flow stages have failed
        return not self._scenario.flow_failed


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
            # Create the scenario class
            scenario_class = JSONScenario.from_parent(self, name=f"Test{self.name.title().replace('_', '')}", model=scenario)

            # Apply marks to the class
            for mark in scenario.marks:
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


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: Item, call):
    """Add HTTP request/response details to test report sections."""
    outcome = yield
    report: TestReport = outcome.get_result()

    # Only add sections during the 'call' phase and if we have stored HTTP details
    if call.when == "call" and hasattr(item, "stash") and http_details_key in item.stash:
        http_details = item.stash[http_details_key]

        # Add each HTTP request/response as a separate section
        for detail in http_details:
            section_title = f"HTTP Details - {detail['stage_name']}"
            report.sections.append((section_title, detail["formatted_output"]))
