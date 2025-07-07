# pytest-http MCP Server Integration with Cursor

This guide explains how to integrate the pytest-http MCP server with Cursor to enable AI-assisted HTTP test creation and management.

## What is MCP?

Model Context Protocol (MCP) is a standardized way for AI tools to connect with external data sources and tools. The pytest-http MCP server exposes pytest-http functionality to AI tools like Cursor, allowing you to:

- Create HTTP test files with AI assistance
- Validate test files automatically
- Run tests directly from Cursor
- Access documentation and examples
- List and manage test files

## Quick Setup

### 1. Start the MCP Server

```bash
# Development mode with MCP Inspector (recommended for testing)
uv run mcp dev mcp_server.py

# Production mode (for actual Cursor integration)
uv run python mcp_server.py
```

### 2. Configure Cursor

Add this configuration to your Cursor settings (adjust the path to your project):

```json
{
  "mcpServers": {
    "pytest-http": {
      "command": "uv",
      "args": ["run", "python", "mcp_server.py"],
      "cwd": "/path/to/your/pytest-http-project",
      "description": "pytest-http MCP Server - HTTP test management"
    }
  }
}
```

Alternative configurations:

#### Using absolute path to script
```json
{
  "mcpServers": {
    "pytest-http": {
      "command": "python",
      "args": ["/absolute/path/to/mcp_server.py"],
      "env": {
        "PYTHONPATH": "/absolute/path/to/project"
      }
    }
  }
}
```

#### Using installed package
```json
{
  "mcpServers": {
    "pytest-http": {
      "command": "python",
      "args": ["-m", "pytest_http.mcp_server"]
    }
  }
}
```

## Available MCP Tools

### `validate_test_file`
Validates pytest-http JSON test file content.

**Usage**: "Validate this HTTP test file for me"

### `create_test_template`
Creates test templates (basic, http, multistage).

**Usage**: "Create a new HTTP test template for testing user registration"

### `list_test_files`
Lists all pytest-http test files in a directory.

**Usage**: "Show me all the HTTP test files in this project"

### `run_pytest_http_test`
Executes specific test files.

**Usage**: "Run the user authentication HTTP test"

### `write_test_file`
Creates new test files with validation.

**Usage**: "Create a test file for the login API endpoint"

### `get_test_documentation`
Gets comprehensive format documentation.

**Usage**: "Show me the documentation for pytest-http format"

## Available MCP Resources

### Schemas
- `schema://pytest-http/stage` - JSON schema for test stages
- `schema://pytest-http/scenario` - JSON schema for test scenarios

### Examples
- `examples://pytest-http/basic` - Basic test example
- `examples://pytest-http/http-request` - HTTP request example
- `examples://pytest-http/multistage` - Multi-stage test example

## Using with Cursor

Once configured, you can interact with pytest-http through natural language commands in Cursor:

### Creating Tests

**Prompt**: "Create a new HTTP test that calls the JSONPlaceholder API to get user 1 and extracts their name and email"

Cursor will use the MCP tools to:
1. Create a proper test template
2. Fill in the API details
3. Add JMESPath expressions for data extraction
4. Validate the test file
5. Save it with the correct naming convention

### Validating Tests

**Prompt**: "Check if this test file is valid and fix any errors"

Cursor will:
1. Use the validation tool to check the test
2. Report any errors found
3. Suggest fixes for common issues
4. Apply fixes if requested

### Running Tests

**Prompt**: "Run all the HTTP tests in the tests/api directory"

Cursor will:
1. List available test files
2. Execute them using pytest
3. Report the results
4. Highlight any failures

### Getting Help

**Prompt**: "Show me how to create a multi-stage HTTP test with variable substitution"

Cursor will:
1. Access the documentation resource
2. Provide examples of multi-stage tests
3. Explain variable substitution syntax
4. Offer to create a template

## Example Workflows

### 1. API Testing Workflow

```
User: "I need to test a user registration and login flow"

Cursor will:
1. Create a multistage test template
2. Add stages for registration and login
3. Set up variable passing between stages
4. Include proper assertions and data extraction
```

### 2. Validation Workflow

```
User: "This test isn't working, can you check what's wrong?"

Cursor will:
1. Validate the test file structure
2. Check for syntax errors
3. Verify required fields
4. Suggest corrections
```

### 3. Test Management Workflow

```
User: "Show me all failing tests and help me fix them"

Cursor will:
1. List all test files
2. Run them to identify failures
3. Analyze error messages
4. Suggest fixes for common issues
```

## Troubleshooting

### MCP Server Not Starting

1. Check that all dependencies are installed:
   ```bash
   uv add "mcp[cli]"
   ```

2. Verify the server starts manually:
   ```bash
   uv run python mcp_server.py
   ```

3. Check the Python path in your Cursor configuration

### Cursor Not Connecting

1. Verify the MCP server configuration in Cursor settings
2. Check that the command and arguments are correct
3. Ensure the working directory is set properly
4. Look at Cursor's developer console for error messages

### Tool Execution Errors

1. Ensure pytest-http is properly installed
2. Check that test files follow the correct naming convention
3. Verify file permissions for reading/writing test files

## Advanced Usage

### Custom Test Types

You can extend the MCP server by adding more test templates or custom validation rules. The server is designed to be easily extensible.

### Integration with CI/CD

The MCP server can be used in CI/CD pipelines by calling the tools programmatically:

```python
from pytest_http.mcp_server import validate_test_file, run_pytest_http_test

# Validate all test files
# Run specific tests
# Generate reports
```

### Batch Operations

Use Cursor to perform batch operations on multiple test files:

**Prompt**: "Validate all HTTP test files in the project and create a summary report"

## Best Practices

1. **Use descriptive test names**: This helps Cursor understand the context
2. **Keep tests focused**: Single responsibility per test stage
3. **Use meaningful variable names**: Makes debugging easier
4. **Include documentation**: Add descriptions to complex tests
5. **Regular validation**: Check tests periodically for issues

## Support

For issues with the MCP integration:
1. Check the MCP Inspector for tool testing
2. Verify server logs for error messages
3. Test tools individually before using with Cursor
4. Review the pytest-http documentation for test format details