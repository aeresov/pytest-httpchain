"""The scenario models.

Every model is validated twice: at collection, while ``{{ }}`` strings are still
unrendered, and again after the engine renders them, when the concrete value is
finally checked against the real type. Hence the ``concrete | template`` unions
— the template branch is what keeps phase one from rejecting an unrendered
value.
"""

import warnings
from collections.abc import Callable
from contextlib import contextmanager
from http import HTTPMethod, HTTPStatus
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Discriminator, Field, JsonValue, PositiveFloat, PositiveInt, RootModel, Tag, model_validator
from pydantic.networks import HttpUrl

from pytest_httpchain.models.types import (
    Base64String,
    FunctionImportName,
    GraphQLQuery,
    HttpMethodToken,
    JMESPathExpression,
    JSONSchemaInline,
    NamespaceFromDict,
    NamespaceOrDict,
    NumberOrTemplate,
    PartialTemplateStr,
    RegexPattern,
    SerializablePath,
    StatusCode,
    TemplateExpression,
    TemplateExpressionOnly,
    VariableName,
    XMLString,
    convert_namespace_to_dict,
)
from pytest_httpchain.templates import contains_template


def _create_discriminator(class_to_tag: dict[type, str]) -> Callable[[Any], str]:
    """Build a discriminator from a ``{model class: tag}`` mapping (keyed by
    class, so a rename breaks statically rather than at runtime).

    Unrecognized input yields an invalid tag rather than raising, so pydantic
    reports a located ``union_tag_invalid`` error listing the valid tags — which
    every caller's ``except ValidationError`` already handles.
    """
    tag_fields = set(class_to_tag.values())

    def discriminator(v: Any) -> str:
        if isinstance(v, dict):
            found = tag_fields & v.keys()
            if found:
                # Several tag keys: pick deterministically and let the chosen
                # variant reject the surplus under extra="forbid".
                return min(found)

            # Name the offending key, so the validation error points at it.
            return next(iter(v), "(empty object)")  # ty: ignore[invalid-return-type]

        tag = class_to_tag.get(type(v))
        if tag:
            return tag

        return f"(non-object: {type(v).__name__})"

    return discriminator


@contextmanager
def _suppress_field_shadow_warning(field_name: str):
    """Silence pydantic's shadowed-attribute warning for the one class statement
    it wraps: "json" and "schema" are intentional domain names."""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=rf'Field name "{field_name}" in ".*" shadows an attribute',
            category=UserWarning,
        )
        yield


def _normalize_list_input(v: Any) -> Any:
    """Flatten the name-keyed mapping form into a list, preserving order:
    ``{"x": [a, b], "y": c}`` -> ``[a, b, c]``. Anything else passes through."""
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


def _normalize_stages_input(v: Any) -> Any:
    """Flatten the ``{name: stage}`` form into a list, the key becoming (and
    overriding) each stage's ``name``."""
    if isinstance(v, list):
        return v

    if isinstance(v, dict):
        result = []
        for name, stage_data in v.items():
            result.append({**stage_data, "name": name} if isinstance(stage_data, dict) else stage_data)
        return result

    return v


class StrictModel(BaseModel):
    """Base for all scenario models: unknown keys are rejected, so a typo fails
    validation instead of silently changing behavior.

    The exception is "$schema" (editor metadata), dropped before validation.
    Inside plain dict values — an inline JSON Schema — it is untouched.
    """

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _drop_schema_key(cls, data: Any) -> Any:
        if isinstance(data, dict) and "$schema" in data:
            return {k: v for k, v in data.items() if k != "$schema"}
        return data


class SSLConfig(StrictModel):
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


class UserFunctionKwargs(StrictModel):
    name: UserFunctionName
    kwargs: dict[VariableName, Any] = Field(default_factory=dict, description="Function arguments.")


UserFunctionCall = UserFunctionName | UserFunctionKwargs

FunctionsList = list[UserFunctionCall]

# Keys are the aliases under which functions are exposed to templates, so they
# must be valid Python identifiers (a non-identifier key could never be
# referenced inside a {{ }} expression).
FunctionsDict = dict[VariableName, UserFunctionCall]


class Descripted(StrictModel):
    description: str | None = Field(default=None, description="Optional description for this component")


class Marked(StrictModel):
    marks: list[str] = Field(default_factory=list, examples=[["xfail"], ["skip", "slow"]], description="pytest markers")


