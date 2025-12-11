import warnings
from collections.abc import Callable
from http import HTTPMethod, HTTPStatus
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Discriminator, Field, JsonValue, PositiveFloat, PositiveInt, RootModel, Tag, model_validator
from pydantic.networks import HttpUrl

from pytest_httpchain_models.types import (
    Base64String,
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


def _create_discriminator(class_to_tag: dict[str, str], error_message: str) -> Callable[[Any], str]:
    """Factory function to create Pydantic discriminator functions.

    Args:
        class_to_tag: Mapping from class names to discriminator tags.
        error_message: Error message to raise when type cannot be determined.

    Returns:
        A discriminator function that can be used with Pydantic's Discriminator.
    """
    tag_fields = set(class_to_tag.values())

    def discriminator(v: Any) -> str:
        if isinstance(v, dict):
            found = tag_fields & v.keys()
            if found:
                return found.pop()

        if hasattr(v, "__class__"):
            tag = class_to_tag.get(v.__class__.__name__)
            if tag:
                return tag

        raise ValueError(error_message)

    return discriminator


# Suppress Pydantic warnings about field names shadowing BaseModel attributes.
# Fields "json" and "schema" are intentional domain-specific names.
warnings.filterwarnings("ignore", message=r'Field name "json" in "JsonBody" shadows an attribute', category=UserWarning)
warnings.filterwarnings("ignore", message=r'Field name "schema" in "ResponseBody" shadows an attribute', category=UserWarning)


def _normalize_list_input(v: Any) -> list[Any]:
    """Normalize list-or-dict input to a flat list.

    Accepts:
    - list: returned as-is
    - dict: values flattened (list values extended, others appended)
    - other: passed through for Pydantic to handle

    Examples:
        [a, b] -> [a, b]
        {"x": a, "y": b} -> [a, b]
        {"x": [a, b], "y": c} -> [a, b, c]
    """
    if isinstance(v, list):
        return v

    if isinstance(v, dict):
        result = []
        for value in v.values():
            if isinstance(value, list):
                result.extend(value)
            else:
                result.append(value)
        return result

    return v


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
    name: UserFunctionName
    kwargs: dict[VariableName, Any] = Field(default_factory=dict, description="Function arguments.")


UserFunctionCall = UserFunctionName | UserFunctionKwargs

FunctionsList = list[UserFunctionCall]

FunctionsDict = dict[str, UserFunctionCall]


class Descripted(BaseModel):
    description: str | None = Field(default=None, description="Optional description for this component")


class Marked(BaseModel):
    marks: list[str] = Field(default_factory=list, examples=["xfail", "skip"], description="pytest markers")


class Fixtured(BaseModel):
    fixtures: list[str] = Field(default_factory=list, description="pytest fixtures")


class Authenticated(BaseModel):
    auth: UserFunctionCall | None = Field(
        default=None,
        description="User function to create custom authentication.",
    )


class JsonBody(BaseModel):
    json: JsonValue = Field(description="JSON data to send.")
    model_config = ConfigDict(extra="forbid")


class XmlBody(BaseModel):
    xml: XMLString | PartialTemplateStr = Field(description="XML content as string.")
    model_config = ConfigDict(extra="forbid")


class FormBody(BaseModel):
    form: dict[str, Any] = Field(description="Form data to be URL-encoded.")
    model_config = ConfigDict(extra="forbid")


class TextBody(BaseModel):
    text: str | PartialTemplateStr = Field(description="Raw text content.")
    model_config = ConfigDict(extra="forbid")


class Base64Body(BaseModel):
    base64: Base64String | PartialTemplateStr = Field(description="Base64-encoded binary data or template expression.")
    model_config = ConfigDict(extra="forbid")


class BinaryBody(BaseModel):
    binary: SerializablePath | PartialTemplateStr = Field(description="Path to binary file.")
    model_config = ConfigDict(extra="forbid")


class FilesBody(BaseModel):
    files: dict[str, SerializablePath | PartialTemplateStr] = Field(description="Files to upload from file paths.")
    model_config = ConfigDict(extra="forbid")


class GraphQL(BaseModel):
    query: GraphQLQuery | PartialTemplateStr = Field(description="GraphQL query string.", examples=["query { user { id name } }", "{{ graphql_query }}"])
    variables: NamespaceOrDict | PartialTemplateStr = Field(default_factory=dict, description="GraphQL query variables.")
    model_config = ConfigDict(extra="forbid")


class GraphQLBody(BaseModel):
    graphql: GraphQL = Field(description="GraphQL query configuration.")
    model_config = ConfigDict(extra="forbid")


get_request_body_discriminator = _create_discriminator(
    {
        "JsonBody": "json",
        "XmlBody": "xml",
        "FormBody": "form",
        "TextBody": "text",
        "Base64Body": "base64",
        "BinaryBody": "binary",
        "FilesBody": "files",
        "GraphQLBody": "graphql",
    },
    "Unable to determine body type",
)


RequestBody = Annotated[
    Annotated[JsonBody, Tag("json")]
    | Annotated[XmlBody, Tag("xml")]
    | Annotated[FormBody, Tag("form")]
    | Annotated[TextBody, Tag("text")]
    | Annotated[Base64Body, Tag("base64")]
    | Annotated[BinaryBody, Tag("binary")]
    | Annotated[FilesBody, Tag("files")]
    | Annotated[GraphQLBody, Tag("graphql")],
    Discriminator(get_request_body_discriminator),
]


class Request(Authenticated):
    url: HttpUrl | PartialTemplateStr = Field()
    method: HTTPMethod | TemplateExpression = Field(default=HTTPMethod.GET)
    params: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: RequestBody | None = Field(default=None, description="Request body configuration.")
    timeout: PositiveFloat | TemplateExpression = Field(default=30.0, description="Request timeout in seconds.")
    allow_redirects: Literal[True, False] | TemplateExpression = Field(default=True, description="Whether to follow redirects.")


class VarsSubstitution(Descripted):
    vars: dict[str, NamespaceFromDict] = Field(description="Variables for substitution.")
    model_config = ConfigDict(extra="forbid")


class FunctionsSubstitution(Descripted):
    functions: FunctionsDict = Field(description="User-defined functions.")
    model_config = ConfigDict(extra="forbid")


get_substitution_discriminator = _create_discriminator(
    {
        "VarsSubstitution": "vars",
        "FunctionsSubstitution": "functions",
    },
    "Unable to determine substitution type",
)


Substitution = Annotated[
    Annotated[VarsSubstitution, Tag("vars")] | Annotated[FunctionsSubstitution, Tag("functions")],
    Discriminator(get_substitution_discriminator),
]

# Input type unions representing all accepted formats for flexible validation
SubstitutionsInput = list[Substitution] | dict[str, Substitution | list[Substitution]]

Substitutions = Annotated[
    list[Substitution],
    BeforeValidator(_normalize_list_input, json_schema_input_type=SubstitutionsInput),
]


class JMESPathSave(Descripted):
    """Save data using JMESPath expressions to extract values from response."""

    jmespath: dict[str, JMESPathExpression | PartialTemplateStr] = Field(description="JMESPath expressions to extract values from response.")
    model_config = ConfigDict(extra="forbid")


class SubstitutionsSave(Descripted):
    """Save data using variable substitutions."""

    substitutions: Substitutions = Field(description="Variable substitution configuration.")
    model_config = ConfigDict(extra="forbid")


class UserFunctionsSave(Descripted):
    """Save data using user-defined functions to process response data."""

    user_functions: FunctionsList = Field(description="Functions to process response data.")
    model_config = ConfigDict(extra="forbid")


get_save_discriminator = _create_discriminator(
    {
        "JMESPathSave": "jmespath",
        "SubstitutionsSave": "substitutions",
        "UserFunctionsSave": "user_functions",
    },
    "Unable to determine save type: must have 'jmespath', 'substitutions', or 'user_functions'",
)


Save = Annotated[
    Annotated[JMESPathSave, Tag("jmespath")] | Annotated[SubstitutionsSave, Tag("substitutions")] | Annotated[UserFunctionsSave, Tag("user_functions")],
    Discriminator(get_save_discriminator),
]


class ResponseBody(BaseModel):
    schema: JSONSchemaInline | SerializablePath | PartialTemplateStr | None = Field(default=None, description="JSON schema for validation.")
    contains: list[str] = Field(default_factory=list)
    not_contains: list[str] = Field(default_factory=list)
    matches: list[RegexPattern] = Field(default_factory=list)
    not_matches: list[RegexPattern] = Field(default_factory=list)


class Verify(Descripted):
    status: HTTPStatus | None | TemplateExpression = Field(default=None)
    headers: dict[str, str] = Field(default_factory=dict)
    expressions: list[Any | TemplateExpression] = Field(
        default_factory=list,
        description="Template expressions to evaluate as boolean conditions. Each must be a full template expression that evaluates to a truthy/falsy value.",
        examples=[["{{ user_age >= 18 }}", "{{ status_code == 200 }}", "{{ 'error' not in response_text }}"]],
    )
    user_functions: FunctionsList = Field(default_factory=list, description="Functions to process response data.")
    body: ResponseBody = Field(default_factory=ResponseBody)


class SaveStep(BaseModel):
    """Save data from HTTP response."""

    save: Save = Field(description="Save configuration.")
    model_config = ConfigDict(extra="forbid")


class VerifyStep(BaseModel):
    """Verify HTTP response and data context."""

    verify: Verify = Field(description="Verify configuration.")
    model_config = ConfigDict(extra="forbid")


get_response_step_discriminator = _create_discriminator(
    {"SaveStep": "save", "VerifyStep": "verify"},
    "Unable to determine step type",
)


ResponseStep = Annotated[
    Annotated[SaveStep, Tag("save")] | Annotated[VerifyStep, Tag("verify")],
    Discriminator(get_response_step_discriminator),
]

# Input type union for Responses - accepts both list and dict formats
ResponsesInput = list[ResponseStep] | dict[str, ResponseStep | list[ResponseStep]]

Responses = Annotated[
    list[ResponseStep],
    BeforeValidator(_normalize_list_input, json_schema_input_type=ResponsesInput),
]


class IndividualParameter(BaseModel):
    individual: dict[str, Annotated[list[Any], Field(min_length=1)] | PartialTemplateStr] = Field(
        description="Parameter name mapped to list of values (single parameter per step, non-empty values) or template expression"
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


class CombinationsParameter(BaseModel):
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


get_parameter_step_discriminator = _create_discriminator(
    {"IndividualParameter": "individual", "CombinationsParameter": "combinations"},
    "Unable to determine parameter step type",
)


Parameter = Annotated[
    Annotated[IndividualParameter, Tag("individual")] | Annotated[CombinationsParameter, Tag("combinations")],
    Discriminator(get_parameter_step_discriminator),
]


Parameters = list[Parameter]


class ParallelConfigBase(BaseModel):
    """Base configuration for parallel HTTP request execution."""

    max_concurrency: PositiveInt | TemplateExpression = Field(
        default=10,
        description="Maximum number of concurrent requests.",
    )
    calls_per_sec: PositiveInt | TemplateExpression | None = Field(
        default=None,
        description="Maximum number of API calls per second. When set, requests are rate-limited globally across all workers.",
    )


class ParallelRepeatConfig(ParallelConfigBase):
    """Execute the same request N times in parallel."""

    repeat: PositiveInt | TemplateExpression = Field(
        description="Execute the same request N times in parallel.",
    )
    model_config = ConfigDict(extra="forbid")


class ParallelForeachConfig(ParallelConfigBase):
    """Execute request once for each parameter set in parallel."""

    foreach: Parameters = Field(
        description="Execute request once for each parameter set in parallel.",
    )
    model_config = ConfigDict(extra="forbid")


get_parallel_config_discriminator = _create_discriminator(
    {
        "ParallelRepeatConfig": "repeat",
        "ParallelForeachConfig": "foreach",
    },
    "Unable to determine parallel config type: must have 'repeat' or 'foreach'",
)


ParallelConfig = Annotated[
    Annotated[ParallelRepeatConfig, Tag("repeat")] | Annotated[ParallelForeachConfig, Tag("foreach")],
    Discriminator(get_parallel_config_discriminator),
]


class Stage(Marked, Fixtured, Descripted):
    name: str = Field(description="Stage name (human-readable).")
    substitutions: Substitutions = Field(default_factory=list, description="Variable substitution configuration.")
    always_run: Literal[True, False] | TemplateExpression = Field(default=False, examples=[True, "{{ should_run }}", "{{ env == 'production' }}"])
    parametrize: Parameters | None = Field(default=None, description="Stage parametrization steps")
    parallel: ParallelConfig | None = Field(default=None, description="Parallel execution configuration for load/stress testing.")
    request: Request = Field(description="HTTP request details.")
    response: Responses = Field(default_factory=list, description="Sequential steps to process the response.")


class Scenario(Marked, Authenticated, Descripted):
    ssl: SSLConfig = Field(
        default_factory=SSLConfig,
        description="SSL/TLS configuration.",
    )
    stages: list[Stage] = Field(default_factory=list)
    substitutions: Substitutions = Field(default_factory=list, description="Variable substitution configuration.")
