"""The ``pytest-httpchain`` command line: ``validate``, ``schema``, ``resolve``,
``show`` and ``graph`` over scenario files, outside a pytest run."""

import importlib.metadata
import json
from collections import Counter
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from pytest_httpchain.constants import check_suffix
from pytest_httpchain.dataflow import DataFlow, analyze_dataflow
from pytest_httpchain.jsonref import ReferenceResolverError
from pytest_httpchain.models import Scenario
from pytest_httpchain.schema import build_schema
from pytest_httpchain.validation import DiagnosticCode, DiscoveryError, ValidateResult, load_scenario, load_scenario_json, validate_paths

app = typer.Typer(no_args_is_help=True)


class OutputFormat(StrEnum):
    text = "text"
    json = "json"


class GraphDirection(StrEnum):
    TD = "TD"
    LR = "LR"


RefParentTraversalDepth = Annotated[int, typer.Option(help="Maximum $ref parent directory traversal depth.")]
RootPath = Annotated[Path | None, typer.Option("--root-path", help="Directory that constrains $ref resolution (default: auto-detected project root).")]
OutputFormatOption = Annotated[OutputFormat, typer.Option("--format", help="Output format: human-readable text or machine-readable JSON.")]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(importlib.metadata.version("pytest-httpchain"))
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", help="Show the pytest-httpchain version and exit.", callback=_version_callback, is_eager=True),
    ] = False,
) -> None:
    """pytest-httpchain command-line tools."""


