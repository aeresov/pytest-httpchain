"""The scenario models.

Every model is validated twice: at collection, while ``{{ }}`` strings are still
unrendered, and again after the engine renders them, when the concrete value is
finally checked against the real type. Hence the ``concrete | template`` unions
— the template branch is what keeps phase one from rejecting an unrendered
value.
"""

import re
import warnings
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPMethod, HTTPStatus
from types import SimpleNamespace
from typing import Annotated, Any, ClassVar, Literal, LiteralString, Self, cast

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    JsonValue,
    PositiveFloat,
    PositiveInt,
    RootModel,
    Tag,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    ValidatorFunctionWrapHandler,
    WithJsonSchema,
    model_validator,
)
from pydantic.json_schema import JsonDict
from pydantic_core import InitErrorDetails, PydanticCustomError

from pytest_httpchain.models.types import (
    RENDERED,
    Base64String,
    BaseUrlStr,
    FunctionImportName,
    GraphQLQuery,
    HttpMethodToken,
    HttpUrlReferenceStr,
    JMESPathExpression,
    JMESPathKey,
    JsonLength,
    JsonNumber,
    JSONSchemaInline,
    JsonTypeName,
    MultipartFieldValue,
    NamespaceFromDict,
    NamespaceOrDict,
    NumberOrTemplate,
    PartContentType,
    PartialTemplateStr,
    ProxyUrlStr,
    RegexGroupName,
    RegexGroupNumber,
    RegexPattern,
    SchemaFileRefStr,
    SerializablePath,
    StatusClass,
    StatusCode,
    TemplateExpression,
    TemplateExpressionOnly,
    TemplateExpressionSchema,
    UnquotedPartialTemplateStr,
    VariableName,
    XMLString,
    as_rendered,
    convert_namespace_items_to_dict,
    convert_namespace_to_dict,
    refuse_escaped_braces,
    regex_group,
    validate_function_import_name,
    validate_unquoted_partial_template_str,
)
from pytest_httpchain.templates import contains_template


def _create_discriminator(class_to_tag: dict[type, str], value_to_tag: tuple[tuple[object, str], ...] = ()) -> Callable[[Any], str]:
    """Build a discriminator from a ``{model class: tag}`` mapping (keyed by
    class, so a rename breaks statically rather than at runtime), and
    ``(value, tag)`` pairs for a member that is one constant, told by identity
    (``False`` is not ``0``, and not ``True`` either).

    Unrecognized input yields an invalid tag rather than raising, so pydantic
    reports a located ``union_tag_invalid`` error listing the valid tags — which
    every caller's ``except ValidationError`` already handles.
    """
    tag_fields = set(class_to_tag.values())

    def discriminator(v: Any) -> str:
        for value, tag in value_to_tag:
            if v is value:
                return tag

        if isinstance(v, dict):
            found = tag_fields & v.keys()
            if found:
                # Several tag keys: pick deterministically and let the chosen
                # variant reject the surplus under extra="forbid".
                return min(found)

            # Name the offending key, so the validation error points at it.
            return next(iter(v), "(empty object)")

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


def normalize_list_input(v: Any) -> Any:
    """Flatten the name-keyed mapping form into a list, preserving order:
    ``{"x": [a, b], "y": c}`` -> ``[a, b, c]``. Anything else passes through.

    Shared, not private: raw-JSON readers in ``scoping`` index their entries
    positionally against the validated models, so the two must derive the list
    the same way.
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


def _normalize_stages_input(v: Any) -> Any:
    """Flatten the ``{name: stage}`` form into a list, the key becoming (and
    overriding) each stage's ``name``."""
    if isinstance(v, list):
        return v

    if isinstance(v, dict):
        result = []
        for name, stage_data in v.items():
            if isinstance(stage_data, dict):
                stage_data = {**stage_data, "name": name}
            elif isinstance(stage_data, Stage):
                stage_data = stage_data.model_copy(update={"name": name})
            result.append(stage_data)
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


class ClientConfig(StrictModel):
    """Settings of the HTTP client all the scenario's stages share. What a stage
    sets in its own request (url, headers, params, timeout, allow_redirects)
    wins over them."""

    # The docstring is the schema's description, for editors. A stage's timeout
    # and allow_redirects win only where it declares them (`model_fields_set`,
    # see request_builder): their model defaults would otherwise hide these.

    # Credentials live here (headers, params, the URLs' userinfo), typically
    # rendered from the environment once per scenario: a value that then fails
    # validation must not be printed, in the scenario's initialization failure
    # or in every later stage's skip reason, which repeats it. The URL fields'
    # own messages do not quote it either (`validate_proxy_url`).
    model_config = ConfigDict(hide_input_in_errors=True)

    base_url: Annotated[BaseUrlStr | UnquotedPartialTemplateStr | None, BeforeValidator(refuse_escaped_braces("base_url"))] = Field(
        default=None,
        description="Absolute http(s) URL without a query or fragment. A relative request.url is appended to its path.",
        examples=["https://api.example.com/v1", "{{ api_root }}"],
    )
    headers: dict[str, str] = Field(default_factory=dict, description="Headers sent with every request; a stage's request.headers override them by name, case-insensitively.")
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Query parameters sent with every request, unless its URL or request.params already sets the key.",
    )
    timeout: PositiveFloat | NumberOrTemplate = Field(default=30.0, description="Timeout in seconds for requests that do not set request.timeout.")
    follow_redirects: Literal[True, False] | TemplateExpressionOnly = Field(default=True, description="Whether requests that do not set request.allow_redirects follow redirects.")
    max_redirects: PositiveInt | NumberOrTemplate = Field(default=20, description="Redirects a request follows at most before it fails.")
    proxy: Annotated[ProxyUrlStr | UnquotedPartialTemplateStr | None, BeforeValidator(refuse_escaped_braces("proxy"))] = Field(
        default=None,
        description="Proxy for every request (http, https, socks5 or socks5h URL); replaces the proxy environment variables.",
        examples=["http://proxy.example.com:8080", "{{ proxy_url }}"],
    )
    http2: Literal[True, False] | TemplateExpressionOnly = Field(
        default=True,
        description="Offer HTTP/2, used when the server negotiates it; the requests to that server then share one connection, 100 at a time at most.",
    )
    max_connections: PositiveInt | NumberOrTemplate | None = Field(
        default=None,
        description="Connections the pool opens at most; null (the default) for no limit, which leaves parallel.max_concurrency to bound them.",
    )
    max_keepalive_connections: PositiveInt | NumberOrTemplate | None = Field(default=20, description="Idle connections the pool keeps open for reuse at most; null for no limit.")


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


