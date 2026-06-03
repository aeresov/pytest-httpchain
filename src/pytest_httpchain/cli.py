from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer()

SKILL_FILE = Path(__file__).parent / "skill.md"


@app.command()
def validate(
    paths: Annotated[list[Path], typer.Argument(help="Scenario JSON file(s) to validate.")],
    ref_parent_traversal_depth: Annotated[int, typer.Option(help="Maximum $ref parent directory traversal depth.")] = 3,
) -> None:
    """Validate pytest-httpchain scenario file(s).

    Prints errors and warnings per file and exits non-zero if any file is invalid.
    """
    from pytest_httpchain.validation import validate_scenario

    all_valid = True
    for path in paths:
        result = validate_scenario(path, ref_parent_traversal_depth=ref_parent_traversal_depth)
        if result.valid:
            status = "OK with warnings" if result.warnings else "OK"
        else:
            status = "INVALID"
            all_valid = False
        typer.echo(f"{path}: {status}")
        for error in result.errors:
            typer.echo(f"  error: {error}")
        for warning in result.warnings:
            typer.echo(f"  warning: {warning}")

    raise typer.Exit(0 if all_valid else 1)


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
