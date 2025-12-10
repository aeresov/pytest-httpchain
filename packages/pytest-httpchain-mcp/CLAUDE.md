# pytest-httpchain-mcp

MCP (Model Context Protocol) server for pytest-httpchain, enabling AI agents to validate and work with HTTP test scenarios.

## Purpose

This package provides an MCP server that exposes tools for AI code agents to:
- Validate pytest-httpchain test scenario JSON files
- Check JSON syntax, structure, and schema compliance
- Detect common issues (duplicate stages, undefined variables, fixture conflicts)
- Generate snippets based on description

## Package Structure

```
src/pytest_httpchain_mcp/
├── __init__.py     # Empty module init
├── server.py       # MCP server with FastMCP, tools defined here
└── cli.py          # CLI entry point using Click
```

## Running the MCP Server

```bash
# From monorepo root
uv run pytest-httpchain-mcp

# Or directly
uv run python -m pytest_httpchain_mcp.cli
```

## Available MCP Tools

### validate_scenario

Validates a pytest-httpchain test scenario JSON file with comprehensive checks.

**Parameters:**
- `path`: Path to the test scenario JSON file
- `ref_parent_traversal_depth`: Max depth for `$ref` parent traversals (default: 3)
- `root_path`: Root path for resolving references (auto-detected if not provided)

**Returns:** `ValidateResult` with:
- `valid`: Boolean indicating validation success
- `errors`: List of error messages
- `warnings`: List of warning messages
- `scenario_info`: Detailed scenario info (see below)

**ScenarioInfo fields:**
- `num_stages`: Number of stages in the scenario
- `stage_names`: List of stage names
- `vars_referenced`: Variables referenced in Jinja templates
- `vars_saved`: Variables saved via jmespath in response steps
- `vars_defined`: Variables defined in vars/substitutions
- `fixtures`: Pytest fixtures used

**Validation checks (errors):**
- File existence and accessibility
- JSON syntax validity
- JSONRef (`$ref`) resolution
- Schema validation against `pytest_httpchain_models.Scenario`
- Duplicate stage name detection
- Fixture/variable name conflict detection

**Validation checks (warnings):**
- Wrong file extension (not `.json`)
- Undefined variables referenced
- Stages with no response validation (no verify step)

## Running Tests

```bash
# From monorepo root
uv run pytest packages/pytest-httpchain-mcp/tests -v

# Or from package directory
cd packages/pytest-httpchain-mcp
uv run pytest tests -v
```