class AuthCredentials(StrictModel):
    """A user name and password."""

    username: str = Field(description="User name (may be a template expression).")
    password: str = Field(description="Password (may be a template expression).")


# A namespace here is a `vars` object a template rendered, standing for the
# credentials it was written as (`"{{ {'basic': creds} }}"`).
_Credentials = Annotated[AuthCredentials, BeforeValidator(convert_namespace_to_dict)]


class BasicAuth(StrictModel):
    basic: _Credentials = Field(description="HTTP Basic authentication: the credentials go with every request.")


class DigestAuth(StrictModel):
    digest: _Credentials = Field(description="HTTP Digest authentication: the credentials answer the server's challenge, which costs the first request a round trip.")


class BearerAuth(StrictModel):
    # Not empty: "Bearer " authenticates nothing, and `env('TOKEN', '')` or a
    # save of an empty field rendering it so is a stage failure, as None is.
    bearer: Annotated[str, Field(min_length=1)] = Field(
        description="Token sent as 'Authorization: Bearer <token>' (may be a template expression).",
        examples=["{{ access_token }}"],
    )


# A bare string is a user function's import name, and `false` the request's
# opt-out; an object is told by its key. `true` is no form of auth, and gets
# the tag error listing those that are.
get_auth_discriminator = _create_discriminator(
    {
        str: "module:function",
        UserFunctionName: "module:function",
        UserFunctionKwargs: "name",
        BasicAuth: "basic",
        DigestAuth: "digest",
        BearerAuth: "bearer",
    },
    value_to_tag=((False, "false"),),
)


def _auth_namespace_to_dict(v: Any) -> Any:
    """A whole auth one template rendered from a `vars` object (``"auth":
    "{{ creds }}"``), as the object it was written as: `vars` makes every
    object in it a namespace, which no member takes, where the auth written
    inline would have been dicts all the way down, a user function's kwargs
    included. Inside an auth written as an object, a namespace is one value a
    template rendered: a user function's kwargs hand it on as it is, and a
    built-in's credentials take it as the object (`_Credentials`)."""
    return convert_namespace_to_dict(v) if isinstance(v, SimpleNamespace) else v


def _refuse_auth_string_unquoted(v: Any) -> Any:
    """A string auth is a user function's ``module:function`` name, or a
    template; one that is neither is refused here, without quoting it.

    The member it would go to quotes it twice, in the name grammar's message
    and the template's, and pydantic's ``hide_input_in_errors`` keeps only its
    own ``input_value`` out, not a validator's message. A whole auth written as
    one template renders what it was written for, and the natural mistake is a
    token (``"auth": "{{ token }}"`` for ``{"bearer": "{{ token }}"}``): the
    stage's failure, and the CI log, would print it.
    """
    if isinstance(v, str):
        if contains_template(v):
            # Only an empty `{{ }}` is refused, by position.
            validate_unquoted_partial_template_str(v)
        else:
            try:
                validate_function_import_name(v)
            except ValueError:
                raise ValueError(
                    """Not a user function's 'module:function' name (not shown, as auth can carry a credential); a built-in scheme is an object, such as {"bearer": "<token>"}"""
                ) from None
    return v


Auth = Annotated[
    Annotated[
        Annotated[UserFunctionName, Tag("module:function")]
        | Annotated[UserFunctionKwargs, Tag("name")]
        | Annotated[BasicAuth, Tag("basic")]
        | Annotated[DigestAuth, Tag("digest")]
        | Annotated[BearerAuth, Tag("bearer")],
        Discriminator(get_auth_discriminator),
    ],
    BeforeValidator(_auth_namespace_to_dict),
    BeforeValidator(_refuse_auth_string_unquoted),
]

# A request's auth also takes `false`: none for this request, the scenario's
# included. The members are Auth's, spelled out: a discriminated union cannot
# take another one as a member.
RequestAuth = Annotated[
    Annotated[
        Annotated[UserFunctionName, Tag("module:function")]
        | Annotated[UserFunctionKwargs, Tag("name")]
        | Annotated[BasicAuth, Tag("basic")]
        | Annotated[DigestAuth, Tag("digest")]
        | Annotated[BearerAuth, Tag("bearer")]
        | Annotated[Literal[False], Tag("false")],
        Discriminator(get_auth_discriminator),
    ],
    BeforeValidator(_auth_namespace_to_dict),
    BeforeValidator(_refuse_auth_string_unquoted),
]


def _refuse_scenario_auth_false(v: Any) -> Any:
    """Name what ``false`` is for, instead of pydantic's list of the union's tags."""
    if v is False:
        raise ValueError("false turns the scenario's auth off for one stage, so it belongs in a stage's request; to send no auth at all, leave auth out")
    return v


ScenarioAuth = Annotated[Auth, BeforeValidator(_refuse_scenario_auth_false)]

