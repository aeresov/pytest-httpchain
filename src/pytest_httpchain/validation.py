"""Static validation for pytest-httpchain scenario files.

This is the single source of truth for scenario validation, consumed by the
`pytest-httpchain validate` CLI command (and available for collection-time and
editor integrations). It performs structural validation via the Pydantic
``Scenario`` model plus cross-cutting semantic checks that a JSON Schema cannot
express (duplicate stage names, undefined-variable/data-flow, fixture conflicts).
"""

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest_httpchain_jsonref.loader
from pydantic import BaseModel, ValidationError
from pytest_httpchain_jsonref.exceptions import ReferenceResolverError
from pytest_httpchain_models.entities import Scenario
from pytest_httpchain_templates.expressions import TEMPLATE_PATTERN
from pytest_httpchain_templates.substitution import JSON_LITERALS, SAFE_FUNCTIONS

# Names provided by the template engine that don't need user definition.
TEMPLATE_BUILTINS = (
    set(SAFE_FUNCTIONS)
    | set(JSON_LITERALS)
    | {"exists", "get"}  # context helpers added at eval time
    | {"rand", "randint", "int", "float", "str"}  # simpleeval defaults
)


def _extract_names_from_expr(expr: str) -> set[str]:
    """Extract identifier names from a Python expression using AST parsing."""
    try:
        tree = ast.parse(expr.strip(), mode="eval")
        return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    except SyntaxError:
        return set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", expr))


def extract_template_variables(obj: Any, variables: set[str] | None = None) -> set[str]:
    """Recursively extract variable names from {{ expr }} template expressions."""
    if variables is None:
        variables = set()

    if isinstance(obj, str):
        for match in re.finditer(TEMPLATE_PATTERN, obj):
            names = _extract_names_from_expr(match.group("expr"))
            variables.update(names - TEMPLATE_BUILTINS)
    elif isinstance(obj, dict):
        for value in obj.values():
            extract_template_variables(value, variables)
    elif isinstance(obj, list):
        for item in obj:
            extract_template_variables(item, variables)

    return variables


def extract_saved_variables(scenario: Scenario) -> set[str]:
    """Extract variable names saved in response steps."""
    saved_vars: set[str] = set()

    for stage in scenario.stages:
        for response_step in stage.response:
            if not hasattr(response_step, "save"):
                continue
            save = response_step.save
            if hasattr(save, "jmespath") and isinstance(save.jmespath, dict):
                saved_vars.update(save.jmespath.keys())
            if hasattr(save, "substitutions"):
                for sub in save.substitutions:
                    if hasattr(sub, "vars") and isinstance(sub.vars, dict):
                        saved_vars.update(sub.vars.keys())
                    if hasattr(sub, "functions") and isinstance(sub.functions, dict):
                        saved_vars.update(sub.functions.keys())

    return saved_vars


def _parameter_names(params: Any) -> set[str]:
    """Names injected by a list of parametrize/foreach Parameter entries.

    Covers both ``individual`` (one name -> list of values) and ``combinations``
    (list of dicts whose keys are the names). Template-string forms (deferred to
    runtime) contribute no statically-known names.
    """
    names: set[str] = set()
    for param in params or []:
        individual = getattr(param, "individual", None)
        if isinstance(individual, dict):
            names.update(k for k in individual if isinstance(k, str))
        combinations = getattr(param, "combinations", None)
        if isinstance(combinations, list):
            for combo in combinations:
                if isinstance(combo, dict):
                    names.update(k for k in combo if isinstance(k, str))
    return names


def extract_defined_variables(scenario: Scenario, test_data: dict[str, Any]) -> set[str]:
    """Extract variable names made available before/within templates.

    Sources: ``vars`` and ``functions`` substitutions (scenario- and stage-level),
    plus parameter names injected by ``parametrize`` and ``parallel.foreach`` — all
    of which the engine wires into the evaluation context at runtime.
    """
    defined_vars: set[str] = set()

    # Defensive: a top-level "vars" key is not a model field but is tolerated.
    if "vars" in test_data and isinstance(test_data["vars"], dict):
        defined_vars.update(k for k in test_data["vars"] if isinstance(k, str))

    def add_substitution_names(subs: Any) -> None:
        for sub in subs:
            vars_ = getattr(sub, "vars", None)
            if isinstance(vars_, dict):
                defined_vars.update(k for k in vars_ if isinstance(k, str))
            functions = getattr(sub, "functions", None)
            if isinstance(functions, dict):
                defined_vars.update(k for k in functions if isinstance(k, str))

    add_substitution_names(scenario.substitutions)

    for stage in scenario.stages:
        add_substitution_names(stage.substitutions)
        defined_vars |= _parameter_names(stage.parametrize)
        if stage.parallel is not None:
            defined_vars |= _parameter_names(getattr(stage.parallel, "foreach", None))

    return defined_vars


class ScenarioInfo(BaseModel):
    """Detailed information about the scenario structure."""

    num_stages: int = 0
    stage_names: list[str] = []
    vars_referenced: list[str] = []
    vars_saved: list[str] = []
    vars_defined: list[str] = []
    fixtures: list[str] = []


