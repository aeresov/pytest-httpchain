# pytest-http MCP Server

This document describes the Model Context Protocol (MCP) server for the pytest-http library, which enables Cursor and other MCP-compatible editors to assist with generating and validating HTTP test scenarios.

## Features

The MCP server provides the following tools to help with pytest-http test scenarios:

### Test Generation Tools

1. **generate_basic_test** - Create a basic test scenario with optional HTTP request
2. **generate_multistage_test** - Create multi-stage test scenarios with variable passing
3. **generate_crud_test** - Generate complete CRUD (Create, Read, Update, Delete) test scenarios
4. **create_test_template** - Generate templates for common test patterns

### Validation Tools

5. **validate_test_scenario** - Validate test scenarios against pytest-http schema
6. **optimize_test_scenario** - Suggest improvements for test scenarios

### JMESPath Tools

7. **validate_jmespath** - Validate JMESPath expressions used in `save` fields
8. **test_jmespath** - Test JMESPath expressions against sample data
9. **suggest_jmespath** - Get suggestions for JMESPath expressions based on data structure

## Installation

1. Install the pytest-http library with MCP dependencies:
   ```bash
   uv add --group mcp mcp
   # or
   pip install mcp
   ```

2. Make sure the MCP server script is executable:
   ```bash
   chmod +x mcp_server.py
   ```

## Configuration for Cursor

Add the following configuration to your Cursor MCP settings (typically in `~/.cursor/mcp_config.json`):

```json
{
  "mcpServers": {
    "pytest-http": {
      "command": "uv",
      "args": ["run", "--group", "mcp", "python", "mcp_server.py"],
      "env": {}
    }
  }
}
```

Alternatively, if you have the package installed globally:

```json
{
  "mcpServers": {
    "pytest-http": {
      "command": "pytest-http-mcp",
      "args": [],
      "env": {}
    }
  }
}
```

## Usage Examples

### Generating a Basic Test

Use the `generate_basic_test` tool:
```
Tool: generate_basic_test
Arguments: {"name": "test_api_endpoint", "url": "https://api.example.com/users"}
```

This will generate:
```json
{
  "stages": [
    {
      "name": "test_api_endpoint",
      "data": {},
      "url": "https://api.example.com/users",
      "headers": {"Accept": "application/json"}
    }
  ]
}
```

### Creating a CRUD Test

Use the `generate_crud_test` tool:
```
Tool: generate_crud_test
Arguments: {"base_url": "https://api.example.com", "resource": "users"}
```

This generates a complete CRUD test with create, read, update, and delete operations.

### Validating a Test Scenario

Use the `validate_test_scenario` tool:
```
Tool: validate_test_scenario
Arguments: {"scenario": {"stages": [{"name": "test", "data": {}}]}}
```

### Testing JMESPath Expressions

Use the `test_jmespath` tool:
```
Tool: test_jmespath
Arguments: {
  "expression": "json.users[0].id",
  "data": {"json": {"users": [{"id": 1, "name": "John"}]}}
}
```

## Available Resources

The MCP server also provides these resources:

1. **schema://pytest-http/scenario** - JSON schema for test scenarios
2. **docs://pytest-http/examples** - Example test scenarios

## Test Scenario Format

pytest-http test scenarios are JSON files with the following structure:

```json
{
  "fixtures": ["fixture1", "fixture2"],  // Optional pytest fixtures
  "marks": ["slow", "integration"],      // Optional pytest marks
  "stages": [                            // Required: test stages
    {
      "name": "stage_name",              // Required: stage name
      "data": {},                        // Required: stage data
      "url": "https://api.example.com",  // Optional: HTTP request URL
      "params": {"key": "value"},        // Optional: query parameters
      "headers": {"Accept": "application/json"}, // Optional: headers
      "save": {                          // Optional: variables to save
        "var_name": "jmespath_expression"
      }
    }
  ]
}
```

## Variable Substitution

Variables saved in one stage can be used in subsequent stages:

```json
{
  "stages": [
    {
      "name": "create_user",
      "url": "https://api.example.com/users",
      "data": {"name": "Test User"},
      "save": {"user_id": "json.id"}
    },
    {
      "name": "get_user",
      "url": "https://api.example.com/users/$user_id",
      "headers": {"Accept": "application/json"}
    }
  ]
}
```

## JMESPath Expressions

The `save` field uses JMESPath expressions to extract values from HTTP responses:

- `json` - The JSON response body
- `text` - The raw response text
- `headers` - Response headers dictionary
- `status_code` - HTTP status code
- `json.field` - Access a field in the JSON response
- `length(@)` - Get array length
- `[0]` - Get first element of array
- `[*].id` - Get all id fields from array elements

## Troubleshooting

1. **Import Errors**: Make sure MCP dependencies are installed:
   ```bash
   uv add --group mcp mcp
   ```

2. **Command Not Found**: Ensure the script path is correct in your MCP configuration

3. **Validation Errors**: Use the `validate_test_scenario` tool to check your test scenarios

4. **JMESPath Issues**: Use `validate_jmespath` and `test_jmespath` tools to debug expressions

## Contributing

The MCP server is part of the pytest-http library. To contribute:

1. Fork the repository
2. Make your changes to `pytest_http/mcp_server.py`
3. Add tests if needed
4. Submit a pull request

For issues specifically with the MCP server, please include your MCP configuration and any error messages.