# A scenario's auth is rendered on its own, once, rather than as part of the
# `Scenario` whose field declares it, so it is validated again here: against
# the whole union, as a request's is within `Request` (a template declared as a
# user function's name can render a built-in scheme, `"{{ creds }}"`), and
# without pydantic's `input_value`, which would print the credentials. Only the
# outermost validator's `hide_input_in_errors` counts, and this one is it.
_RENDERED_SCENARIO_AUTH: TypeAdapter[Any] = TypeAdapter(ScenarioAuth, config=ConfigDict(hide_input_in_errors=True, title="auth"))


def validate_rendered_scenario_auth(value: Any) -> Auth:
    """A scenario's ``auth`` once its templates rendered (see above)."""
    return _RENDERED_SCENARIO_AUTH.validate_python(value, context=RENDERED)


def validate_rendered[M: BaseModel](model: type[M], value: Any) -> M:
    """``value``, a model of ``model`` dumped with its templates rendered,
    validated again as one (the `RENDERED` context): its text is final, so
    what only the scenario's own text means, an escaped ``\\{{``, is not
    read in it."""
    return model.model_validate(value, context=RENDERED)


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


class FileSpec(StrictModel):
    """A file of a multipart body, written as an object: where its bytes come
    from (exactly one of path, content and base64), and the filename and
    content type its part is sent with."""

    SOURCES: ClassVar[tuple[str, ...]] = ("path", "content", "base64")

    path: SerializablePath | PartialTemplateStr | None = Field(default=None, description="File to send; a relative path resolves against the scenario file's directory.")
    content: str | None = Field(default=None, description="The file's content as text, sent UTF-8 encoded.")
    base64: Base64String | PartialTemplateStr | None = Field(default=None, description="The file's content, base64-encoded: for binary data.")
    filename: str | None = Field(
        default=None,
        description="Filename the part is sent with. Not set: the path's last component, or for content and base64 the field's name. "
        "An empty string sends the part without a filename.",
    )
    content_type: PartContentType | None = Field(
        default=None,
        description="Content-Type of the part. Not set: guessed from the filename's extension, else application/octet-stream.",
        examples=["image/png", "application/json"],
    )

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        sources = [name for name in self.SOURCES if getattr(self, name) is not None]
        if len(sources) != 1:
            got = f", got {' and '.join(sources)}" if sources else ""
            raise ValueError(f"A file object sets exactly one of: {', '.join(self.SOURCES)}{got}")
        return self


def _file_entry_tag(v: Any) -> str:
    """An object is a `FileSpec`, a list several files under one name, and
    anything else a path: a template renders any of them."""
    if isinstance(v, dict | FileSpec):
        return "object"
    if isinstance(v, list | tuple):
        return "list"
    return "path"


def _refuse_null_file(v: Any) -> Any:
    """A file a template rendered to None, in one sentence: the path branch
    would refuse it twice, as a path and as template text, under a location
    spelling out pydantic's union of the two."""
    if v is None:
        raise ValueError("A file is a path or a file object, got null")
    return v


# One file: a path, or an object saying more about it. A list here, a list in
# a list, gets the tag error listing these two. Its namespace is converted
# here as well as in `FileEntries`: a tuple a template rendered is taken as
# the list, and `convert_namespace_to_dict` walks lists only.
_FileEntry = Annotated[
    Annotated[
        Annotated[SerializablePath | PartialTemplateStr, Tag("path")] | Annotated[FileSpec, Tag("object")],
        Discriminator(_file_entry_tag),
    ],
    BeforeValidator(convert_namespace_to_dict),
    BeforeValidator(_refuse_null_file),
]

# The files under one field name: one, or a list of them, each a part of its
# own. A template over `vars` renders an object as a SimpleNamespace, which
# stands for the file object it was written as; converted ahead of the union,
# keeping its tags in error locations.
FileEntries = Annotated[
    Annotated[
        Annotated[SerializablePath | PartialTemplateStr, Tag("path")] | Annotated[FileSpec, Tag("object")] | Annotated[list[_FileEntry], Tag("list")],
        Discriminator(_file_entry_tag),
    ],
    BeforeValidator(convert_namespace_to_dict),
    BeforeValidator(_refuse_null_file),
]

_FILES_DESCRIPTION = (
    "Files per field name: a path (relative to the scenario file's directory), a file object "
    "(exactly one of path, content or base64, and optionally filename and content_type), or a list of them, sent as parts of the same name."
)


class FilesBody(StrictModel):
    files: dict[str, FileEntries] = Field(
        description=f"Files to upload as multipart/form-data. {_FILES_DESCRIPTION}",
        examples=[{"document": "./report.pdf", "images": ["./a.png", {"path": "./b.png", "filename": "photo.png", "content_type": "image/png"}]}],
    )


class Multipart(StrictModel):
    """A multipart/form-data body: form fields and files, each a part of its
    own, the fields first; at least one of the two is set."""

    model_config = ConfigDict(json_schema_extra={"minProperties": 1})

    fields: dict[str, MultipartFieldValue] = Field(
        default_factory=dict,
        description="Form fields: text, a number or a boolean (sent as true or false), or a list of them, sent as fields of the same name.",
        examples=[{"title": "Report", "tags": ["a", "b"], "draft": False}],
    )
    files: dict[str, FileEntries] = Field(
        default_factory=dict,
        description=_FILES_DESCRIPTION,
        examples=[{"document": "./report.pdf", "note": {"content": "inline text", "filename": "note.txt", "content_type": "text/plain"}}],
    )

    @model_validator(mode="after")
    def _sets_fields_or_files(self) -> Self:
        # By key, not by emptiness: `{"fields": {}}`, or lists that are empty
        # once rendered, send a multipart body without parts, as a form
        # without inputs does.
        if not self.model_fields_set:
            raise ValueError("A multipart body sets at least one of: fields, files")
        return self


