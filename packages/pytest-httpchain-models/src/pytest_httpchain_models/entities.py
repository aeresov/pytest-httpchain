from http import HTTPMethod, HTTPStatus
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, PositiveFloat, RootModel, model_validator
from pydantic.networks import HttpUrl

from pytest_httpchain_models.types import (
    FunctionImportName,
    JMESPathExpression,
    JSONSchemaInline,
    PartialTemplateStr,
    RegexPattern,
    SerializablePath,
    TemplateExpression,
    VariableName,
    XMLSting,
)


class SSLConfig(BaseModel):
    verify: Literal[True, False] | SerializablePath | TemplateExpression = Field(
        default=True,
        description="SSL certificate verification. True (verify), False (no verification), or path to CA bundle.",
        examples=[False, "/path/to/ca-bundle.crt", "{{ verify_ssl }}"],
    )
    cert: tuple[SerializablePath | PartialTemplateStr, SerializablePath | PartialTemplateStr] | SerializablePath | PartialTemplateStr | None = Field(
        default=None,
        description="SSL client certificate. Single file path or tuple of (cert_path, key_path).",
        examples=[
            ["/path/to/client.crt", "/path/to/client.key"],
            ["/path/to/{{ client_cert_name }}", "/path/to/client.key"],
            "/path/to/client.pem",
            "/path/to/{{ cert_file_name }}",
        ],
    )


class UserFunctionName(RootModel):
    root: FunctionImportName | PartialTemplateStr = Field(
        description="Name of the function to be called.",
        examples=[
            "module.submodule:funcname",
            "module.{{ submodule_name }}:funcname",
        ],
    )


class UserFunctionKwargs(BaseModel):
    function: UserFunctionName
    kwargs: dict[VariableName, Any] = Field(default_factory=dict, description="Function arguments.")


UserFunctionCall = UserFunctionName | UserFunctionKwargs


class Functions(RootModel):
    root: list[UserFunctionCall] = Field(default_factory=list)

    def __iter__(self):
        return iter(self.root)

    def __getitem__(self, item):
        return self.root[item]


class RequestBodyBase(BaseModel):
    """Base class for request body types."""

    model_config = ConfigDict(extra="forbid")


class JsonBody(RequestBodyBase):
    """JSON request body."""

    body_type: Literal["json"] = "json"
    json: JsonValue = Field(description="JSON data to send.")


class XmlBody(RequestBodyBase):
    """XML request body."""

    body_type: Literal["xml"] = "xml"
    xml: XMLSting | PartialTemplateStr = Field(description="XML content as string.")


class FormBody(RequestBodyBase):
    """Form-encoded request body."""

    body_type: Literal["form"] = "form"
    form: dict[str, Any] = Field(description="Form data to be URL-encoded.")


class RawBody(RequestBodyBase):
    """Raw text request body."""

    body_type: Literal["raw"] = "raw"
    raw: str = Field(description="Raw text content.")


class FilesBody(RequestBodyBase):
    """Multipart file upload request body."""

    body_type: Literal["files"] = "files"
    files: dict[str, SerializablePath | PartialTemplateStr] = Field(description="Files to upload from file paths.")


# Simplified discriminated union using Pydantic's built-in discrimination
RequestBody = Annotated[JsonBody | XmlBody | FormBody | RawBody | FilesBody, Field(discriminator="body_type")]


class CallSecurity(BaseModel):
    """Security configuration for HTTP calls."""

    ssl: SSLConfig = Field(
        default_factory=SSLConfig,
        description="SSL/TLS configuration.",
    )
    auth: UserFunctionCall | None = Field(
        default=None,
        description="User function to create custom authentication.",
    )


class Request(CallSecurity):
    """HTTP request configuration."""

    url: HttpUrl | PartialTemplateStr = Field()
    method: HTTPMethod | TemplateExpression = Field(default=HTTPMethod.GET)
    params: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: RequestBody | None = Field(default=None, description="Request body configuration.")
    timeout: PositiveFloat | TemplateExpression = Field(default=30.0, description="Request timeout in seconds.")
    allow_redirects: Literal[True, False] | TemplateExpression = Field(default=True, description="Whether to follow redirects.")


