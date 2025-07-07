import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ValidationError

from pytest_http.models import Scenario, Stage

# Initialize MCP server
mcp = FastMCP("pytest-http")

class HTTPTestFileInfo(BaseModel):
    """Information about a test file"""
    path: str
    name: str
    valid: bool
    error: str | None = None
    stages_count: int = 0
    fixtures: List[str] = []
    marks: List[str] = []

class HTTPTestValidationResult(BaseModel):
    """Result of validating a test file"""
    valid: bool
    error: str | None = None
    scenario: Dict[str, Any] | None = None

class HTTPTestExecutionResult(BaseModel):
    """Result of executing a test"""
    success: bool
    output: str
    error: str | None = None
    exit_code: int

@mcp.resource("schema://pytest-http/stage")
def get_stage_schema() -> str:
    """Get the JSON schema for a pytest-http stage"""
    return """
{
  "type": "object",
  "properties": {
    "name": {
      "type": "string",
      "description": "Name of the test stage"
    },
    "data": {
      "description": "Test data for the stage"
    },
    "url": {
      "type": "string",
      "description": "HTTP URL to request (optional)"
    },
    "params": {
      "type": "object",
      "description": "HTTP query parameters"
    },
    "headers": {
      "type": "object",
      "description": "HTTP headers"
    },
    "save": {
      "type": "object",
      "description": "Variables to save from response using JMESPath expressions",
      "patternProperties": {
        "^[a-zA-Z_][a-zA-Z0-9_]*$": {
          "type": "string",
          "description": "JMESPath expression to extract value"
        }
      }
    }
  },
  "required": ["name", "data"]
}
"""

@mcp.resource("schema://pytest-http/scenario")
def get_scenario_schema() -> str:
    """Get the JSON schema for a pytest-http test scenario"""
    return """
{
  "type": "object",
  "properties": {
    "fixtures": {
      "type": "array",
      "items": {"type": "string"},
      "description": "List of pytest fixture names to use"
    },
    "marks": {
      "type": "array", 
      "items": {"type": "string"},
      "description": "List of pytest marks to apply"
    },
    "stages": {
      "type": "array",
      "items": {"$ref": "#/definitions/stage"},
      "description": "List of test stages to execute"
    }
  },
  "required": ["stages"],
  "definitions": {
    "stage": {
      "type": "object",
      "properties": {
        "name": {"type": "string"},
        "data": {},
        "url": {"type": "string"},
        "params": {"type": "object"},
        "headers": {"type": "object"},
        "save": {
          "type": "object",
          "patternProperties": {
            "^[a-zA-Z_][a-zA-Z0-9_]*$": {"type": "string"}
          }
        }
      },
      "required": ["name", "data"]
    }
  }
}
"""

@mcp.resource("examples://pytest-http/basic")
def get_basic_example() -> str:
    """Get a basic pytest-http test example"""
    return json.dumps({
        "stages": [
            {
                "name": "basic_test",
                "data": {"message": "Hello, World!"}
            }
        ]
    }, indent=2)

@mcp.resource("examples://pytest-http/http-request")
def get_http_example() -> str:
    """Get an HTTP request pytest-http test example"""
    return json.dumps({
        "stages": [
            {
                "name": "api_test",
                "url": "https://jsonplaceholder.typicode.com/posts/1",
                "headers": {
                    "Accept": "application/json"
                },
                "data": {},
                "save": {
                    "post_id": "json.id",
                    "post_title": "json.title"
                }
            }
        ]
    }, indent=2)

@mcp.resource("examples://pytest-http/multistage")
def get_multistage_example() -> str:
    """Get a multistage pytest-http test example"""
    return json.dumps({
        "fixtures": ["base_url"],
        "stages": [
            {
                "name": "get_user_list",
                "url": "$base_url/users",
                "data": {},
                "save": {
                    "first_user_id": "[0].id"
                }
            },
            {
                "name": "get_user_details", 
                "url": "$base_url/users/$first_user_id",
                "data": {},
                "save": {
                    "user_email": "json.email"
                }
            }
        ]
    }, indent=2)

@mcp.tool()
def validate_test_file(file_content: str) -> HTTPTestValidationResult:
    """Validate a pytest-http JSON test file content"""
    try:
        # Parse JSON
        data = json.loads(file_content)
        
        # Validate with Pydantic model
        scenario = Scenario.model_validate(data)
        
        return HTTPTestValidationResult(
            valid=True,
            scenario=scenario.model_dump()
        )
    except json.JSONDecodeError as e:
        return HTTPTestValidationResult(
            valid=False,
            error=f"Invalid JSON: {e}"
        )
    except ValidationError as e:
        return HTTPTestValidationResult(
            valid=False,
            error=f"Validation error: {e}"
        )
    except Exception as e:
        return HTTPTestValidationResult(
            valid=False,
            error=f"Unexpected error: {e}"
        )