class MultipartBody(StrictModel):
    multipart: Multipart = Field(description="multipart/form-data body: form fields and files, each file from a path, text or base64.")


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
        MultipartBody: "multipart",
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
    | Annotated[MultipartBody, Tag("multipart")]
    | Annotated[GraphQLBody, Tag("graphql")],
    Discriminator(get_request_body_discriminator),
]


def _omit_schema_default(schema: JsonDict) -> None:
    schema.pop("default", None)


class Request(StrictModel):
    # A request is validated again once rendered, which is when its auth's
    # credentials (and any header's) carry the rendered secret: pydantic's
    # `input_value` would print it in the stage's failure. The validators'
    # own messages still say what was refused.
    model_config = ConfigDict(hide_input_in_errors=True)

    url: HttpUrlReferenceStr | PartialTemplateStr = Field(
        description="Absolute http(s) URL, or a URL relative to client.base_url (may be a template expression), passed to httpx as written."
    )
    method: HTTPMethod | HttpMethodToken | TemplateExpressionOnly = Field(
        default=HTTPMethod.GET,
        description="HTTP method: a standard verb (autocompleted) or any RFC 9110 token (e.g. PROPFIND, PURGE).",
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="URL query parameters, merged into any query already in the URL and over client.params; a key in both takes the value given here.",
    )
    headers: dict[str, str] = Field(default_factory=dict, description="HTTP request headers, over client.headers.")
    body: RequestBody | None = Field(default=None, description="Request body configuration.")
    auth: RequestAuth | None = Field(
        default=None,
        description="Authentication for this request, over the scenario's: basic, digest or bearer, a user function, or false for none.",
    )
    # Their defaults stand in for the client's (request_builder sends only a
    # declared value), so the schema shows none: an editor would offer them.
    timeout: PositiveFloat | NumberOrTemplate = Field(
        default=30.0,
        description="Request timeout in seconds. Not set, client.timeout applies (30 by default).",
        json_schema_extra=_omit_schema_default,
    )
    allow_redirects: Literal[True, False] | TemplateExpressionOnly = Field(
        default=True,
        description="Whether to follow redirects. Not set, client.follow_redirects applies (true by default).",
        json_schema_extra=_omit_schema_default,
    )


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
    BeforeValidator(normalize_list_input, json_schema_input_type=SubstitutionsInput),
]


class JMESPathSave(Descripted):
    """Save data using JMESPath expressions to extract values from response."""

    jmespath: dict[VariableName, JMESPathExpression | PartialTemplateStr] = Field(description="JMESPath expressions to extract values from response.")


class RegexCapture(StrictModel):
    """A save.regex entry written as an object: the pattern, which of its
    groups to save, and whether from the first match or from every match."""

    pattern: RegexPattern | PartialTemplateStr = Field(description="Regular expression searched for in the response body's text (re.search).")
    group: RegexGroupNumber | RegexGroupName | TemplateExpressionOnly | None = Field(
        default=None,
        description="The group to save: its number (0 for the whole match) or its name. Not set: group 1 if the pattern has groups, else the whole match.",
    )
    all: Literal[True, False] | TemplateExpressionOnly = Field(
        default=False,
        description="Save a list holding the group of every match (re.finditer), empty when nothing matches, instead of the first match's group.",
    )

    @model_validator(mode="after")
    def _group_is_in_the_pattern(self, info: ValidationInfo) -> Self:
        """A group a literal pattern does not have fails at load: the pattern
        it is searched with, its escapes rendered (`as_rendered`). One a
        template renders is checked once it has, when re-validated here, or
        by `response_steps.process_save` when it rendered to template text."""
        if self.group is not None and not contains_template([self.pattern, self.group]):
            regex_group(re.compile(as_rendered(self.pattern, info)), self.group)
        return self


def _regex_entry_tag(v: Any) -> str:
    """An object is a `RegexCapture`, anything else a pattern: a template
    that renders an object where a pattern is written makes it a capture,
    as a header matcher can be rendered whole."""
    return "capture" if isinstance(v, dict | RegexCapture) else "pattern"


# A template over `vars` renders an object as a SimpleNamespace, which stands
# for the capture object. Ahead of the union, keeping its tags in error
# locations.
RegexSaveEntry = Annotated[
    Annotated[
        Annotated[RegexPattern | PartialTemplateStr, Tag("pattern")] | Annotated[RegexCapture, Tag("capture")],
        Discriminator(_regex_entry_tag),
    ],
    BeforeValidator(convert_namespace_to_dict),
]


class RegexSave(Descripted):
    """Save what regular expressions find in the response body's text, for
    bodies that are not JSON (HTML, plain text)."""

    regex: dict[VariableName, RegexSaveEntry] = Field(
        description=(
            "A pattern per variable, searched for in the response body's text (re.search): the variable is group 1 of the first match "
            "if the pattern has groups, else the whole match. Or an object: pattern, group (a number or a name) and all "
            "(a list from every match). A pattern that does not match fails the step, unless all is set."
        ),
        examples=[
            {
                "csrf": 'name="csrf" value="([^"]+)"',
                "order_id": {"pattern": "Order #(?P<id>\\d+)", "group": "id"},
                "all_ids": {"pattern": "id=(\\d+)", "all": True},
            }
        ],
    )


class SubstitutionsSave(Descripted):
    """Save data using variable substitutions."""

    substitutions: Substitutions = Field(description="Variable substitution configuration.")


class UserFunctionsSave(Descripted):
    """Save data using user-defined functions to process response data."""

    user_functions: FunctionsList = Field(description="Functions to process response data.")


