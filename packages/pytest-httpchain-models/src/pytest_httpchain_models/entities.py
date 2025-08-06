from http import HTTPMethod, HTTPStatus
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Discriminator, Field, JsonValue, PositiveFloat, RootModel, Tag, model_validator
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


def get_request_body_discriminator(v: Any) -> str:
    """Discriminator function for request body types."""
    # For dict inputs, check which field is present
    if isinstance(v, dict):
        body_fields = {"json", "xml", "form", "raw", "files"}
        found = body_fields & v.keys()
        if found:
            return found.pop()

    # For object inputs, map class name to discriminator
    if hasattr(v, "__class__"):
        class_to_tag = {"JsonBody": "json", "XmlBody": "xml", "FormBody": "form", "RawBody": "raw", "FilesBody": "files"}
        tag = class_to_tag.get(v.__class__.__name__)
        if tag:
            return tag

    raise ValueError("Unable to determine body type")


class JsonBody(BaseModel):
    """JSON request body."""

    json: JsonValue = Field(description="JSON data to send.")
    model_config = ConfigDict(extra="forbid")


class XmlBody(BaseModel):
    """XML request body."""

    xml: XMLSting | PartialTemplateStr = Field(description="XML content as string.")
    model_config = ConfigDict(extra="forbid")


class FormBody(BaseModel):
    """Form-encoded request body."""

    form: dict[str, Any] = Field(description="Form data to be URL-encoded.")
    model_config = ConfigDict(extra="forbid")


class RawBody(BaseModel):
    """Raw text request body."""

    raw: str = Field(description="Raw text content.")
    model_config = ConfigDict(extra="forbid")


class FilesBody(BaseModel):
    """Multipart file upload request body."""

    files: dict[str, SerializablePath | PartialTemplateStr] = Field(description="Files to upload from file paths.")
    model_config = ConfigDict(extra="forbid")


# Discriminated union with callable discriminator
RequestBody = Annotated[
    Annotated[JsonBody, Tag("json")] | Annotated[XmlBody, Tag("xml")] | Annotated[FormBody, Tag("form")] | Annotated[RawBody, Tag("raw")] | Annotated[FilesBody, Tag("files")],
    Discriminator(get_request_body_discriminator),
]


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


def get_response_step_discriminator(v: Any) -> str:
    """Discriminator function for response steps."""
    # For dict inputs, check which field is present
    if isinstance(v, dict):
        step_fields = {"save", "verify"}
        found = step_fields & v.keys()
        if found:
            return found.pop()

    # For object inputs, map class name to discriminator
    if hasattr(v, "__class__"):
        class_to_tag = {"SaveStep": "save", "VerifyStep": "verify"}
        tag = class_to_tag.get(v.__class__.__name__)
        if tag:
            return tag

    raise ValueError("Unable to determine step type")


class SaveStep(BaseModel):
    """Save data from HTTP response."""

    save: Save = Field(description="Save configuration.")
    model_config = ConfigDict(extra="forbid")


class VerifyStep(BaseModel):
    """Verify HTTP response and data context."""

    verify: Verify = Field(description="Verify configuration.")
    model_config = ConfigDict(extra="forbid")


# Discriminated union with callable discriminator
ResponseStep = Annotated[Annotated[SaveStep, Tag("save")] | Annotated[VerifyStep, Tag("verify")], Discriminator(get_response_step_discriminator)]


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
    request: Request = Field(description="HTTP request details.")
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
