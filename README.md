# pytest-http

A pytest plugin for HTTP testing using JSON files with Model Context Protocol (MCP) server support.

## Overview

pytest-http allows you to write HTTP tests in JSON format that are automatically discovered and executed by pytest. It supports multi-stage tests, variable substitution, and JMESPath expressions for extracting data from HTTP responses.

## Features

- Write HTTP tests in JSON format
- Multi-stage test execution with variable passing
- JMESPath expressions for response data extraction
- Variable substitution from fixtures and previous stages
- Pytest integration with fixtures and marks
- **MCP Server for AI tool integration (like Cursor)**

## Installation

```bash
# Using uv (recommended)
uv add pytest-http

# Using pip
pip install pytest-http
```

## JSON Test Format

Test files must follow the pattern: `test_<name>.http.json`

### Basic Structure

```json
{
  "fixtures": ["fixture1", "fixture2"],
  "marks": ["mark1", "mark2"],
  "stages": [
    {
      "name": "stage_name",
      "data": {},
      "url": "https://api.example.com",
      "params": {"key": "value"},
      "headers": {"Accept": "application/json"},
      "save": {
        "var_name": "jmespath.expression"
      }
    }
  ]
}
```

### Examples

#### Simple Test
```json
{
  "stages": [
    {
      "name": "basic_test",
      "data": {"message": "Hello World"}
    }
  ]
}
```

#### HTTP API Test
```json
{
  "stages": [
    {
      "name": "get_user",
      "url": "https://jsonplaceholder.typicode.com/users/1",
      "headers": {"Accept": "application/json"},
      "data": {},
      "save": {
        "user_id": "json.id",
        "user_name": "json.name"
      }
    }
  ]
}
```

## MCP Server Integration

This project includes an MCP (Model Context Protocol) server that exposes pytest-http functionality to AI tools like Cursor. The MCP server provides tools for creating, validating, and running HTTP tests.

### Available MCP Tools

1. **validate_test_file** - Validate pytest-http JSON test content
2. **create_test_template** - Generate test templates (basic, http, multistage)
3. **list_test_files** - List all test files in a directory
4. **run_pytest_http_test** - Execute specific test files
5. **write_test_file** - Create new test files with validation
6. **get_test_documentation** - Get comprehensive format documentation

### Available MCP Resources

1. **schema://pytest-http/stage** - JSON schema for test stages
2. **schema://pytest-http/scenario** - JSON schema for test scenarios
3. **examples://pytest-http/basic** - Basic test example
4. **examples://pytest-http/http-request** - HTTP request example
5. **examples://pytest-http/multistage** - Multi-stage test example

### Running the MCP Server

#### Development Mode (with MCP Inspector)

```bash
# Start the MCP Inspector for development and testing
uv run mcp dev mcp_server.py
```

This will open the MCP Inspector in your browser where you can test all the tools and resources.

#### Production Mode

```bash
# Run the server directly
uv run python mcp_server.py
```

### Integrating with Cursor

To use the MCP server with Cursor, add the following configuration:

#### Option 1: Using the provided config file

Copy the configuration from `mcp_config.json` to your Cursor settings:

```json
{
  "mcpServers": {
    "pytest-http": {
      "command": "python",
      "args": ["/path/to/your/project/mcp_server.py"],
      "env": {
        "PYTHONPATH": "/path/to/your/project"
      },
      "description": "pytest-http MCP Server - HTTP test management"
    }
  }
}
```

#### Option 2: Using uv run (recommended)

```json
{
  "mcpServers": {
    "pytest-http": {
      "command": "uv",
      "args": ["run", "python", "mcp_server.py"],
      "cwd": "/path/to/your/project",
      "description": "pytest-http MCP Server - HTTP test management"
    }
  }
}
```

#### Option 3: Install as package

```bash
# Install the package
uv add pytest-http

# Then use in Cursor config
{
  "mcpServers": {
    "pytest-http": {
      "command": "python",
      "args": ["-m", "pytest_http.mcp_server"],
      "description": "pytest-http MCP Server - HTTP test management"
    }
  }
}
```

### Using MCP Tools in Cursor

Once configured, you can use Cursor to:

1. **Create test files**: Ask Cursor to create HTTP test files, and it will use the MCP tools to generate properly formatted JSON tests
2. **Validate tests**: Cursor can validate your test files and provide feedback on errors
3. **Run tests**: Execute tests directly from Cursor using the MCP integration
4. **Get examples**: Access built-in examples and documentation through MCP resources
5. **List tests**: Browse all available test files in your project

Example prompts you can use with Cursor:
- "Create a new HTTP test that calls the JSONPlaceholder API"
- "Validate this test file and fix any errors"
- "List all the pytest-http test files in this project"
- "Run the user authentication test"
- "Show me examples of multi-stage HTTP tests"

## Running Tests

```bash
# Run all HTTP tests
pytest tests/

# Run specific test file
pytest tests/examples/test_basic.http.json

# Run with verbose output
pytest -v tests/examples/test_http_example.http.json
```

## Development

```bash
# Install development dependencies
uv add --dev responses ruff

# Run linting
uv run ruff check
uv run ruff format

# Run unit tests
uv run pytest tests/

# Test the MCP server
uv run mcp dev mcp_server.py
```

## Configuration

The plugin supports configuration through `pytest.ini` or `pyproject.toml`:

```ini
[tool.pytest.ini_options]
# Customize the file suffix (default: "http")
suffix = "http"
```

## Variable Substitution

- Use `"$variable_name"` for full value substitution
- Use `$variable_name` within strings for partial substitution
- Variables can come from fixtures or previous stages' saved values

## JMESPath Expressions

Used in the `save` field to extract values from HTTP responses:
- `json.field` - Extract field from JSON response
- `status_code` - HTTP status code
- `headers.HeaderName` - Response header value
- `[0].id` - First item's id from JSON array
- `length(@)` - Count of items in array

## License

MIT License