get_save_discriminator = _create_discriminator(
    {
        JMESPathSave: "jmespath",
        RegexSave: "regex",
        SubstitutionsSave: "substitutions",
        UserFunctionsSave: "user_functions",
    },
)


Save = Annotated[
    Annotated[JMESPathSave, Tag("jmespath")]
    | Annotated[RegexSave, Tag("regex")]
    | Annotated[SubstitutionsSave, Tag("substitutions")]
    | Annotated[UserFunctionsSave, Tag("user_functions")],
    Discriminator(get_save_discriminator),
]


with _suppress_field_shadow_warning("schema"):

    class ResponseBody(StrictModel):
        # A template over `vars` renders an object as a SimpleNamespace, which
        # stands for the dict it was declared as: converted all the way down, as
        # a schema is plain JSON. Ahead of the union, keeping its member tags in
        # pydantic's error locations.
        schema: Annotated[
            JSONSchemaInline | SchemaFileRefStr | PartialTemplateStr | None,
            BeforeValidator(convert_namespace_to_dict),
            BeforeValidator(refuse_escaped_braces("A schema file reference")),
        ] = Field(
            default=None,
            description="JSON Schema the body must validate against: inline, or a local file, optionally with a JSON pointer into it "
            "(openapi.json#/components/schemas/User). Its $refs resolve across the document and into other local files.",
            examples=[
                "./schemas/user.json",
                "./openapi.json#/components/schemas/User",
                {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}},
                "{{ user_schema }}",
            ],
        )
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


# An operand compared as JSON. A template over `vars` renders an object as a
# SimpleNamespace, which stands for the object it was written as.
_JsonOperand = Annotated[JsonValue, BeforeValidator(convert_namespace_to_dict)]


def _operand_schema(schema: JsonDict) -> None:
    """A matcher key's schema: no default, which an editor would offer (a key
    left out is not a check), and no null where null is not an operand."""
    schema.pop("default", None)
    branches = schema.get("anyOf")
    if isinstance(branches, list) and {"type": "null"} in branches:
        branches.remove({"type": "null"})
        if len(branches) == 1 and isinstance(only := branches[0], dict):
            del schema["anyOf"]
            schema.update(only)


_EQ_HINT = 'to compare with an object, give it as eq: {"eq": {...}}'


class JMESPathMatcher(StrictModel):
    """Matcher for the value one JMESPath expression extracts from the response
    body; every key given must hold. To compare with an object, give it as eq."""

    model_config = ConfigDict(json_schema_extra={"minProperties": 1})

    # A key the scenario sets is a check, one it leaves out is not
    # (`model_fields_set`): a null operand is compared like any other JSON value
    # where one can be (eq, ne, contains, not_contains), so None cannot stand
    # for "not set" there, and elsewhere null is refused.
    NULL_OPERANDS: ClassVar[frozenset[str]] = frozenset({"eq", "ne", "contains", "not_contains"})

    eq: _JsonOperand = Field(default=None, description="Equal to this JSON value (true is not 1; 1 equals 1.0).", json_schema_extra=_omit_schema_default)
    ne: _JsonOperand = Field(default=None, description='Not equal to this JSON value; {"ne": null} for a value that is there and not null.', json_schema_extra=_omit_schema_default)
    gt: JsonNumber | TemplateExpressionOnly | None = Field(default=None, description="A number greater than this.", json_schema_extra=_operand_schema)
    ge: JsonNumber | TemplateExpressionOnly | None = Field(default=None, description="A number greater than or equal to this.", json_schema_extra=_operand_schema)
    lt: JsonNumber | TemplateExpressionOnly | None = Field(default=None, description="A number less than this.", json_schema_extra=_operand_schema)
    le: JsonNumber | TemplateExpressionOnly | None = Field(default=None, description="A number less than or equal to this.", json_schema_extra=_operand_schema)
    contains: _JsonOperand = Field(
        default=None,
        description="A string holding this substring, an array holding an element equal to this, or an object holding this key.",
        json_schema_extra=_omit_schema_default,
    )
    not_contains: _JsonOperand = Field(
        default=None,
        description="A string without this substring, an array without an element equal to this, or an object without this key.",
        json_schema_extra=_omit_schema_default,
    )
    matches: RegexPattern | PartialTemplateStr | None = Field(default=None, description="A string this regex matches (re.search).", json_schema_extra=_operand_schema)
    not_matches: RegexPattern | PartialTemplateStr | None = Field(default=None, description="A string this regex does not match (re.search).", json_schema_extra=_operand_schema)
    type: JsonTypeName | TemplateExpressionOnly | None = Field(
        default=None,
        description="A value of this JSON type: integer is a number written without a fraction or exponent, number any number; neither is a boolean.",
        json_schema_extra=_operand_schema,
    )
    length: JsonLength | TemplateExpressionOnly | None = Field(
        default=None, description="A string, array or object of this length (characters, elements, keys).", json_schema_extra=_operand_schema
    )

    @model_validator(mode="after")
    def _checks_something(self) -> Self:
        if not self.model_fields_set:
            raise ValueError(f'JMESPath matcher must set at least one of: {", ".join(type(self).model_fields)}; to compare with an empty object, write {{"eq": {{}}}}')
        for name in self.model_fields_set - self.NULL_OPERANDS:
            if getattr(self, name) is None:
                raise ValueError(f"JMESPath matcher's {name} must not be null; null is an operand of eq, ne, contains and not_contains only")
        return self

    # Defined last, so it wraps the checks above too: pydantic applies a
    # model's validators in the order they are defined, each around the last.
    @model_validator(mode="wrap")
    @classmethod
    def _object_is_a_matcher(cls, data: Any, handler: ValidatorFunctionWrapHandler, info: ValidationInfo) -> Any:
        """An object in a ``verify.jmespath`` value is a matcher, always: a
        literal object written for equality lands here, and gets told where it
        belongs. One with a key no matcher has fails with that alone, rather
        than pydantic's "extra inputs are not permitted"; one whose keys are all
        a matcher's but whose values are not its operands (``{"type":
        "admin"}``) fails as a matcher, and gets the same hint beside those
        errors. A rendered matcher (`validate_rendered_verify`) was one as
        declared: only its rendered operands can fail, and the hint is noise."""
        if not isinstance(data, dict) or info.context is RENDERED:
            return handler(data)
        unknown = [key for key in data if key not in cls.model_fields and key != "$schema"]
        if unknown:
            raise ValueError(f"An object here is a matcher, and {', '.join(map(repr, unknown))} is not one of its keys ({', '.join(cls.model_fields)}); {_EQ_HINT}")
        try:
            return handler(data)
        except ValidationError as e:
            if not data:
                # Its own message says how to compare with an empty object.
                raise
            # Kept as they were (each type and message, in pydantic's order),
            # with the hint as one more error at the object itself. Each is
            # re-raised as a custom error of the same type and message, as a
            # built-in one would need its own context back to be re-raised.
            errors: list[InitErrorDetails] = [
                {"type": PydanticCustomError(cast(LiteralString, error["type"]), cast(LiteralString, error["msg"])), "loc": error["loc"], "input": error["input"]}
                for error in e.errors()
            ]
            hint = PydanticCustomError("jmespath_matcher", cast(LiteralString, f"An object here is a matcher; {_EQ_HINT}"))
            raise ValidationError.from_exception_data(e.title, [*errors, {"type": hint, "loc": (), "input": data}]) from None


