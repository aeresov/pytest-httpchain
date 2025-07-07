import json
import logging
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from pytest_http.models import Scenario, Stage

# Check if MCP is available, if not provide graceful degradation
try:
    import mcp.server.stdio
    import mcp.types as types
    from mcp.server import NotificationOptions, Server
    from mcp.server.models import InitializationOptions
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
    # Mock types for when MCP is not available
    class types:
        class TextContent:
            def __init__(self, type: str, text: str):
                self.type = type
                self.text = text
        class Resource:
            def __init__(self, uri: str, name: str, description: str, mimeType: str):
                pass
        class Tool:
            def __init__(self, name: str, description: str, inputSchema: dict):
                pass

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if MCP_AVAILABLE:
    server = Server("pytest-http")


def find_test_files(directory: Path = Path.cwd()) -> list[Path]:
    """Find all pytest-http test files in the given directory and subdirectories."""
    test_files = []
    for path in directory.rglob("test_*.http.json"):
        test_files.append(path)
    return sorted(test_files)


def validate_test_scenario(data: dict[str, Any]) -> tuple[bool, str]:
    """Validate a test scenario against the pytest-http schema."""
    try:
        Scenario.model_validate(data)
        return True, "Valid"
    except ValidationError as e:
        return False, str(e)


def extract_test_name(file_path: Path) -> str:
    """Extract test name from file path."""
    pattern = re.compile(r"^test_(?P<name>.+)\.http\.json$")
    match = pattern.match(file_path.name)
    return match.group("name") if match else file_path.stem


if MCP_AVAILABLE:
    @server.list_resources()
    async def handle_list_resources() -> list[types.Resource]:
        """List all available pytest-http test scenarios as resources."""
        test_files = find_test_files()
        resources = []
        
        for file_path in test_files:
            test_name = extract_test_name(file_path)
            relative_path = file_path.relative_to(Path.cwd())
            
            resources.append(
                types.Resource(
                    uri=f"pytest-http://test/{test_name}",
                    name=f"Test: {test_name}",
                    description=f"HTTP test scenario at {relative_path}",
                    mimeType="application/json",
                )
            )
        
        return resources


    @server.read_resource()
    async def handle_read_resource(uri: str) -> str:
        """Read a specific test scenario resource."""
        if not uri.startswith("pytest-http://test/"):
            raise ValueError(f"Unknown resource URI: {uri}")
        
        test_name = uri.replace("pytest-http://test/", "")
        test_files = find_test_files()
        
        for file_path in test_files:
            if extract_test_name(file_path) == test_name:
                try:
                    content = file_path.read_text()
                    # Validate and pretty-print the JSON
                    data = json.loads(content)
                    return json.dumps(data, indent=2)
                except (json.JSONDecodeError, FileNotFoundError) as e:
                    raise ValueError(f"Error reading test file {file_path}: {e}")
        
        raise ValueError(f"Test scenario '{test_name}' not found")


    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        """List available MCP tools for pytest-http management."""
        return [
            types.Tool(
                name="create_test_scenario",
                description="Create a new HTTP test scenario file",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Name of the test scenario (will be used as filename: test_{name}.http.json)"
                        },
                        "scenario": {
                            "type": "object",
                            "description": "The test scenario configuration",
                            "properties": {
                                "fixtures": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "List of pytest fixtures to use"
                                },
                                "marks": {
                                    "type": "array", 
                                    "items": {"type": "string"},
                                    "description": "List of pytest marks to apply"
                                },
                                "stages": {
                                    "type": "array",
                                    "description": "List of test stages",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "name": {"type": "string"},
                                            "url": {"type": "string"},
                                            "data": {},
                                            "params": {"type": "object"},
                                            "headers": {"type": "object"},
                                            "save": {"type": "object"}
                                        },
                                        "required": ["name"]
                                    }
                                }
                            },
                            "required": ["stages"]
                        },
                        "directory": {
                            "type": "string",
                            "description": "Directory to create the test file in (optional, defaults to current directory)",
                            "default": "."
                        }
                    },
                    "required": ["name", "scenario"]
                }
            ),
            types.Tool(
                name="list_test_scenarios",
                description="List all existing HTTP test scenarios",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "directory": {
                            "type": "string",
                            "description": "Directory to search for test files (optional, defaults to current directory)",
                            "default": "."
                        }
                    }
                }
            ),
            types.Tool(
                name="validate_test_scenario",
                description="Validate a test scenario against the pytest-http schema",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "scenario": {
                            "type": "object",
                            "description": "The test scenario to validate"
                        }
                    },
                    "required": ["scenario"]
                }
            ),
            types.Tool(
                name="create_stage",
                description="Create a test stage configuration",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Name of the stage"
                        },
                        "url": {
                            "type": "string",
                            "description": "HTTP endpoint URL (optional)"
                        },
                        "method": {
                            "type": "string",
                            "description": "HTTP method (currently only GET is supported)",
                            "default": "GET"
                        },
                        "params": {
                            "type": "object",
                            "description": "URL query parameters"
                        },
                        "headers": {
                            "type": "object",
                            "description": "HTTP headers"
                        },
                        "data": {
                            "description": "Data for the stage (any type)"
                        },
                        "save": {
                            "type": "object",
                            "description": "Variables to save using JMESPath expressions"
                        }
                    },
                    "required": ["name"]
                }
            ),
            types.Tool(
                name="generate_test_template",
                description="Generate a test scenario template with common patterns",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "template_type": {
                            "type": "string",
                            "enum": ["basic", "api_test", "multi_stage", "with_fixtures"],
                            "description": "Type of template to generate"
                        },
                        "api_endpoint": {
                            "type": "string",
                            "description": "API endpoint URL (for api_test template)"
                        }
                    },
                    "required": ["template_type"]
                }
            )
        ]


    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
        """Handle tool calls for pytest-http operations."""
        if arguments is None:
            arguments = {}
        
        if name == "create_test_scenario":
            return await create_test_scenario(arguments)
        elif name == "list_test_scenarios":
            return await list_test_scenarios(arguments)
        elif name == "validate_test_scenario":
            return await validate_test_scenario_tool(arguments)
        elif name == "create_stage":
            return await create_stage(arguments)
        elif name == "generate_test_template":
            return await generate_test_template(arguments)
        else:
            raise ValueError(f"Unknown tool: {name}")


