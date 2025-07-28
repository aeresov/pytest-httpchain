from http import HTTPMethod, HTTPStatus
from typing import Any, Self

from pydantic import BaseModel, Field, JsonValue, RootModel, model_validator
from pydantic.networks import HttpUrl

from pytest_http_engine.models.types import FunctionName, JMESPathExpression, JSONSchemaInline, RegexPattern, SerializablePath, VariableName, XMLSting


class SSLConfig(BaseModel):
    verify: bool | SerializablePath = Field(default=True, description="SSL certificate verification. True (verify), False (no verification), or path to CA bundle")
    cert: tuple[SerializablePath, SerializablePath] | SerializablePath | None = Field(
        default=None, description="SSL client certificate. Single file path or tuple of (cert_path, key_path)"
    )


class FunctionCall(BaseModel):
    function: FunctionName = Field(description="Name of the function to be called.")
    kwargs: dict[VariableName, Any] = Field(default_factory=dict, description="Function arguments.")


class Functions(RootModel):
    root: list[FunctionName | FunctionCall] = Field(default_factory=list)

    def __iter__(self):
        return iter(self.root)

    def __getitem__(self, item):
        return self.root[item]


class JsonBody(BaseModel):
    """JSON request body."""

    json: JsonValue = Field(description="JSON data to send")


class XmlBody(BaseModel):
    """XML request body."""

    xml: XMLSting = Field(description="XML content as string")


class FormBody(BaseModel):
    """Form-encoded request body."""

    form: dict[str, Any] = Field(description="Form data to be URL-encoded")


class RawBody(BaseModel):
    """Raw text request body."""

    raw: str = Field(description="Raw text content")


class FilesBody(BaseModel):
    """Multipart file upload request body."""

    files: dict[str, SerializablePath] = Field(description="Files to upload from file paths (e.g., '/path/to/file')")


# Union type for all possible body types
RequestBody = JsonBody | XmlBody | FormBody | RawBody | FilesBody


class Request(BaseModel):
    url: HttpUrl = Field()
    method: HTTPMethod = Field(default=HTTPMethod.GET)
    params: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: RequestBody | None = Field(default=None, description="Request body configuration")
    timeout: float = Field(default=30.0, description="Request timeout in seconds", gt=0)
    allow_redirects: bool = Field(default=True, description="Whether to follow redirects")
    ssl: SSLConfig = Field(
        default_factory=SSLConfig,
        description="SSL/TLS configuration for this specific request (overrides scenario SSL settings)",
        examples=[
            {"verify": True},
            {"verify": False},
            {"verify": "/path/to/ca-bundle.crt"},
            {"cert": "/path/to/client.pem"},
            {"cert": ["/path/to/client.crt", "/path/to/client.key"]},
        ],
    )
    auth: FunctionName | FunctionCall | None = Field(default=None, description="Authentication function name or function call (overrides scenario auth settings)")


class Save(BaseModel):
    vars: dict[str, JMESPathExpression] = Field(default_factory=dict, description="Dictionary of JMESPath expressions to extract the value from the response")
    functions: Functions = Field(default_factory=Functions, description="List of functions to be called to save data.")


class ResponseBody(BaseModel):
    schema: JSONSchemaInline | SerializablePath | None = Field(default=None, description="JSON schema or path to schema file")
    contains: list[str] = Field(default_factory=list, description="Substrings that must be present in the response body")
    not_contains: list[str] = Field(default_factory=list, description="Substrings that must NOT be present in the response body")
    matches: list[RegexPattern] = Field(default_factory=list, description="Regular expressions that must match the response body text")
    not_matches: list[RegexPattern] = Field(default_factory=list, description="Regular expressions that must NOT match the response body text")


class Verify(BaseModel):
    status: HTTPStatus | None = Field(default=None, description="Expected HTTP status code.")
    headers: dict[str, str] = Field(default_factory=dict, description="Expected response headers (case-insensitive).")
    vars: dict[str, Any] = Field(default_factory=dict, description="Expected values for variables.")
    functions: Functions = Field(default_factory=Functions, description="List of functions to be called to verify the response.")
    body: ResponseBody = Field(default_factory=ResponseBody, description="Response body validation (schema and regex patterns).")


class Response(BaseModel):
    save: Save = Field(default_factory=Save)
    verify: Verify = Field(default_factory=Verify)


class Decorated(BaseModel):
    marks: list[str] = Field(default_factory=list, description="List of marks to be applied to this stage", examples=["xfail", "skip"])
    vars: dict[str, Any] = Field(default_factory=dict, description="Initial variables for the stage context")


class StageCanvas(Decorated):
    name: str = Field()
    fixtures: list[str] = Field(default_factory=list, description="List of pytest fixture names for this stage")

    @model_validator(mode="after")
    def validate_variable_naming_conflicts(self) -> Self:
        """Validate that fixtures don't conflict with vars."""
        conflicting_vars = set(set(self.fixtures) & self.vars.keys())
        if len(conflicting_vars) > 0:
            var_names = ",".join(conflicting_vars)
            raise ValueError(f"Conflicting fixtures and vars: {var_names}")

        return self


class Stage(StageCanvas):
    always_run: bool = Field(default=False, description="Run this stage even if previous stages failed")
    request: Any = Field()
    response: Any = Field(default_factory=dict)


class Scenario(Decorated):
    ssl: SSLConfig = Field(
        default_factory=SSLConfig,
        description="SSL/TLS configuration applied to the HTTP session for all requests",
        examples=[
            {"verify": True},
            {"verify": False},
            {"verify": "/path/to/ca-bundle.crt"},
            {"cert": "/path/to/client.pem"},
            {"cert": ["/path/to/client.crt", "/path/to/client.key"]},
            {"verify": "/path/to/ca-bundle.crt", "cert": ["/path/to/client.crt", "/path/to/client.key"]},
        ],
    )
    auth: FunctionName | FunctionCall | None = Field(default=None, description="Authentication function name or function call applied to the HTTP session for all requests")
    stages: list[StageCanvas] = Field(default_factory=list, description="Stages collection")