def _jmespath_expectation_tag(v: Any) -> str:
    """An object is a matcher, whatever its keys; anything else is a value, and
    so is whatever a value declared as one rendered (`_DeclaredValue`)."""
    if isinstance(v, _DeclaredValue):
        return "value"
    return "matcher" if isinstance(v, dict | JMESPathMatcher) else "value"


@dataclass(frozen=True, slots=True)
class _DeclaredValue:
    """A ``verify.jmespath`` expectation the scenario declared as a value (a
    template), once rendered: still a value, whatever it rendered to. Only an
    object the scenario writes is a matcher; one a template renders where a
    value was written is the value to compare with (`validate_rendered_verify`)."""

    value: Any


def _declared_value(v: Any) -> Any:
    """The rendered value a `_DeclaredValue` carries, as JSON: a template over
    ``vars`` renders an object as a SimpleNamespace, which stands for it."""
    return convert_namespace_to_dict(v.value) if isinstance(v, _DeclaredValue) else v


# A value compared by JSON equality: anything but an object, which is a matcher.
# The schema says so, so an editor holds a literal object to the matcher's keys.
_JsonEqualityValue = Annotated[
    JsonValue,
    WithJsonSchema({"type": ["string", "number", "boolean", "null", "array"], "description": "Equal to this JSON value (true is not 1; 1 equals 1.0)."}),
]

JMESPathExpectation = Annotated[
    Annotated[
        Annotated[JMESPathMatcher, Tag("matcher")] | Annotated[_JsonEqualityValue, BeforeValidator(_declared_value), Tag("value")],
        Discriminator(_jmespath_expectation_tag),
    ],
    # Ahead of the union: a namespace is the object it stands for, so a matcher
    # as it stands. Rendered where a value was declared, it arrives wrapped in a
    # `_DeclaredValue` instead, and `_declared_value` converts it as a value.
    BeforeValidator(convert_namespace_to_dict),
]


# One expected status: a code, `HTTPStatus` first for the schema's
# autocompletion, or a class such as "2xx"; a template renders to either.
ExpectedStatus = HTTPStatus | StatusCode | StatusClass | NumberOrTemplate


class Verify(Descripted):
    # A list's entries are values, as the `headers` map's exact strings are:
    # one a template rendered to None fails re-validation, not the None guard.
    status: ExpectedStatus | Annotated[list[ExpectedStatus], Field(min_length=1)] | None = Field(
        default=None,
        description=(
            "Expected HTTP status: a code (a standard one autocompleted, or any integer 100-599, e.g. 499), "
            "a class '1xx'-'5xx' (case-insensitive) matching any code in it, "
            "or a non-empty list of codes and classes, any one of which passes. "
            "A template may render to any of these forms."
        ),
        examples=[200, "2xx", [200, 201], ["2xx", 304], "{{ expected_status }}"],
    )
    # A matcher written as one template over `vars` ("{{ ct }}") renders as a
    # SimpleNamespace, which stands for the matcher object it was declared as.
    headers: dict[str, Annotated[str | HeaderMatcher, BeforeValidator(convert_namespace_to_dict)]] = Field(
        default_factory=dict,
        description="Expected response headers: a string (exact match) or a matcher object (contains/not_contains/matches/not_matches) per key.",
    )
    # Keys are never rendered (walk() substitutes values), values are.
    jmespath: dict[JMESPathKey, JMESPathExpectation] = Field(
        default_factory=dict,
        description=(
            "Assertions on the JSON response body: a JMESPath expression per key, mapped to the value it must equal "
            "(any JSON value but an object) or to a matcher object (eq, ne, gt, ge, lt, le, contains, not_contains, "
            "matches, not_matches, type, length). A missing path extracts null."
        ),
        examples=[{"data.id": "{{ user_id }}", "length(items)": 3, "items[0].name": {"matches": "^A"}, "meta": {"eq": {"page": 1}}}],
    )
    expressions: list[TemplateExpressionSchema] = Field(
        default_factory=list,
        description=(
            "Template expressions evaluated as boolean conditions against the context "
            "(saved variables, fixtures, substitutions). Each must be a full template "
            "expression that evaluates to a boolean. Response metadata is "
            "available as `response.*`: status, reason, headers, elapsed_ms."
        ),
        examples=[["{{ user_age >= 18 }}", "{{ response.status == 200 }}", "{{ 'json' in response.headers['content-type'] }}"]],
    )
    user_functions: FunctionsList = Field(default_factory=list, description="Functions to process response data.")
    body: ResponseBody = Field(default_factory=ResponseBody)


