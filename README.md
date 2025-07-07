# pytest-http

A pytest plugin for HTTP testing using JSON files with MCP (Model Context Protocol) support for AI-powered test creation.

## Overview

`pytest-http` allows you to define HTTP test scenarios in JSON files instead of Python code. It supports:

- **JSON-based test definitions** with schema validation
- **Multi-stage test scenarios** with variable passing between stages
- **Fixture integration** with standard pytest fixtures
- **JMESPath expressions** for extracting and validating response data
- **MCP Server integration** for AI-powered test scenario creation

## Installation

```bash
# Install the plugin
uv add pytest-http

# For MCP support, also install:
uv add mcp
```

## Basic Usage

### 1. Create a JSON test file

Create a file named `test_api.http.json`:

```json
{
  "stages": [
    {
      "name": "get_users",
      "url": "https://jsonplaceholder.typicode.com/users",
      "headers": {
        "Accept": "application/json"
      },
      "save": {
        "user_count": "length(@)",
        "first_user_id": "[0].id"
      }
    },
    {
      "name": "get_specific_user", 
      "url": "https://jsonplaceholder.typicode.com/users/${first_user_id}",
      "headers": {
        "Accept": "application/json"
      },
      "save": {
        "user_email": "json.email"
      }
    }
  ]
}
```

### 2. Run the test

```bash
pytest test_api.http.json
```

## JSON Test Schema

### Basic Structure

```json
{
  "fixtures": ["fixture1", "fixture2"],  // Optional pytest fixtures
  "marks": ["slow", "integration"],      // Optional pytest marks
  "stages": [                            // Required: list of test stages
    {
      "name": "stage_name",              // Required: stage identifier
      "url": "https://api.example.com",  // Optional: HTTP endpoint
      "params": {"key": "value"},        // Optional: query parameters
      "headers": {"key": "value"},       // Optional: HTTP headers
      "data": {},                        // Optional: stage data
      "save": {                          // Optional: variables to save
        "var_name": "jmespath_expression"
      }
    }
  ]
}
```

### Variable Substitution

Variables saved in previous stages can be used in subsequent stages:

```json
{
  "stages": [
    {
      "name": "login",
      "url": "https://api.example.com/login", 
      "save": {
        "auth_token": "json.token"
      }
    },
    {
      "name": "get_profile",
      "url": "https://api.example.com/profile",
      "headers": {
        "Authorization": "Bearer ${auth_token}"
      }
    }
  ]
}
```

### Using Fixtures

```json
{
  "fixtures": ["httpserver_listen_address"],
  "stages": [
    {
      "name": "test_local_server",
      "url": "http://${httpserver_listen_address}/test"
    }
  ]
}
```

## MCP Server Integration

The plugin includes an MCP (Model Context Protocol) server that allows AI assistants like Cursor to create and manage test scenarios.

### Setup for Cursor

1. **Install MCP dependencies:**
   ```bash
   uv add mcp
   ```

2. **Add to your Cursor MCP configuration:**

   Create or update your MCP configuration file:

   **For Cursor (add to MCP settings):**
   ```json
   {
     "pytest-http": {
       "command": "uv",
       "args": [
         "run", 
         "pytest-http-mcp"
       ]
     }
   }
   ```

   **For Claude Desktop (add to `claude_desktop_config.json`):**
   ```json
   {
     "mcpServers": {
       "pytest-http": {
         "command": "uv",
         "args": [
           "run",
           "pytest-http-mcp"
         ]
       }
     }
   }
   ```

### Available MCP Tools

The MCP server provides the following tools for AI assistants:

- **`create_test_scenario`** - Create new HTTP test scenario files
- **`list_test_scenarios`** - List all existing test scenarios  
- **`validate_test_scenario`** - Validate test scenarios against the schema
- **`create_stage`** - Create individual test stage configurations
- **`generate_test_template`** - Generate templates for common test patterns

### Example AI Prompts

With the MCP server running, you can ask your AI assistant:

- *"Create a test scenario for the GitHub API that lists repositories and gets details for the first one"*
- *"Generate a multi-stage test that creates a user, logs them in, and fetches their profile"*
- *"List all existing HTTP test scenarios in this project"*
- *"Create a basic API test template"*

## Configuration

### pytest.ini settings

```ini
[tool:pytest]
# Change the file suffix (default: http)
suffix = api

# This would look for test_*.api.json files instead
```

### pyproject.toml settings

```toml
[tool.pytest.ini_options]
# Custom suffix for test files
suffix = "http"
```

## Advanced Features

### JMESPath Expressions

Use JMESPath to extract data from HTTP responses:

```json
{
  "save": {
    "user_emails": "[*].email",
    "first_name": "[0].name", 
    "user_count": "length(@)",
    "admin_users": "[?role=='admin'].name"
  }
}
```

### JSON References

Use `$ref` to reference shared configurations:

```json
{
  "stages": [
    {
      "name": "test_stage",
      "$ref": "common_stage.json"
    }
  ]
}
```

### Pytest Integration

HTTP tests integrate seamlessly with pytest:

```bash
# Run only HTTP tests
pytest --collect-only -q *.http.json

# Run with specific markers
pytest -m "not slow" *.http.json

# Verbose output
pytest -v test_api.http.json
```

## Examples

### REST API Testing

```json
{
  "marks": ["integration"],
  "stages": [
    {
      "name": "create_post",
      "url": "https://jsonplaceholder.typicode.com/posts",
      "data": {
        "title": "Test Post",
        "body": "This is a test",
        "userId": 1
      },
      "save": {
        "post_id": "json.id"
      }
    },
    {
      "name": "verify_post",
      "url": "https://jsonplaceholder.typicode.com/posts/${post_id}",
      "save": {
        "post_title": "json.title"
      }
    }
  ]
}
```

### Authentication Flow

```json
{
  "stages": [
    {
      "name": "get_auth_token",
      "url": "https://api.example.com/auth/login",
      "data": {
        "username": "testuser",
        "password": "testpass"
      },
      "save": {
        "access_token": "json.access_token",
        "user_id": "json.user.id"
      }
    },
    {
      "name": "access_protected_resource",
      "url": "https://api.example.com/user/${user_id}/profile",
      "headers": {
        "Authorization": "Bearer ${access_token}"
      }
    }
  ]
}
```

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes
4. Add tests for your changes
5. Run the test suite (`uv run pytest`)
6. Commit your changes (`git commit -m 'Add amazing feature'`)
7. Push to the branch (`git push origin feature/amazing-feature`)
8. Open a Pull Request

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Development

### Setup

```bash
# Clone the repository
git clone https://github.com/your-org/pytest-http.git
cd pytest-http

# Install in development mode
uv sync
uv run pytest

# Run tests
uv run pytest tests/

# Run linting
uv run ruff check
uv run ruff format
```

### Testing the MCP Server

```bash
# Test the MCP server directly
uv run python -m pytest_http.mcp_server

# Or use the CLI entry point
uv run pytest-http-mcp
```