@mcp.tool()
def create_test_template(test_name: str, test_type: str = "basic") -> str:
    """Create a pytest-http test file template"""
    templates = {
        "basic": {
            "stages": [
                {
                    "name": f"{test_name}_stage",
                    "data": {"description": f"Test data for {test_name}"}
                }
            ]
        },
        "http": {
            "stages": [
                {
                    "name": f"{test_name}_request",
                    "url": "https://example.com/api/endpoint",
                    "headers": {
                        "Accept": "application/json"
                    },
                    "data": {},
                    "save": {
                        "response_id": "json.id"
                    }
                }
            ]
        },
        "multistage": {
            "fixtures": ["api_base_url"],
            "stages": [
                {
                    "name": "setup_stage",
                    "data": {"setup": True}
                },
                {
                    "name": "main_test",
                    "url": "$api_base_url/test",
                    "data": {},
                    "save": {
                        "test_result": "json.result"
                    }
                },
                {
                    "name": "cleanup_stage", 
                    "data": {"cleanup": True}
                }
            ]
        }
    }
    
    template = templates.get(test_type, templates["basic"])
    return json.dumps(template, indent=2)

@mcp.tool()
def list_test_files(directory: str = "tests") -> List[HTTPTestFileInfo]:
    """List all pytest-http test files in a directory"""
    test_files = []
    test_dir = Path(directory)
    
    if not test_dir.exists():
        return test_files
    
    # Find all .http.json files
    for file_path in test_dir.rglob("test_*.http.json"):
        try:
            content = file_path.read_text()
            data = json.loads(content)
            scenario = Scenario.model_validate(data)
            
            test_files.append(HTTPTestFileInfo(
                path=str(file_path),
                name=file_path.stem.replace("test_", ""),
                valid=True,
                stages_count=len(scenario.stages),
                fixtures=scenario.fixtures,
                marks=scenario.marks
            ))
        except Exception as e:
            test_files.append(HTTPTestFileInfo(
                path=str(file_path),
                name=file_path.stem.replace("test_", ""),
                valid=False,
                error=str(e)
            ))
    
    return test_files

@mcp.tool()
def run_pytest_http_test(file_path: str, verbose: bool = True) -> HTTPTestExecutionResult:
    """Run a specific pytest-http test file"""
    try:
        cmd = ["python", "-m", "pytest"]
        if verbose:
            cmd.append("-v")
        cmd.append(file_path)
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=Path.cwd()
        )
        
        return HTTPTestExecutionResult(
            success=result.returncode == 0,
            output=result.stdout,
            error=result.stderr if result.stderr else None,
            exit_code=result.returncode
        )
    except Exception as e:
        return HTTPTestExecutionResult(
            success=False,
            output="",
            error=str(e),
            exit_code=-1
        )

@mcp.tool()
def write_test_file(file_path: str, content: str) -> Dict[str, Any]:
    """Write a pytest-http test file after validation"""
    try:
        # Validate content first
        validation = validate_test_file(content)
        if not validation.valid:
            return {
                "success": False,
                "error": f"Invalid test content: {validation.error}"
            }
        
        # Ensure .http.json extension
        path = Path(file_path)
        if not file_path.endswith('.http.json'):
            path = path.with_suffix('.http.json')
        
        # Ensure test_ prefix
        if not path.name.startswith('test_'):
            path = path.with_name(f"test_{path.name}")
        
        # Create parent directories if needed
        path.parent.mkdir(parents=True, exist_ok=True)
        
        # Write file
        path.write_text(content)
        
        return {
            "success": True,
            "file_path": str(path),
            "message": f"Test file written successfully to {path}"
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }

@mcp.tool()
def get_test_documentation() -> str:
    """Get comprehensive documentation for pytest-http test format"""
    return """
# pytest-http Test Format Documentation

pytest-http allows you to write HTTP tests in JSON format that are automatically discovered and executed by pytest.

## File Naming Convention
Test files must follow the pattern: `test_<name>.http.json`

## Test Structure

### Basic Structure
```json
{
  "fixtures": ["fixture1", "fixture2"],  // Optional: pytest fixtures to use
  "marks": ["mark1", "mark2"],           // Optional: pytest marks to apply
  "stages": [                            // Required: list of test stages
    {
      "name": "stage_name",              // Required: stage name
      "data": {},                        // Required: test data
      "url": "https://api.example.com",  // Optional: HTTP URL to request
      "params": {"key": "value"},        // Optional: query parameters
      "headers": {"Accept": "application/json"}, // Optional: HTTP headers
      "save": {                          // Optional: variables to save from response
        "var_name": "jmespath.expression"
      }
    }
  ]
}
```

### Variable Substitution
- Use `"$variable_name"` for full value substitution
- Use `$variable_name` within strings for partial substitution
- Variables can come from fixtures or previous stages' saved values

### JMESPath Expressions
Used in the `save` field to extract values from HTTP responses:
- `json.field` - Extract field from JSON response
- `status_code` - HTTP status code
- `headers.HeaderName` - Response header value
- `[0].id` - First item's id from JSON array
- `length(@)` - Count of items in array

### Response Data Structure
When making HTTP requests, the following data is available for JMESPath:
```json
{
  "status_code": 200,
  "headers": {"content-type": "application/json"},
  "text": "raw response text",
  "json": {} // parsed JSON if content-type is application/json
}
```

## Examples

### Simple Test
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

### HTTP API Test
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

### Multi-stage Test with Variable Substitution
```json
{
  "fixtures": ["api_base"],
  "stages": [
    {
      "name": "create_user",
      "url": "$api_base/users",
      "data": {"name": "Test User"},
      "save": {"new_user_id": "json.id"}
    },
    {
      "name": "get_created_user",
      "url": "$api_base/users/$new_user_id",
      "data": {},
      "save": {"user_email": "json.email"}
    }
  ]
}
```
"""

# Main entry point for the MCP server
if __name__ == "__main__":
    mcp.run()