def validate_rendered_verify(declared: Verify, value: Any) -> Verify:
    """A verify step once its templates rendered: `Verify` again, but each
    ``verify.jmespath`` expectation keeps the kind it was declared as.

    Re-validation alone cannot tell a matcher the scenario wrote from an object
    a value's template rendered (``"meta": "{{ saved }}"``): both arrive as a
    dict, and the union takes a dict for a matcher. Read as one, a rendered
    object whose keys happen to be a matcher's (``{"type": "object"}``, a saved
    JSON Schema fragment) would be checked as that matcher, and could pass where
    the objects differ. So what was declared decides: a value stays a value
    (`_DeclaredValue`) and is compared by equality, whatever it rendered to."""
    values = {expression for expression, expected in declared.jmespath.items() if not isinstance(expected, JMESPathMatcher)}
    if values and isinstance(value, dict) and isinstance(expectations := value.get("jmespath"), dict):
        value = {**value, "jmespath": {expression: _DeclaredValue(expected) if expression in values else expected for expression, expected in expectations.items()}}
    return Verify.model_validate(value, context=RENDERED)


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
    BeforeValidator(normalize_list_input, json_schema_input_type=ResponsesInput),
]


class IndividualParameter(StrictModel):
    individual: Annotated[
        dict[str, Annotated[list[Any], Field(min_length=1)] | PartialTemplateStr],
        Field(min_length=1, max_length=1),
    ] = Field(description="Parameter name mapped to list of values (single parameter per step, non-empty values) or template expression")
    ids: list[str] | None = Field(default=None, description="Optional IDs for each value")

    @model_validator(mode="after")
    def validate_ids_match_values(self) -> Self:
        if self.ids is not None:
            values = next(iter(self.individual.values()))
            if isinstance(values, str):  # template form: count unknown until runtime
                return self
            if len(self.ids) != len(values):
                raise ValueError(f"Number of ids ({len(self.ids)}) must match number of values ({len(values)})")
        return self


class CombinationsParameter(StrictModel):
    # A template over `vars` renders each combination as a SimpleNamespace,
    # which stands for the dict it was declared as. Stage `parametrize` and
    # `parallel.foreach` both re-validate the rendered list here, so one rule
    # serves both. Converted ahead of the union rather than per item: an item
    # validator would turn `list[dict[str,any]]` in error locations into its repr.
    combinations: Annotated[
        Annotated[list[Annotated[dict[str, Any], Field(min_length=1)]], Field(min_length=1)] | PartialTemplateStr,
        BeforeValidator(convert_namespace_items_to_dict),
    ] = Field(description="Non-empty list of parameter combinations (each dict must have at least one parameter) or template expression")
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

        if self.ids is not None and len(self.ids) != len(self.combinations):
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


def _refuse_bool(setting: str) -> Callable[[Any], Any]:
    """A numeric ``setting`` written or rendered as true or false, refused
    ahead of its union. The number branches take a bool as 1 (pydantic's lax
    mode), so a retry's `"attempts": true`, or `"{{ poll }}"` rendering a
    flag, would attempt once and turn retry off without a word, `"delay":
    true` wait a second, and a threshold's `"min_success_ratio": true` have
    every iteration pass. The JSON Schema's integer and number refuse a
    boolean already."""

    def refuse(v: Any) -> Any:
        if isinstance(v, bool):
            raise ValueError(f"{setting} is a number or a template, got {str(v).lower()}")
        return v

    return refuse


# A parallel stage's limits: a success ratio from 0 to 1, and a positive
# number of milliseconds or iterations per second, each finite, as the carrier
# resolves them (`_threshold_limits`). None takes a bool (`_refuse_bool`).
_SuccessRatio = Annotated[Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] | NumberOrTemplate, BeforeValidator(_refuse_bool("A threshold"))]
_PositiveLimit = Annotated[Annotated[float, Field(gt=0, allow_inf_nan=False)] | NumberOrTemplate, BeforeValidator(_refuse_bool("A threshold"))]


