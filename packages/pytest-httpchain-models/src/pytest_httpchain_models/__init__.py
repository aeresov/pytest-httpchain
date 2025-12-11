"""Pydantic models for pytest-httpchain test scenarios.

This package defines the data models used to validate and represent HTTP test
scenarios, including requests, responses, verification steps, and configuration.
All models use Pydantic V2 with discriminated unions for flexible body types.

Key models:
- Scenario: Root model representing a complete test scenario
- Stage: Individual test stage with request and response processing
- Request: HTTP request configuration (method, URL, headers, body)
- Verify: Response verification rules (status, headers, body, expressions)
- Save: Data extraction from responses (JMESPath, substitutions, user functions)
"""

from .entities import (
    Base64Body,
    BinaryBody,
    CombinationsParameter,
    FilesBody,
    FormBody,
    FunctionsDict,
    FunctionsList,
    FunctionsSubstitution,
    GraphQLBody,
    IndividualParameter,
    JMESPathSave,
    JsonBody,
    ParallelConfig,
    ParallelConfigBase,
    ParallelForeachConfig,
    ParallelRepeatConfig,
    Parameter,
    Parameters,
    Request,
    RequestBody,
    ResponseBody,
    Responses,
    ResponseStep,
    Save,
    SaveStep,
    Scenario,
    SSLConfig,
    Stage,
    Substitution,
    Substitutions,
    SubstitutionsSave,
    TextBody,
    UserFunctionCall,
    UserFunctionKwargs,
    UserFunctionName,
    UserFunctionsSave,
    VarsSubstitution,
    Verify,
    VerifyStep,
    XmlBody,
)
from .types import check_json_schema

__all__ = [
    "Scenario",
    "Stage",
    "ParallelConfig",
    "ParallelConfigBase",
    "ParallelForeachConfig",
    "ParallelRepeatConfig",
    "Parameters",
    "Parameter",
    "CombinationsParameter",
    "IndividualParameter",
    "Responses",
    "ResponseStep",
    "VerifyStep",
    "SaveStep",
    "Verify",
    "ResponseBody",
    "Save",
    "JMESPathSave",
    "SubstitutionsSave",
    "UserFunctionsSave",
    "Substitutions",
    "Substitution",
    "FunctionsSubstitution",
    "VarsSubstitution",
    "Request",
    "RequestBody",
    "JsonBody",
    "XmlBody",
    "FormBody",
    "TextBody",
    "Base64Body",
    "BinaryBody",
    "FilesBody",
    "GraphQLBody",
    "FunctionsDict",
    "FunctionsList",
    "UserFunctionCall",
    "UserFunctionName",
    "UserFunctionKwargs",
    "SSLConfig",
    "check_json_schema",
]
