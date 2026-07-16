import enum
import json
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from pytest_httpchain.dataflow import DataFlow, analyze_dataflow
from pytest_httpchain.jsonref import ReferenceResolverError, load_json
from pytest_httpchain.models import Scenario
from pytest_httpchain.schema import build_schema
from pytest_httpchain.validation import ValidateResult, load_scenario, resolve_root_path, validate_scenario

app = typer.Typer()


class OutputFormat(enum.StrEnum):
    text = "text"
    json = "json"


class GraphDirection(enum.StrEnum):
    TD = "TD"
    LR = "LR"


# Shared $ref traversal-depth option, reused by validate/resolve/show/graph so the
# help text and default stay in one place.
RefParentTraversalDepth = Annotated[int, typer.Option(help="Maximum $ref parent directory traversal depth.")]


@app.callback()
def main() -> None:
    """pytest-httpchain command-line tools."""


@app.command()
def validate(
    paths: Annotated[list[Path], typer.Argument(help="Scenario JSON file(s) to validate.")],
    ref_parent_traversal_depth: RefParentTraversalDepth = 3,
    root_path: Annotated[Path | None, typer.Option("--root-path", help="Directory that constrains $ref resolution (default: auto-detected project root).")] = None,
    output_format: Annotated[OutputFormat, typer.Option("--format", help="Output format: human-readable text or machine-readable JSON.")] = OutputFormat.text,
    deep: Annotated[bool, typer.Option("--deep", help="Run deep checks: resolve user-function imports/signatures and referenced files. Imports user modules.")] = False,
    syspath: Annotated[list[Path] | None, typer.Option("--syspath", help="Extra directories to add to sys.path for --deep import resolution (repeatable).")] = None,
    strict: Annotated[bool, typer.Option("--strict", help="Treat warnings as failures for the exit code.")] = False,
) -> None:
    """Validate pytest-httpchain scenario file(s).

    Reports errors and warnings (each with a stable HTTPCHAINxxx diagnostic code)
    per file and exits non-zero if any file is invalid (or, with --strict, has any
    warnings).
    """
    results = [
        (path, validate_scenario(path, ref_parent_traversal_depth=ref_parent_traversal_depth, root_path=root_path, deep=deep, syspaths=list(syspath or []))) for path in paths
    ]

    def passed(result: ValidateResult) -> bool:
        return result.valid and not (strict and result.warnings)

    all_passed = all(passed(result) for _, result in results)

    if output_format is OutputFormat.json:
        payload = {
            "valid": all_passed,
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
                typer.echo(f"  {diagnostic.severity} [{diagnostic.code}]: {diagnostic.message}")

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
    root_path: Annotated[Path | None, typer.Option("--root-path", help="Directory that constrains $ref resolution (default: auto-detected project root).")] = None,
) -> None:
    """Resolve $ref/$include/$merge and print the merged scenario JSON to stdout."""
    try:
        data = load_json(
            scenario,
            max_parent_traversal_depth=ref_parent_traversal_depth,
            root_path=root_path or resolve_root_path(scenario),
        )
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
    producer_of: dict[tuple[int, str], int] = {}
    for edge in flow.edges:
        for var_name in edge.vars:
            producer_of[(edge.consumer, var_name)] = edge.producer

    all_fixtures = sorted({*flow.scenario_fixtures, *(f for s in flow.stages for f in s.fixtures)})
    lines: list[str] = [scenario.description or path.name]
    summary = f"{len(flow.stages)} stage(s)"
    if all_fixtures:
        summary += f" · fixtures: {all_fixtures}"
    if flow.scenario_vars:
        summary += f" · vars: {flow.scenario_vars}"
    lines.append(summary)
    lines.append("")

    for s in flow.stages:
        name = s.name or f"(stage {s.index + 1})"
        lines.append(f"{s.index + 1} · {name}    {s.method} {s.url}")
        if s.saves:
            lines.append(f"    saves:    {', '.join(s.saves)}")
        if s.consumes:
            parts: list[str] = []
            for var_name in s.consumes:
                producer = producer_of.get((s.index, var_name))
                if producer is None:
                    parts.append(var_name)
                else:
                    producer_name = flow.stages[producer].name or f"stage {producer + 1}"
                    parts.append(f"{var_name} (from #{producer + 1} {producer_name})")
            lines.append(f"    consumes: {', '.join(parts)}")
        if s.marks:
            lines.append(f"    marks:    {', '.join(s.marks)}")
    return lines


@app.command()
def show(
    scenario: Annotated[Path, typer.Argument(help="Scenario JSON file to summarize.")],
    output_format: Annotated[OutputFormat, typer.Option("--format", help="Output format: human-readable text or machine-readable JSON.")] = OutputFormat.text,
    ref_parent_traversal_depth: RefParentTraversalDepth = 3,
    root_path: Annotated[Path | None, typer.Option("--root-path", help="Directory that constrains $ref resolution (default: auto-detected project root).")] = None,
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
        lines.append(f"    S{edge.producer} -->|{', '.join(edge.vars)}| S{edge.consumer}")
    return "\n".join(lines)


@app.command()
def graph(
    scenario: Annotated[Path, typer.Argument(help="Scenario JSON file to graph.")],
    direction: Annotated[GraphDirection, typer.Option("--direction", help="Flowchart orientation.")] = GraphDirection.TD,
    ref_parent_traversal_depth: RefParentTraversalDepth = 3,
    root_path: Annotated[Path | None, typer.Option("--root-path", help="Directory that constrains $ref resolution (default: auto-detected project root).")] = None,
) -> None:
    """Emit a Mermaid flowchart of the stage data-flow."""
    sc, test_data = _load_for_inspection(scenario, ref_parent_traversal_depth, root_path)
    flow = analyze_dataflow(sc, test_data)
    typer.echo(_to_mermaid(flow, direction.value))


if __name__ == "__main__":
    app()