class Save(BaseModel):
    """Configuration for saving data from HTTP response."""

    vars: dict[str, JMESPathExpression | PartialTemplateStr] = Field(default_factory=dict, description="JMESPath expressions to extract values.")
    functions: Functions = Field(default_factory=Functions, description="Functions to process response data.")


class ResponseBody(BaseModel):
    """Response body validation configuration."""

    schema: JSONSchemaInline | SerializablePath | PartialTemplateStr | None = Field(default=None, description="JSON schema for validation.")
    contains: list[str] = Field(default_factory=list)
    not_contains: list[str] = Field(default_factory=list)
    matches: list[RegexPattern] = Field(default_factory=list)
    not_matches: list[RegexPattern] = Field(default_factory=list)


class Verify(BaseModel):
    """Response verification configuration."""

    status: HTTPStatus | None | TemplateExpression = Field(default=None)
    headers: dict[str, str] = Field(default_factory=dict)
    vars: dict[str, Any] = Field(default_factory=dict)
    functions: Functions = Field(default_factory=Functions)
    body: ResponseBody = Field(default_factory=ResponseBody)


class Decorated(BaseModel):
    """Pytest test decoration configuration."""

    marks: list[str] = Field(default_factory=list, examples=["xfail", "skip"])
    fixtures: list[str] = Field(default_factory=list)
    vars: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_no_conflicts(self) -> Self:
        """Ensure fixtures and vars don't conflict."""
        conflicts = set(self.fixtures) & self.vars.keys()
        if conflicts:
            raise ValueError(f"Conflicting fixtures and vars: {', '.join(conflicts)}")
        return self


class ResponseStepBase(BaseModel):
    """Base class for response step types."""

    model_config = ConfigDict(extra="forbid")


class SaveStep(ResponseStepBase):
    """Save data from HTTP response."""

    step_type: Literal["save"] = "save"
    save: Save = Field(description="Save configuration.")


class VerifyStep(ResponseStepBase):
    """Verify HTTP response and data context."""

    step_type: Literal["verify"] = "verify"
    verify: Verify = Field(description="Verify configuration.")


# Simplified discriminated union for response steps
ResponseStep = Annotated[SaveStep | VerifyStep, Field(discriminator="step_type")]


class Response(RootModel):
    """Sequential response processing configuration."""

    root: list[ResponseStep] = Field(
        default_factory=list,
        description="Sequential steps to process the response. Each step is either a save or verify action.",
        examples=[
            [
                {"verify": {"status": 200}},
                {"save": {"vars": {"user_id": "$.id"}}},
                {"verify": {"vars": {"user_id": "12345"}}},
            ],
            [
                {"verify": {"status": 500}},
                {"verify": {"body": {"contains": ["error", "failed"]}}},
            ],
        ],
    )

    def __iter__(self):
        return iter(self.root)

    def __getitem__(self, item):
        return self.root[item]


class Stage(Decorated):
    """HTTP test stage configuration."""

    name: str = Field(description="Stage name (human-readable).")
    always_run: Literal[True, False] | TemplateExpression = Field(default=False, examples=[True, "{{ should_run }}", "{{ env == 'production' }}"])
    request: Any = Field(description="HTTP request details.")
    response: Response = Field(default_factory=Response)


class Scenario(Decorated, CallSecurity):
    """HTTP test scenario with multiple stages."""

    stages: list[Stage] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_no_var_conflicts(self) -> Self:
        """Ensure stage variables don't conflict with fixtures."""
        for stage in self.stages:
            for step in stage.response:
                if isinstance(step, SaveStep) and step.save.vars:
                    conflicts = set(step.save.vars.keys()) & set(self.fixtures)
                    if conflicts:
                        raise ValueError(f"Stage '{stage.name}' has conflicting vars and fixtures: {', '.join(conflicts)}")
        return self