async def create_test_scenario(arguments: dict[str, Any]) -> list[types.TextContent]:
    """Create a new HTTP test scenario file."""
    name = arguments["name"]
    scenario = arguments["scenario"]
    directory = Path(arguments.get("directory", "."))
    
    # Validate the scenario
    is_valid, error_message = validate_test_scenario(scenario)
    if not is_valid:
        return [types.TextContent(
            type="text",
            text=f"❌ Invalid scenario: {error_message}"
        )]
    
    # Create the file path
    file_path = directory / f"test_{name}.http.json"
    
    # Check if file already exists
    if file_path.exists():
        return [types.TextContent(
            type="text",
            text=f"❌ Test file already exists: {file_path}"
        )]
    
    # Create directory if it doesn't exist
    directory.mkdir(parents=True, exist_ok=True)
    
    # Write the scenario to file
    try:
        with open(file_path, 'w') as f:
            json.dump(scenario, f, indent=2)
        
        return [types.TextContent(
            type="text",
            text=f"✅ Created test scenario: {file_path}\n\nContent:\n```json\n{json.dumps(scenario, indent=2)}\n```"
        )]
    except Exception as e:
        return [types.TextContent(
            type="text",
            text=f"❌ Failed to create test file: {e}"
        )]


async def list_test_scenarios(arguments: dict[str, Any]) -> list[types.TextContent]:
    """List all existing HTTP test scenarios."""
    directory = Path(arguments.get("directory", "."))
    
    if not directory.exists():
        return [types.TextContent(
            type="text",
            text=f"❌ Directory does not exist: {directory}"
        )]
    
    test_files = []
    for path in directory.rglob("test_*.http.json"):
        test_files.append(path)
    
    if not test_files:
        return [types.TextContent(
            type="text",
            text=f"No HTTP test scenarios found in {directory}"
        )]
    
    result_lines = [f"Found {len(test_files)} HTTP test scenario(s):\n"]
    
    for file_path in sorted(test_files):
        test_name = extract_test_name(file_path)
        relative_path = file_path.relative_to(directory)
        
        # Try to read and validate the file
        try:
            content = file_path.read_text()
            data = json.loads(content)
            is_valid, error = validate_test_scenario(data)
            status = "✅ Valid" if is_valid else f"❌ Invalid: {error}"
            
            # Get stage count
            stages_count = len(data.get("stages", []))
            
            result_lines.append(f"• **{test_name}** ({relative_path})")
            result_lines.append(f"  - Status: {status}")
            result_lines.append(f"  - Stages: {stages_count}")
            
            # Show fixtures and marks if present
            if data.get("fixtures"):
                result_lines.append(f"  - Fixtures: {', '.join(data['fixtures'])}")
            if data.get("marks"):
                result_lines.append(f"  - Marks: {', '.join(data['marks'])}")
            
            result_lines.append("")
            
        except Exception as e:
            result_lines.append(f"• **{test_name}** ({relative_path}) - ❌ Error: {e}\n")
    
    return [types.TextContent(
        type="text",
        text="\n".join(result_lines)
    )]


