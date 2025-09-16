from http import HTTPMethod, HTTPStatus
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Discriminator, Field, JsonValue, PositiveFloat, RootModel, Tag, model_validator
from pydantic.networks import HttpUrl

from pytest_httpchain_models.types import (
    FunctionImportName,
    GraphQLQuery,
    JMESPathExpression,
    JSONSchemaInline,
    NamespaceFromDict,
    NamespaceOrDict,
    PartialTemplateStr,
    RegexPattern,
    SerializablePath,
    TemplateExpression,
    VariableName,
    XMLString,
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

# Annotated type for list of functions (used in response save/verify)
FunctionsList = Annotated[list[UserFunctionCall], Field(default_factory=list, description="Functions to process response data.")]

# Annotated type for dict of functions (used in substitutions)
FunctionsDict = Annotated[dict[str, UserFunctionCall], Field(default_factory=dict, description="User-defined functions for processing.")]


def get_request_body_discriminator(v: Any) -> str:
    """Discriminator function for request body types."""
    # For dict inputs, check which field is present
    if isinstance(v, dict):
        body_fields = {"json", "xml", "form", "raw", "files", "graphql"}
        found = body_fields & v.keys()
        if found:
            return found.pop()

    # For object inputs, map class name to discriminator
    if hasattr(v, "__class__"):
        class_to_tag = {"JsonBody": "json", "XmlBody": "xml", "FormBody": "form", "RawBody": "raw", "FilesBody": "files", "GraphQLBody": "graphql"}
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

    xml: XMLString | PartialTemplateStr = Field(description="XML content as string.")
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


class GraphQL(BaseModel):
    """GraphQL query with variables."""

    query: GraphQLQuery | PartialTemplateStr = Field(description="GraphQL query string.", examples=["query { user { id name } }", "{{ graphql_query }}"])
    variables: NamespaceOrDict | PartialTemplateStr = Field(default_factory=dict, description="GraphQL query variables.")
    model_config = ConfigDict(extra="forbid")


class GraphQLBody(BaseModel):
    """GraphQL request body."""

    graphql: GraphQL = Field(description="GraphQL query configuration.")
    model_config = ConfigDict(extra="forbid")


# Discriminated union with callable discriminator
RequestBody = Annotated[
    Annotated[JsonBody, Tag("json")]
    | Annotated[XmlBody, Tag("xml")]
    | Annotated[FormBody, Tag("form")]
    | Annotated[RawBody, Tag("raw")]
    | Annotated[FilesBody, Tag("files")]
    | Annotated[GraphQLBody, Tag("graphql")],
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
    functions: FunctionsList


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
    functions: FunctionsList
    body: ResponseBody = Field(default_factory=ResponseBody)


class Decorated(BaseModel):
    """Pytest test decoration configuration."""

    marks: list[str] = Field(default_factory=list, examples=["xfail", "skip"])
    fixtures: list[str] = Field(default_factory=list)


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
ResponseStep = Annotated[
    Annotated[SaveStep, Tag("save")] | Annotated[VerifyStep, Tag("verify")],
    Discriminator(get_response_step_discriminator),
]


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


class IndividualStep(BaseModel):
    """Individual parameter step that creates cartesian product."""

    individual: dict[str, Annotated[list[Any], Field(min_length=1)] | PartialTemplateStr] = Field(
        max_length=1, description="Parameter name mapped to list of values (single parameter per step, non-empty values) or template expression"
    )
    ids: list[str] | None = Field(default=None, description="Optional IDs for each value")

    @model_validator(mode="after")
    def validate_ids_match_values(self) -> Self:
        if self.ids and self.individual:
            values = next(iter(self.individual.values()))
            # Skip validation if values is a template string
            if isinstance(values, str):
                return self
            if len(self.ids) != len(values):
                raise ValueError(f"Number of ids ({len(self.ids)}) must match number of values ({len(values)})")
        return self


class CombinationsStep(BaseModel):
    """Explicit parameter combinations step."""

    combinations: list[Annotated[dict[str, Any], Field(min_length=1)]] | PartialTemplateStr = Field(
        description="List of parameter combinations (each dict must have at least one parameter) or template expression"
    )
    ids: list[str] | None = Field(default=None, description="Optional IDs for each combination")

    @model_validator(mode="after")
    def validate_combinations(self) -> Self:
        # Skip validation if combinations is a template string
        if isinstance(self.combinations, str):
            return self
        # Ensure all combinations have the same keys (if there are multiple)
        if len(self.combinations) > 1:
            first_keys = set(self.combinations[0].keys())
            for i, combo in enumerate(self.combinations[1:], 1):
                combo_keys = set(combo.keys())
                if combo_keys != first_keys:
                    raise ValueError(f"Combination {i} has different parameters than combination 0")

        # Validate ids match combinations count
        if self.ids and self.combinations:
            if len(self.ids) != len(self.combinations):
                raise ValueError(f"Number of ids ({len(self.ids)}) must match number of combinations ({len(self.combinations)})")
        return self


def get_parameter_step_discriminator(v: Any) -> str:
    """Discriminator function for parameter step types."""
    # For dict inputs, check which field is present
    if isinstance(v, dict):
        if "individual" in v:
            return "individual"
        elif "combinations" in v:
            return "combinations"

    # For object inputs, map class name to discriminator
    if hasattr(v, "__class__"):
        class_to_tag = {"IndividualStep": "individual", "CombinationsStep": "combinations"}
        tag = class_to_tag.get(v.__class__.__name__)
        if tag:
            return tag

    raise ValueError("Unable to determine parameter step type")


ParameterStep = Annotated[
    Annotated[IndividualStep, Tag("individual")] | Annotated[CombinationsStep, Tag("combinations")],
    Discriminator(get_parameter_step_discriminator),
]


class Parameters(RootModel):
    """List of parameter steps to be applied sequentially (creates cartesian product)."""

    root: list[ParameterStep] = Field(
        default_factory=list,
        description="Sequential parameter steps. Multiple steps create cartesian product.",
        examples=[
            [
                {"individual": {"method": ["GET", "POST"]}},
                {"individual": {"status": [200, 201]}},
            ],
            [{"combinations": [{"user": "alice", "role": "admin"}, {"user": "bob", "role": "user"}]}],
        ],
    )

    def __iter__(self):
        return iter(self.root)

    def __getitem__(self, item):
        return self.root[item]

    def __len__(self):
        return len(self.root)


class Stage(Decorated):
    """HTTP test stage configuration."""

    name: str = Field(description="Stage name (human-readable).")
    description: str | None = Field(default=None, description="Extended description for the test stage.")
    vars: dict[str, NamespaceFromDict] = Field(default_factory=dict)
    always_run: Literal[True, False] | TemplateExpression = Field(default=False, examples=[True, "{{ should_run }}", "{{ env == 'production' }}"])
    parameters: Parameters | None = Field(default=None, description="Stage parametrization steps")
    request: Request = Field(description="HTTP request details.")
    response: Response = Field(default_factory=Response)


class Substitution(BaseModel):
    """Single variable substitution step."""

    vars: dict[str, NamespaceFromDict] = Field(default_factory=dict, description="Variables for substitution.")
    functions: FunctionsDict


# Type alias for list of substitution steps
Substitutions = Annotated[
    list[Substitution],
    Field(
        default_factory=list,
        description="Sequential substitution steps to apply.",
        examples=[
            [
                {"vars": {"base_url": "https://api.example.com"}},
                {"vars": {"endpoint": "/users"}, "functions": {"auth_token": "auth:get_token"}},
            ],
        ],
    ),
]


class Scenario(Decorated, CallSecurity):
    """HTTP test scenario with multiple stages."""

    stages: list[Stage] = Field(default_factory=list)
    substitutions: Substitutions = Field(default_factory=Substitutions, description="Variable substitution configuration.")
