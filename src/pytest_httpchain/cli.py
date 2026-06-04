import enum
import json
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer()

SKILL_FILE = Path(__file__).parent / "skill.md"


class OutputFormat(enum.StrEnum):
    text = "text"
    json = "json"


@app.command()
def validate(
    paths: Annotated[list[Path], typer.Argument(help="Scenario JSON file(s) to validate.")],
    ref_parent_traversal_depth: Annotated[int, typer.Option(help="Maximum $ref parent directory traversal depth.")] = 3,
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
    from pytest_httpchain.validation import validate_scenario

    results = [(path, validate_scenario(path, ref_parent_traversal_depth=ref_parent_traversal_depth, deep=deep, syspaths=list(syspath or []))) for path in paths]

    def passed(result) -> bool:
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
def install(
    global_: Annotated[bool, typer.Option("--global", "-g", help="Install to ~/.claude (personal scope) instead of project")] = False,
    project_dir: Annotated[Path, typer.Option(help="Project directory (ignored with --global)")] = Path("."),
) -> None:
    """Install the Claude Code skill for authoring test scenarios."""
    if global_:
        _install_skill(Path.home() / ".claude" / "skills" / "pytest-httpchain")
    else:
        _install_skill(project_dir.resolve() / ".claude" / "skills" / "pytest-httpchain")


def _install_skill(skill_dir: Path) -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    dest = skill_dir / "SKILL.md"
    dest.write_text(SKILL_FILE.read_text())
    typer.echo(f"Installed skill to {dest}")


if __name__ == "__main__":
    app()