def _suffix_option(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return check_suffix(value)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from None


# The results that stand for no file checked, each the one diagnostic of a
# path given that names nothing to check, and how the summary counts them
# apart (singular, plural).
_NOTHING_CHECKED = {
    DiagnosticCode.FILE_NOT_FOUND: ("path not found", "paths not found"),
    DiagnosticCode.NOT_A_FILE: ("path that is not a file", "paths that are not files"),
    DiagnosticCode.NO_SCENARIO_FILES: ("directory without scenario files", "directories without scenario files"),
}


def _summary(results: list[tuple[Path, ValidateResult]]) -> str | None:
    """The closing line of a text report, when it checked more than one file:
    each file counted once, under errors if it has any, else under warnings if
    it has any. A path that names nothing to check (`_NOTHING_CHECKED`) is no
    file checked, and is counted apart."""
    files: list[ValidateResult] = []
    apart: Counter[DiagnosticCode] = Counter()
    for _, result in results:
        code = next((d.code for d in result.diagnostics if d.code in _NOTHING_CHECKED), None)
        if code is None:
            files.append(result)
        else:
            apart[code] += 1
    if len(files) < 2:
        return None
    with_errors = sum(not result.valid for result in files)
    with_warnings = sum(result.valid and bool(result.warnings) for result in files)
    line = f"{len(files)} files checked, {with_errors} with errors, {with_warnings} with warnings"
    for code, (one, many) in _NOTHING_CHECKED.items():
        if count := apart[code]:
            line += f", {count} {one if count == 1 else many}"
    return line


@app.command()
def validate(
    paths: Annotated[list[Path], typer.Argument(help="Scenario files to validate, or directories to search for them.")],
    ref_parent_traversal_depth: RefParentTraversalDepth = 3,
    root_path: RootPath = None,
    output_format: OutputFormatOption = OutputFormat.text,
    deep: Annotated[bool, typer.Option("--deep", help="Run deep checks: resolve user-function imports/signatures and referenced files. Imports user modules.")] = False,
    syspath: Annotated[list[Path] | None, typer.Option("--syspath", help="Extra directories to add to sys.path for --deep import resolution (repeatable).")] = None,
    strict: Annotated[bool, typer.Option("--strict", help="Treat warnings as failures for the exit code.")] = False,
    suffix: Annotated[
        str | None,
        typer.Option(
            "--suffix",
            help="Find scenario files in directories as test_<name>.<SUFFIX>.json and .jsonc "
            "(default: the httpchain_suffix pytest's configuration sets for these paths, else http).",
            callback=_suffix_option,
        ),
    ] = None,
) -> None:
    """Validate pytest-httpchain scenario files, given one by one or as the
    directories holding them.

    A directory is searched as pytest collects it. Reports errors and warnings
    (each with a stable HTTPCHAINxxx diagnostic code) per file and exits
    non-zero if any file is invalid (or, with --strict, has any warnings), or a
    directory holds no scenario file.
    """
    try:
        results = validate_paths(paths, suffix=suffix, ref_parent_traversal_depth=ref_parent_traversal_depth, root_path=root_path, deep=deep, syspaths=list(syspath or []))
    except DiscoveryError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from e

    def passed(result: ValidateResult) -> bool:
        return result.valid and not (strict and result.warnings)

    all_passed = all(passed(result) for _, result in results)

    if output_format is OutputFormat.json:
        # Top-level `valid` is the gate result and matches the exit code; each
        # file's own `valid` is pure validity.
        payload = {
            "valid": all_passed,
            "strict": strict,
            "files": [{"path": str(path), "result": result.model_dump()} for path, result in results],
        }
        typer.echo(json.dumps(payload, indent=2, default=str))
    else:
        for path, result in results:
            if not result.valid:
                status = "INVALID"
            elif result.warnings:
                status = "FAILED (warnings)" if strict else "OK with warnings"
            else:
                status = "OK"
            typer.echo(f"{path}: {status}")
            for diagnostic in result.diagnostics:
                # Some diagnostics already name the location inside the
                # message; repeating it makes the suffix look untrustworthy
                # everywhere else. The field itself stays — it is the
                # machine-readable form of what the message spells out in prose,
                # so a JSON consumer can route on it without parsing English.
                at = f" (at {diagnostic.location})" if diagnostic.location and diagnostic.location not in diagnostic.message else ""
                typer.echo(f"  {diagnostic.severity} [{diagnostic.code}]: {diagnostic.message}{at}")
        if summary := _summary(results):
            typer.echo(summary)

    raise typer.Exit(0 if all_passed else 1)


@app.command()
def schema() -> None:
    """Emit the JSON Schema for scenario files (editor autocomplete/validation).

    Writes to stdout; redirect to a file (``pytest-httpchain schema > scenario.schema.json``).
    """
    typer.echo(json.dumps(build_schema(), indent=2, default=str))


@app.command()
def resolve(
    scenario: Annotated[Path, typer.Argument(help="Scenario JSON file to resolve.")],
    ref_parent_traversal_depth: RefParentTraversalDepth = 3,
    root_path: RootPath = None,
) -> None:
    """Resolve $ref/$include/$merge and print the merged scenario JSON to stdout.

    The output is strict JSON: the comments and trailing commas a scenario or
    an included file may hold are not in it.
    """
    try:
        # The loader load_scenario uses, so the printed document is what
        # collection sees.
        data = load_scenario_json(scenario, root_path=root_path, ref_parent_traversal_depth=ref_parent_traversal_depth)
    except (ReferenceResolverError, json.JSONDecodeError, OSError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from e

    typer.echo(json.dumps(data, indent=2, default=str))


def _load_for_inspection(path: Path, depth: int, root_path: Path | None = None) -> tuple[Scenario, dict]:
    """Load + validate a scenario for show/graph, mapping failures to Exit(1)."""
    try:
        return load_scenario(path, root_path=root_path, ref_parent_traversal_depth=depth)
    except (ReferenceResolverError, json.JSONDecodeError, OSError) as e:
        typer.echo(f"error: cannot load {path}: {e}", err=True)
        raise typer.Exit(1) from e
    except ValidationError:
        typer.echo(f"error: {path} is not a valid scenario — run `pytest-httpchain validate {path}` for details", err=True)
        raise typer.Exit(1) from None


def _render_show_text(path: Path, scenario: Scenario, flow: DataFlow) -> list[str]:
    # Listed nearest first: a name has several producers when the nearer ones
    # have a skip_if (analyze_dataflow), each read only when the nearer skipped.
    producers_of: dict[tuple[int, str], list[int]] = {}
    for edge in flow.edges:
        for var_name in edge.vars:
            producers_of.setdefault((edge.consumer, var_name), []).append(edge.producer)

    all_fixtures = sorted({*flow.scenario_fixtures, *(f for s in flow.stages for f in s.fixtures)})
    lines: list[str] = [scenario.description or path.name]
    summary = f"{len(flow.stages)} stage(s)"
    if all_fixtures:
        summary += f" · fixtures: {', '.join(all_fixtures)}"
    if flow.scenario_vars:
        summary += f" · vars: {', '.join(flow.scenario_vars)}"
    lines.append(summary)
    lines.append("")

    for s in flow.stages:
        name = s.name or f"(stage {s.index + 1})"
        lines.append(f"{s.index + 1} · {name}    {s.method} {s.url}")
        if s.skip_if is not False:
            lines.append(f"    skip_if:  {s.skip_if.strip() if isinstance(s.skip_if, str) else 'true'}")
        if s.saves:
            lines.append(f"    saves:    {', '.join(s.saves)}")
        if s.consumes:
            parts: list[str] = []
            for var_name in s.consumes:
                # analyze_dataflow lists a consume only when an earlier stage
                # saved the name, so every one has a producing edge.
                sources = [f"#{p + 1} {flow.stages[p].name or f'stage {p + 1}'}" for p in sorted(producers_of[(s.index, var_name)], reverse=True)]
                parts.append(f"{var_name} (from {', else '.join(sources)})")
            lines.append(f"    consumes: {', '.join(parts)}")
        if s.marks:
            lines.append(f"    marks:    {', '.join(s.marks)}")
    return lines


@app.command()
def show(
    scenario: Annotated[Path, typer.Argument(help="Scenario JSON file to summarize.")],
    output_format: OutputFormatOption = OutputFormat.text,
    ref_parent_traversal_depth: RefParentTraversalDepth = 3,
    root_path: RootPath = None,
) -> None:
    """Summarize a scenario's stages and variable data-flow."""
    sc, test_data = _load_for_inspection(scenario, ref_parent_traversal_depth, root_path)
    flow = analyze_dataflow(sc, test_data)

    if output_format is OutputFormat.json:
        payload = flow.model_dump()
        payload["description"] = sc.description or None
        typer.echo(json.dumps(payload, indent=2, default=str))
    else:
        for line in _render_show_text(scenario, sc, flow):
            typer.echo(line)


def _mermaid_label(text: str) -> str:
    return text.replace('"', "'").replace("\n", " ")


def _to_mermaid(flow: DataFlow, direction: str = "TD") -> str:
    lines = [f"flowchart {direction}"]
    if not flow.stages:
        lines.append("    %% (no stages)")
        return "\n".join(lines)
    for s in flow.stages:
        label = f"{s.index + 1} · {s.name}" if s.name else f"{s.index + 1}"
        lines.append(f'    S{s.index}["{_mermaid_label(label)}"]')
    for edge in flow.edges:
        # Dotted from a stage that may skip (skip_if): the values may come from
        # an earlier producer instead (its own edge), or never.
        arrow = "-->" if flow.stages[edge.producer].skip_if is False else "-.->"
        lines.append(f"    S{edge.producer} {arrow}|{', '.join(edge.vars)}| S{edge.consumer}")
    return "\n".join(lines)


@app.command()
def graph(
    scenario: Annotated[Path, typer.Argument(help="Scenario JSON file to graph.")],
    direction: Annotated[GraphDirection, typer.Option("--direction", help="Flowchart orientation.")] = GraphDirection.TD,
    ref_parent_traversal_depth: RefParentTraversalDepth = 3,
    root_path: RootPath = None,
) -> None:
    """Emit a Mermaid flowchart of the stage data-flow."""
    sc, test_data = _load_for_inspection(scenario, ref_parent_traversal_depth, root_path)
    flow = analyze_dataflow(sc, test_data)
    typer.echo(_to_mermaid(flow, direction.value))


if __name__ == "__main__":
    app()