async def validate_test_scenario_tool(arguments: dict[str, Any]) -> list[types.TextContent]:
    """Validate a test scenario against the pytest-http schema."""
    scenario = arguments["scenario"]
    
    is_valid, message = validate_test_scenario(scenario)
    
    if is_valid:
        return [types.TextContent(
            type="text",
            text=f"✅ Test scenario is valid!\n\nScenario:\n```json\n{json.dumps(scenario, indent=2)}\n```"
        )]
    else:
        return [types.TextContent(
            type="text",
            text=f"❌ Test scenario validation failed:\n\n{message}\n\nScenario:\n```json\n{json.dumps(scenario, indent=2)}\n```"
        )]


async def create_stage(arguments: dict[str, Any]) -> list[types.TextContent]:
    """Create a test stage configuration."""
    stage_config = {
        "name": arguments["name"]
    }
    
    # Add optional fields if provided
    if "url" in arguments:
        stage_config["url"] = arguments["url"]
    if "params" in arguments:
        stage_config["params"] = arguments["params"]
    if "headers" in arguments:
        stage_config["headers"] = arguments["headers"]
    if "data" in arguments:
        stage_config["data"] = arguments["data"]
    if "save" in arguments:
        stage_config["save"] = arguments["save"]
    
    # Validate the stage
    try:
        Stage.model_validate(stage_config)
        return [types.TextContent(
            type="text",
            text=f"✅ Valid stage configuration:\n\n```json\n{json.dumps(stage_config, indent=2)}\n```"
        )]
    except ValidationError as e:
        return [types.TextContent(
            type="text",
            text=f"❌ Invalid stage configuration: {e}\n\nStage:\n```json\n{json.dumps(stage_config, indent=2)}\n```"
        )]


async def generate_test_template(arguments: dict[str, Any]) -> list[types.TextContent]:
    """Generate a test scenario template with common patterns."""
    template_type = arguments["template_type"]
    
    templates = {
        "basic": {
            "stages": [
                {
                    "name": "basic_test",
                    "data": "test_data"
                }
            ]
        },
        "api_test": {
            "stages": [
                {
                    "name": "get_data",
                    "url": arguments.get("api_endpoint", "https://api.example.com/data"),
                    "headers": {
                        "Accept": "application/json",
                        "User-Agent": "pytest-http/test"
                    },
                    "save": {
                        "response_data": "json",
                        "status_code": "status_code"
                    }
                }
            ]
        },
        "multi_stage": {
            "stages": [
                {
                    "name": "setup",
                    "data": {"setup": True}
                },
                {
                    "name": "execute",
                    "url": arguments.get("api_endpoint", "https://api.example.com/execute"),
                    "headers": {
                        "Accept": "application/json"
                    },
                    "save": {
                        "result": "json.result"
                    }
                },
                {
                    "name": "verify",
                    "data": "${result}"
                }
            ]
        },
        "with_fixtures": {
            "fixtures": ["httpserver_listen_address"],
            "stages": [
                {
                    "name": "test_with_fixture",
                    "url": "http://${httpserver_listen_address}/test",
                    "headers": {
                        "Accept": "application/json"
                    }
                }
            ]
        }
    }
    
    if template_type not in templates:
        return [types.TextContent(
            type="text",
            text=f"❌ Unknown template type: {template_type}\n\nAvailable templates: {', '.join(templates.keys())}"
        )]
    
    template = templates[template_type]
    
    return [types.TextContent(
        type="text",
        text=f"✅ Generated '{template_type}' template:\n\n```json\n{json.dumps(template, indent=2)}\n```\n\n💡 You can use this template with the `create_test_scenario` tool to create a new test file."
    )]


async def main():
    """Main entry point for the MCP server."""
    if not MCP_AVAILABLE:
        print("MCP library not available. Please install with: uv add mcp")
        return
    
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="pytest-http",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={}
                )
            )
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())