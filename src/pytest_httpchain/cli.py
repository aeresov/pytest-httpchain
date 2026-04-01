import json
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer()

SKILL_FILE = Path(__file__).parent / "skill.md"

MCP_SERVER_CONFIG = {
    "command": "uvx",
    "args": ["pytest-httpchain", "mcp"],
}


@app.command()
def mcp() -> None:
    """Run the MCP server."""
    from pytest_httpchain_mcp.server import mcp as server

    server.run()


@app.command()
def install(
    skill: bool = typer.Option(..., "--skill/--no-skill", "-s/-S", help="Install Claude Code skill"),
    mcp_config: bool = typer.Option(..., "--mcp/--no-mcp", "-m/-M", help="Install MCP server config"),
    global_: bool = typer.Option(False, "--global", "-g", help="Install to ~/.claude (personal scope) instead of project"),
    project_dir: Annotated[Path, typer.Option(help="Project directory (ignored with --global)")] = Path("."),
) -> None:
    """Install MCP server config and/or Claude Code skill."""
    if global_:
        if skill:
            _install_skill(Path.home() / ".claude" / "skills" / "pytest-httpchain")
        if mcp_config:
            typer.echo("To add the MCP server globally, run:")
            typer.echo("  claude mcp add --scope user pytest-httpchain -- uvx pytest-httpchain mcp")
    else:
        project_dir = project_dir.resolve()
        if skill:
            _install_skill(project_dir / ".claude" / "skills" / "pytest-httpchain")
        if mcp_config:
            _install_mcp_config(project_dir / ".mcp.json")


def _install_skill(skill_dir: Path) -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    dest = skill_dir / "SKILL.md"
    dest.write_text(SKILL_FILE.read_text())
    typer.echo(f"Installed skill to {dest}")


def _install_mcp_config(mcp_file: Path) -> None:
    if mcp_file.exists():
        config = json.loads(mcp_file.read_text())
    else:
        config = {}

    config.setdefault("mcpServers", {})
    config["mcpServers"]["pytest-httpchain"] = MCP_SERVER_CONFIG
    mcp_file.write_text(json.dumps(config, indent=2) + "\n")
    typer.echo(f"Installed MCP server config to {mcp_file}")


if __name__ == "__main__":
    app()