class Fixtured(StrictModel):
    fixtures: list[str] = Field(default_factory=list, description="pytest fixtures")


class Authenticated(StrictModel):
    auth: UserFunctionCall | None = Field(
        default=None,
        description="User function to create custom authentication.",
    )


with _suppress_field_shadow_warning("json"):

    class JsonBody(StrictModel):
        json: Annotated[JsonValue, BeforeValidator(convert_namespace_to_dict)] = Field(description="JSON data to send.")


class XmlBody(StrictModel):
    xml: XMLString | PartialTemplateStr = Field(description="XML content as string.")


class FormBody(StrictModel):
    form: dict[str, Any] = Field(description="Form data to be URL-encoded.")


class TextBody(StrictModel):
    text: str | PartialTemplateStr = Field(description="Raw text content.")


class Base64Body(StrictModel):
    base64: Base64String | PartialTemplateStr = Field(description="Base64-encoded binary data or template expression.")


class BinaryBody(StrictModel):
    binary: SerializablePath | PartialTemplateStr = Field(description="Path to binary file.")


class FilesBody(StrictModel):
    files: dict[str, SerializablePath | PartialTemplateStr] = Field(description="Files to upload from file paths.")


class GraphQL(StrictModel):
    query: GraphQLQuery | PartialTemplateStr = Field(description="GraphQL query string.", examples=["query { user { id name } }", "{{ graphql_query }}"])
    variables: NamespaceOrDict | PartialTemplateStr = Field(default_factory=dict, description="GraphQL query variables.")


class GraphQLBody(StrictModel):
    graphql: GraphQL = Field(description="GraphQL query configuration.")


