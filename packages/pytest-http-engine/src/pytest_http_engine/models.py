import os
from http import HTTPMethod, HTTPStatus
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from pytest_http_engine.types import FunctionName, JMESPathExpression, JSONSerializable, VariableName


class FunctionCall(BaseModel):
    """
    User function call to be made.

    Attributes:
        function: Full qualified name of the function to be called. Function must be available to import.
        kwargs: Dictionary of arguments to be passed to the function.
    """

    function: FunctionName = Field(description="Name of the function to be called.")
    kwargs: dict[VariableName, Any] | None = Field(default=None, description="Function arguments.")


class Functions(RootModel):
    """
    Collection of functions to be called.
    Functions are called in the order they are provided.

    Attributes:
        root:   List of functions provided by user.
                Each item can be a function name or a function call.
                When using a function name, the function is called with the response as the only argument.
                When using a function call, the function is called with the response as the first argument and the kwargs provided.
    """

    root: list[FunctionName | FunctionCall] = Field(default_factory=list)

    def __iter__(self):
        return iter(self.root)

    def __getitem__(self, item):
        return self.root[item]


class Save(BaseModel):
    """
    Configuration on how to save data from the response.
    Data is saved into a dictionary called variable_context. This context is available for all stages in the scenario. Every stage updates the context with its own saves.

    Attributes:
        vars:       A dictionary where key is the variable name and value is the JMESPath expression to extract the value from the response.
                    Dictionary with extracted values is merged into the variable_context.
                    "vars" are processed first.
        functions:  A list of functions to be called to save data.
                    Function returns a dictionary to be merged into the variable_context.
                    Functions are called after "vars".
                    Functions can use variable_context entries for kwargs, including entries from current stage.
    """

    vars: dict[str, JMESPathExpression] | None = Field(default=None, description="Dictionary of JMESPath expressions to extract the value from the response")
    functions: Functions | None = Field(default=None, description="List of functions to be called to save data.")


class Verify(BaseModel):
    """
    Configuration on how to verify the response.

    Attributes:
        status:     Expected HTTP status code.
        vars:       A dictionary where key is the variable name and value is the expected value.
                    Variables come from variable_context.
                    Variables from current stage are available.
        functions:  List of functions to be called to verify the response.
                    Function returns a boolean value, negative result triggers test failure.
                    Functions are called after "vars".
                    Functions can use variable_context entries for kwargs, including entries from current stage.
    """

    status: HTTPStatus | None = Field(default=None, description="Expected HTTP status code.")
    vars: dict[str, Any] | None = Field(default=None, description="Expected values for variables.")
    functions: Functions | None = Field(default=None, description="List of functions to be called to verify the response.")


class Request(BaseModel):
    """
    HTTP request configuration.

    Attributes:
        url:      URL to be requested. Can contain variable names for substitution.
        method:   HTTP method to be used.
        params:   Query parameters to be sent.
        headers:  HTTP headers to be sent.
        json:     JSON body to be sent.
    """

    url: str = Field()
    method: HTTPMethod = Field(default=HTTPMethod.GET)
    params: dict[str, Any] | None = Field(default=None)
    headers: dict[str, str] | None = Field(default=None)
    json: JSONSerializable = Field(default=None)


class Response(BaseModel):
    """
    HTTP response configuration.

    Attributes:
        save:   Configuration on how to save data from the response.
        verify: Configuration on how to verify the response.
    """

    save: Save | None = Field(default=None)
    verify: Verify | None = Field(default=None)


class Stage(BaseModel):
    """
    HTTP request and response configuration.
    Represents a single step in the scenario's test chain.

    Attributes:
        name:     Stage name.
        fixtures: List of pytest fixture names to be supplied to this stage.
        request:  HTTP request configuration.
        response: HTTP response configuration.
    """

    name: str = Field()
    fixtures: list[str] = Field(default_factory=list, description="List of pytest fixture names for this stage")
    request: Request = Field()
    response: Response | None = Field(default=None)


class Stages(RootModel):
    """
    Collection of stages.
    Represents scenario's test chain.
    Stages are executed in the order they are provided.

    Attributes:
        root: List of stages.
    """

    root: list[Stage] = Field(default_factory=list)

    def __iter__(self):
        return iter(self.root)

    def __getitem__(self, item):
        return self.root[item]


class AWSBase(BaseModel):
    """
    Base AWS configuration with common fields.

    Attributes:
        service:    AWS service name (e.g., 'execute-api', 's3', 'lambda').
        region:     AWS region. Defaults to AWS_DEFAULT_REGION env var or 'us-east-1'.
    """

    service: str = Field(description="AWS service name")
    region: str = Field(default_factory=lambda: os.getenv("AWS_DEFAULT_REGION", "us-east-1"), description="AWS region")


class AWSProfile(AWSBase):
    """
    AWS configuration using profile-based authentication.

    Attributes:
        profile:    AWS profile name. Defaults to AWS_PROFILE env var.
    """

    profile: str = Field(default_factory=lambda: os.getenv("AWS_PROFILE", "default"), description="AWS profile name")


class AWSCredentials(AWSBase):
    """
    AWS configuration using credential-based authentication.

    Attributes:
        access_key_id:      AWS access key ID. Defaults to AWS_ACCESS_KEY_ID env var.
        secret_access_key:  AWS secret access key. Defaults to AWS_SECRET_ACCESS_KEY env var.
        session_token:      AWS session token. Defaults to AWS_SESSION_TOKEN env var.
    """

    access_key_id: str = Field(default_factory=lambda: os.getenv("AWS_ACCESS_KEY_ID"), description="AWS access key ID")
    secret_access_key: str = Field(default_factory=lambda: os.getenv("AWS_SECRET_ACCESS_KEY"), description="AWS secret access key")
    session_token: str | None = Field(default_factory=lambda: os.getenv("AWS_SESSION_TOKEN"), description="AWS session token")


class Scenario(BaseModel):
    """
    Scenario represents a pytest test function that runs a chain of HTTP requests.
    Scenario is organized as a collection of stages that are executed in order.

    Attributes:
        fixtures:   List of pytest fixture names to be supplied with, like a regular pytest function.
        marks:      List of marks to be applied to, like to a regular pytest function.
        aws:        AWS configuration for IAM authentication (optional)
        flow:       Main test chain.
        final:      Finalization chain, runs after the flow chain whether it fails or not.
    """

    fixtures: list[str] = Field(default_factory=list, description="List of pytest fixture names (deprecated: use stage-level fixtures instead)")
    marks: list[str] = Field(default_factory=list, description="List of marks to be applied", examples=["xfail", "skip"])
    aws: AWSProfile | AWSCredentials | None = Field(
        default=None,
        description="AWS configuration for IAM authentication",
        examples=[
            {"service": "execute-api", "region": "us-west-2", "profile": "dev"},
            {"service": "s3", "access_key_id": "AKIAIOSFODNN7EXAMPLE", "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"},
        ],
    )
    flow: Stages = Field(default_factory=Stages, description="Main test chain")
    final: Stages = Field(default_factory=Stages, description="Finalization chain")

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="after")
    def validate_save_variables_not_fixtures(self) -> "Scenario":
        if not self.fixtures:
            return self

        fixture_names = set(self.fixtures)

        for stage in self.flow:
            if stage.response and stage.response.save and stage.response.save.vars:
                for var_name in stage.response.save.vars.keys():
                    if var_name in fixture_names:
                        raise ValueError(f"Variable name '{var_name}' conflicts with fixture name")

        return self