class ParallelThresholds(StrictModel):
    """What a parallel stage's stats must reach, checked once every iteration
    has ended: each limit is optional, and a stage fails naming every one it
    did not meet. Latencies are over the iterations that passed."""

    min_success_ratio: _SuccessRatio | None = Field(
        default=None,
        description=(
            "The share of the iterations that must pass, from 0 to 1. Below 1 a failing iteration neither cancels the others nor fails "
            "the stage by itself: the stage fails once they have all ended if fewer passed, and saves what the passed ones saved. "
            "Without it (or at 1) the first failing iteration cancels the rest and fails the stage."
        ),
        examples=[0.95, "{{ min_ratio }}"],
    )
    max_mean_ms: _PositiveLimit | None = Field(default=None, description="The passed iterations' mean latency, in milliseconds, at most.")
    max_p50_ms: _PositiveLimit | None = Field(default=None, description="The passed iterations' median (p50) latency, in milliseconds, at most.")
    max_p95_ms: _PositiveLimit | None = Field(default=None, description="The passed iterations' 95th percentile latency, in milliseconds, at most.")
    max_p99_ms: _PositiveLimit | None = Field(default=None, description="The passed iterations' 99th percentile latency, in milliseconds, at most.")
    min_rps: _PositiveLimit | None = Field(default=None, description="Passed iterations per second of the stage's wall time, at least.")


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
    collect_saves: Literal[True, False] | TemplateExpressionOnly = Field(
        default=False,
        description=(
            "Keep every iteration's saves: each name any iteration saves becomes a list with one entry per iteration, "
            "in iteration order, null where an iteration did not save it. False: the iterations' saves merge, "
            "and of those that save the same name the highest iteration index wins."
        ),
    )
    thresholds: ParallelThresholds | None = Field(
        default=None,
        description="Limits the stage's stats must meet once every iteration has ended: a success ratio, latencies and a throughput.",
    )
    stats_as: VariableName | None = Field(
        default=None,
        description=(
            "Save the stage's stats under this name, as an object (iterations, passed, failed, success_ratio, wall_ms, rps, completed_rps, "
            "min_ms, mean_ms, p50_ms, p95_ms, p99_ms, max_ms), for the stages after it: saved, as every save is, only when the stage passes."
        ),
        examples=["load"],
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


# The failures a stage's `retry` can make another attempt after, by the
# response step or the request that failed (`RetryConfig.on`).
RetryOn = Literal["verify", "save", "request"]
RETRY_ON: tuple[RetryOn, ...] = ("verify", "save", "request")


# A retry's attempts, seconds and backoff factor. The seconds and the factor
# are finite, as the carrier resolves them (`_setting_number`), so what it
# refuses the model refuses at load: JSON's 1e999 reads as inf, which float
# takes by default. None of them takes a bool (`_refuse_bool`).
_RetryAttempts = Annotated[PositiveInt | NumberOrTemplate, BeforeValidator(_refuse_bool("A retry setting"))]
_RetrySeconds = Annotated[Annotated[float, Field(ge=0, allow_inf_nan=False)] | NumberOrTemplate, BeforeValidator(_refuse_bool("A retry setting"))]
_RetryFactor = Annotated[Annotated[float, Field(ge=1, allow_inf_nan=False)] | NumberOrTemplate, BeforeValidator(_refuse_bool("A retry setting"))]


class RetryConfig(StrictModel):
    """Attempt the stage again when it fails: poll until the response steps pass, or ride out a failing network."""

    attempts: _RetryAttempts = Field(
        description="Attempts in all, the first included: 1 attempts once, as without retry.",
        examples=[10, "{{ max_polls }}"],
    )
    delay: _RetrySeconds = Field(default=1.0, description="Seconds to wait before the second attempt.")
    backoff: _RetryFactor = Field(
        default=1.0,
        description="What the wait is multiplied by after each attempt: 2 doubles it. 1 waits delay seconds every time.",
    )
    max_delay: _RetrySeconds | None = Field(default=None, description="Seconds a single wait lasts at most; null for no limit.")
    on: RetryOn | Annotated[list[RetryOn], Field(min_length=1)] = Field(
        default=list(RETRY_ON),
        description=(
            "The failures that make another attempt: a verify step's checks (verify), a save step's extraction (save), "
            "the request timing out or its connection failing (request). The scenario's own failures are never retried: "
            "a template that fails to render, a user function that cannot be called or crashes, a body schema that cannot be read."
        ),
        examples=["verify", ["verify", "save"]],
    )


class Stage(Marked, Fixtured, Descripted):
    name: str = Field(default="", description="Stage name (human-readable).")
    substitutions: Substitutions = Field(default_factory=list, description="Variable substitution configuration.")
    always_run: Literal[True, False] | TemplateExpressionOnly = Field(
        default=False,
        description="Execute even if a previous stage failed. A template expression is evaluated (truthiness) when the chain is aborted, "
        "against fixtures, parametrize parameters, scenario substitutions, and previously saved variables.",
        examples=[True, "{{ should_run }}", "{{ target == 'production' }}"],
    )
    skip_if: Literal[True, False] | TemplateExpressionOnly = Field(
        default=False,
        description="Skip the stage when true. A template expression is evaluated when the stage is about to run and must evaluate to a boolean, "
        "against fixtures, parametrize parameters, scenario substitutions, previously saved variables, and the stage's own substitutions. "
        "A skipped stage saves nothing and does not abort the chain.",
        examples=[True, "{{ env('TARGET_ENV', 'dev') == 'production' }}", "{{ not get('feature_enabled', false) }}"],
    )
    parametrize: Parameters | None = Field(default=None, description="Stage parametrization steps")
    parallel: ParallelConfig | None = Field(default=None, description="Parallel execution configuration for load/stress testing.")
    retry: RetryConfig | None = Field(
        default=None,
        description="Attempt the stage again while it fails, after a wait: each attempt renders and sends the request anew and runs every response step. "
        "With parallel, each iteration retries on its own.",
    )
    request: Request = Field(description="HTTP request details.")
    response: Responses = Field(default_factory=list, description="Sequential steps to process the response.")


Stages = Annotated[
    list[Stage],
    BeforeValidator(_normalize_stages_input, json_schema_input_type=list[Stage] | dict[str, Stage]),
]


class Scenario(Marked, Fixtured, Descripted):
    fixtures: list[str] = Field(default_factory=list, description="pytest fixtures available to all stages")
    auth: ScenarioAuth | None = Field(
        default=None,
        description="Authentication for every request: basic, digest or bearer, or a user function for a custom scheme.",
    )
    ssl: SSLConfig = Field(
        default_factory=SSLConfig,
        description="SSL/TLS configuration.",
    )
    client: ClientConfig = Field(
        default_factory=ClientConfig,
        description="The shared HTTP client: base URL, default headers and query parameters, timeout, redirects, proxy, HTTP/2 and connection pool.",
    )
    stages: Stages = Field(default_factory=list, description="Ordered list (or name-keyed mapping) of stages to execute.")
    substitutions: Substitutions = Field(default_factory=list, description="Variable substitution configuration.")