class ValidateResult(BaseModel):
    """Result of scenario validation."""

    valid: bool
    errors: list[str] = []
    warnings: list[str] = []
    scenario_info: ScenarioInfo | None = None


def check_scenario(scenario: Scenario, test_data: dict[str, Any]) -> tuple[list[str], list[str], ScenarioInfo]:
    """Semantic checks on an already-loaded, schema-valid scenario.

    Operates on the parsed ``test_data`` dict together with the validated ``Scenario``
    so it can be shared by :func:`validate_scenario` (file-based) and by the pytest
    collector. Returns ``(errors, warnings, scenario_info)``.
    """
    errors: list[str] = []
    warnings: list[str] = []

    stage_names = [stage.name for stage in scenario.stages]
    seen_names: set[str] = set()
    duplicate_names: set[str] = set()
    for name in stage_names:
        if name in seen_names:
            duplicate_names.add(name)
        seen_names.add(name)
    if duplicate_names:
        errors.append(f"Duplicate stage names found: {sorted(duplicate_names)}")

    fixtures: list[str] = []
    if "fixtures" in test_data and isinstance(test_data["fixtures"], list):
        fixtures = list(test_data["fixtures"])
    for stage in scenario.stages:
        fixtures.extend(stage.fixtures)
    fixtures = list(set(fixtures))

    vars_defined = extract_defined_variables(scenario, test_data)
    vars_saved = extract_saved_variables(scenario)
    vars_referenced = extract_template_variables(test_data)

    fixture_set = set(fixtures)
    var_conflicts = fixture_set & vars_defined
    if var_conflicts:
        errors.append(f"Conflicting fixtures and vars with same names: {sorted(var_conflicts)}")

    # NOTE: response data (response/status_code/body/json/text/headers/cookies) is
    # NOT ambient in {{ }} templates — it reaches save/verify handlers directly and
    # only enters the template context via an earlier `save` step. So there are no
    # response "builtins" to whitelist here; template functions are already excluded
    # from vars_referenced via TEMPLATE_BUILTINS.
    all_available_vars = vars_defined | vars_saved | fixture_set

    undefined_vars = vars_referenced - all_available_vars
    if undefined_vars:
        warnings.append(f"Potentially undefined variables referenced: {sorted(undefined_vars)}")

    for stage in scenario.stages:
        if not any(hasattr(step, "verify") for step in stage.response):
            warnings.append(f"Stage '{stage.name}' has no response validation (no verify step)")

    scenario_info = ScenarioInfo(
        num_stages=len(scenario.stages),
        stage_names=stage_names,
        vars_referenced=sorted(vars_referenced),
        vars_saved=sorted(vars_saved),
        vars_defined=sorted(vars_defined),
        fixtures=sorted(fixtures),
    )

    return errors, warnings, scenario_info


def validate_scenario(
    path: Path,
    ref_parent_traversal_depth: int = 3,
    root_path: Path | None = None,
) -> ValidateResult:
    """Validate a pytest-httpchain test scenario file.

    Performs file/JSON/$ref/schema validation plus semantic checks (duplicate
    stage names, undefined variables, fixture/variable conflicts, missing verify).
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not path.exists():
        return ValidateResult(valid=False, errors=[f"File not found: {path}"])

    if not path.is_file():
        return ValidateResult(valid=False, errors=[f"Path is not a file: {path}"])

    if path.suffix.lower() not in (".json",):
        warnings.append(f"File has extension '{path.suffix}' but expected '.json'. Consider renaming to use .json extension.")

    if root_path is None:
        potential_root = path.parent
        while potential_root.parent != potential_root:
            if potential_root.name == "tests":
                root_path = potential_root
                break
            potential_root = potential_root.parent
        else:
            root_path = path.parent

    try:
        test_data = pytest_httpchain_jsonref.loader.load_json(
            path,
            max_parent_traversal_depth=ref_parent_traversal_depth,
            root_path=root_path,
        )
    except ReferenceResolverError as e:
        return ValidateResult(valid=False, errors=[f"JSON reference resolution error: {e}"], warnings=warnings)
    except json.JSONDecodeError as e:
        return ValidateResult(valid=False, errors=[f"Invalid JSON syntax: {e}"], warnings=warnings)
    except Exception as e:
        return ValidateResult(valid=False, errors=[f"Failed to parse JSON file: {e}"], warnings=warnings)

    try:
        scenario = Scenario.model_validate(test_data)
    except ValidationError as e:
        error_details = [f"{' -> '.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]
        return ValidateResult(valid=False, errors=["Schema validation failed:"] + error_details, warnings=warnings)

    semantic_errors, semantic_warnings, scenario_info = check_scenario(scenario, test_data)
    errors.extend(semantic_errors)
    warnings.extend(semantic_warnings)

    return ValidateResult(valid=not errors, errors=errors, warnings=warnings, scenario_info=scenario_info)