get_request_body_discriminator = _create_discriminator(
    {
        JsonBody: "json",
        XmlBody: "xml",
        FormBody: "form",
        TextBody: "text",
        Base64Body: "base64",
        BinaryBody: "binary",
        FilesBody: "files",
        GraphQLBody: "graphql",
    },
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
    url: HttpUrl | PartialTemplateStr = Field(description="Request URL (may be a template expression).")
    method: HTTPMethod | HttpMethodToken | TemplateExpressionOnly = Field(
        default=HTTPMethod.GET,
        description="HTTP method: a standard verb (autocompleted) or any RFC 9110 token (e.g. PROPFIND, PURGE).",
    )
    params: dict[str, Any] = Field(default_factory=dict, description="URL query parameters.")
    headers: dict[str, str] = Field(default_factory=dict, description="HTTP request headers.")
    body: RequestBody | None = Field(default=None, description="Request body configuration.")
    timeout: PositiveFloat | NumberOrTemplate = Field(default=30.0, description="Request timeout in seconds.")
    allow_redirects: Literal[True, False] | TemplateExpressionOnly = Field(default=True, description="Whether to follow redirects.")


class VarsSubstitution(Descripted):
    vars: dict[VariableName, NamespaceFromDict] = Field(description="Variables for substitution.")


class FunctionsSubstitution(Descripted):
    functions: FunctionsDict = Field(description="User-defined functions.")


get_substitution_discriminator = _create_discriminator(
    {
        VarsSubstitution: "vars",
        FunctionsSubstitution: "functions",
    },
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

    jmespath: dict[VariableName, JMESPathExpression | PartialTemplateStr] = Field(description="JMESPath expressions to extract values from response.")


class SubstitutionsSave(Descripted):
    """Save data using variable substitutions."""

    substitutions: Substitutions = Field(description="Variable substitution configuration.")


class UserFunctionsSave(Descripted):
    """Save data using user-defined functions to process response data."""

    user_functions: FunctionsList = Field(description="Functions to process response data.")


get_save_discriminator = _create_discriminator(
    {
        JMESPathSave: "jmespath",
        SubstitutionsSave: "substitutions",
        UserFunctionsSave: "user_functions",
    },
)


Save = Annotated[
    Annotated[JMESPathSave, Tag("jmespath")] | Annotated[SubstitutionsSave, Tag("substitutions")] | Annotated[UserFunctionsSave, Tag("user_functions")],
    Discriminator(get_save_discriminator),
]


with _suppress_field_shadow_warning("schema"):

    class ResponseBody(StrictModel):
        schema: JSONSchemaInline | SerializablePath | PartialTemplateStr | None = Field(default=None, description="JSON schema for validation.")
        contains: list[str] = Field(default_factory=list, description="Substrings the response body must contain.")
        not_contains: list[str] = Field(default_factory=list, description="Substrings the response body must NOT contain.")
        matches: list[RegexPattern] = Field(default_factory=list, description="Regex patterns the response body must match.")
        not_matches: list[RegexPattern] = Field(default_factory=list, description="Regex patterns the response body must NOT match.")


class HeaderMatcher(StrictModel):
    """Matcher for one expected response header; at least one field must be set.
    An absent header behaves as an empty string, as bodies do."""

    contains: str | PartialTemplateStr | None = Field(default=None, description="Substring the header value must contain.")
    not_contains: str | PartialTemplateStr | None = Field(default=None, description="Substring the header value must NOT contain.")
    matches: RegexPattern | PartialTemplateStr | None = Field(default=None, description="Regex the header value must match (re.search).")
    not_matches: RegexPattern | PartialTemplateStr | None = Field(default=None, description="Regex the header value must NOT match (re.search).")

    @model_validator(mode="after")
    def at_least_one_check(self) -> Self:
        if self.contains is None and self.not_contains is None and self.matches is None and self.not_matches is None:
            raise ValueError("Header matcher must set at least one of: contains, not_contains, matches, not_matches")
        return self


class Verify(Descripted):
    status: HTTPStatus | StatusCode | None | NumberOrTemplate = Field(
        default=None,
        description="Expected HTTP status code: a standard code (autocompleted) or any integer 100-599 (e.g. 499).",
    )
    headers: dict[str, str | HeaderMatcher] = Field(
        default_factory=dict,
        description="Expected response headers: a string (exact match) or a matcher object (contains/not_contains/matches/not_matches) per key.",
    )
    expressions: list[Any] = Field(
        default_factory=list,
        description=(
            "Template expressions evaluated as boolean conditions against the context "
            "(saved variables, fixtures, substitutions). Each must be a full template "
            "expression that evaluates to a truthy/falsy value. Response metadata is "
            "available as `response.*`: status, reason, headers, elapsed_ms."
        ),
        examples=[["{{ user_age >= 18 }}", "{{ response.status == 200 }}", "{{ 'json' in response.headers['content-type'] }}"]],
    )
    user_functions: FunctionsList = Field(default_factory=list, description="Functions to process response data.")
    body: ResponseBody = Field(default_factory=ResponseBody)


class SaveStep(StrictModel):
    """Save data from HTTP response."""

    save: Save = Field(description="Save configuration.")


class VerifyStep(StrictModel):
    """Verify HTTP response and data context."""

    verify: Verify = Field(description="Verify configuration.")


get_response_step_discriminator = _create_discriminator(
    {SaveStep: "save", VerifyStep: "verify"},
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


class IndividualParameter(StrictModel):
    individual: Annotated[
        dict[str, Annotated[list[Any], Field(min_length=1)] | PartialTemplateStr],
        Field(min_length=1, max_length=1),
    ] = Field(description="Parameter name mapped to list of values (single parameter per step, non-empty values) or template expression")
    ids: list[str] | None = Field(default=None, description="Optional IDs for each value")

    @model_validator(mode="after")
    def validate_ids_match_values(self) -> Self:
        if self.ids and self.individual:
            values = next(iter(self.individual.values()))
            if isinstance(values, str):  # template form: count unknown until runtime
                return self
            if len(self.ids) != len(values):
                raise ValueError(f"Number of ids ({len(self.ids)}) must match number of values ({len(values)})")
        return self


class CombinationsParameter(StrictModel):
    combinations: Annotated[list[Annotated[dict[str, Any], Field(min_length=1)]], Field(min_length=1)] | PartialTemplateStr = Field(
        description="Non-empty list of parameter combinations (each dict must have at least one parameter) or template expression"
    )
    ids: list[str] | None = Field(default=None, description="Optional IDs for each combination")

    @model_validator(mode="after")
    def validate_combinations(self) -> Self:
        if isinstance(self.combinations, str):  # template form: keys unknown until runtime
            return self
        if len(self.combinations) > 1:
            first_keys = set(self.combinations[0].keys())
            for i, combo in enumerate(self.combinations[1:], 1):
                combo_keys = set(combo.keys())
                if combo_keys != first_keys:
                    raise ValueError(f"Combination {i} has different parameters than combination 0")

        if self.ids and self.combinations:
            if len(self.ids) != len(self.combinations):
                raise ValueError(f"Number of ids ({len(self.ids)}) must match number of combinations ({len(self.combinations)})")
        return self


get_parameter_step_discriminator = _create_discriminator(
    {IndividualParameter: "individual", CombinationsParameter: "combinations"},
)


Parameter = Annotated[
    Annotated[IndividualParameter, Tag("individual")] | Annotated[CombinationsParameter, Tag("combinations")],
    Discriminator(get_parameter_step_discriminator),
]


Parameters = list[Parameter]


def parametrize_values_contain_template(parametrize: Parameters | None) -> bool:
    """True if any parametrize VALUE holds a ``{{ }}`` template — ``ids`` are
    never rendered, so a template-looking string there must not count.

    Shared by the factory (which must then resolve at collection time) and the
    validator (which reports that as HTTPCHAIN025), so the two agree.
    """
    for step in parametrize or []:
        match step:
            case IndividualParameter(individual=individual):
                if contains_template(individual):
                    return True
            case CombinationsParameter(combinations=combinations):
                if contains_template(combinations):
                    return True
    return False


class ParallelConfigBase(StrictModel):
    """Base configuration for parallel HTTP request execution."""

    max_concurrency: PositiveInt | NumberOrTemplate = Field(
        default=10,
        description="Maximum number of concurrent requests.",
    )
    calls_per_sec: PositiveInt | NumberOrTemplate | None = Field(
        default=None,
        description=(
            "Maximum number of API calls per second, shared by this stage's concurrent iterations. "
            "The limiter is per stage execution and per process: consecutive stages, other scenarios, "
            "and pytest-xdist workers each get their own budget."
        ),
    )
    max_rate_limit_delay: PositiveInt | NumberOrTemplate = Field(
        default=60,
        description="Maximum seconds to wait when rate-limited before giving up. Defaults to 60 seconds.",
    )


class ParallelRepeatConfig(ParallelConfigBase):
    """Execute the same request N times in parallel."""

    repeat: PositiveInt | NumberOrTemplate = Field(
        description="Execute the same request N times in parallel.",
    )


class ParallelForeachConfig(ParallelConfigBase):
    """Execute request once for each parameter set in parallel."""

    foreach: Annotated[Parameters, Field(min_length=1)] = Field(
        description="Execute request once for each parameter set in parallel.",
    )


get_parallel_config_discriminator = _create_discriminator(
    {
        ParallelRepeatConfig: "repeat",
        ParallelForeachConfig: "foreach",
    },
)


ParallelConfig = Annotated[
    Annotated[ParallelRepeatConfig, Tag("repeat")] | Annotated[ParallelForeachConfig, Tag("foreach")],
    Discriminator(get_parallel_config_discriminator),
]


class Stage(Marked, Fixtured, Descripted):
    name: str = Field(default="", description="Stage name (human-readable).")
    substitutions: Substitutions = Field(default_factory=list, description="Variable substitution configuration.")
    always_run: Literal[True, False] | TemplateExpressionOnly = Field(
        default=False,
        description="Execute even if a previous stage failed. A template expression is evaluated (truthiness) when the chain is aborted, "
        "against fixtures, parametrize parameters, scenario substitutions, and previously saved variables.",
        examples=[True, "{{ should_run }}", "{{ env == 'production' }}"],
    )
    parametrize: Parameters | None = Field(default=None, description="Stage parametrization steps")
    parallel: ParallelConfig | None = Field(default=None, description="Parallel execution configuration for load/stress testing.")
    request: Request = Field(description="HTTP request details.")
    response: Responses = Field(default_factory=list, description="Sequential steps to process the response.")


Stages = Annotated[
    list[Stage],
    BeforeValidator(_normalize_stages_input, json_schema_input_type=list[Stage] | dict[str, Stage]),
]


class Scenario(Marked, Fixtured, Authenticated, Descripted):
    fixtures: list[str] = Field(default_factory=list, description="pytest fixtures available to all stages")
    ssl: SSLConfig = Field(
        default_factory=SSLConfig,
        description="SSL/TLS configuration.",
    )
    stages: Stages = Field(default_factory=list, description="Ordered list (or name-keyed mapping) of stages to execute.")
    substitutions: Substitutions = Field(default_factory=list, description="Variable substitution configuration.")
