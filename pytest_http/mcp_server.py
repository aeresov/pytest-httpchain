"""MCP Server for pytest-http library"""

import argparse
import asyncio
import json
import logging
import sys
from typing import Any, Dict, List

import jmespath
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolResult,
    Resource,
    TextContent,
    Tool,
)
from pydantic import ValidationError

from pytest_http.models import Scenario, Stage

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Server("pytest-http-mcp")

class TestScenarioGenerator:
    """Helper class for generating test scenarios"""
    
    @staticmethod
    def generate_basic_scenario(name: str, url: str = None) -> Dict[str, Any]:
        """Generate a basic test scenario"""
        stage = {
            "name": name,
            "data": {}
        }
        
        if url:
            stage["url"] = url
            stage["headers"] = {"Accept": "application/json"}
        
        return {
            "stages": [stage]
        }
    
    @staticmethod
    def generate_multistage_scenario(stages: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Generate a multi-stage test scenario"""
        scenario_stages = []
        
        for i, stage_info in enumerate(stages):
            stage = {
                "name": stage_info.get("name", f"stage_{i+1}"),
                "data": stage_info.get("data", {})
            }
            
            if "url" in stage_info:
                stage["url"] = stage_info["url"]
                stage["headers"] = stage_info.get("headers", {"Accept": "application/json"})
            
            if "params" in stage_info:
                stage["params"] = stage_info["params"]
            
            if "save" in stage_info:
                stage["save"] = stage_info["save"]
            
            scenario_stages.append(stage)
        
        return {
            "stages": scenario_stages
        }
    
    @staticmethod
    def generate_crud_scenario(base_url: str, resource: str) -> Dict[str, Any]:
        """Generate a CRUD test scenario"""
        return {
            "stages": [
                {
                    "name": f"create_{resource}",
                    "url": f"{base_url}/{resource}",
                    "data": {"test": "data"},
                    "headers": {"Content-Type": "application/json"},
                    "save": {
                        "created_id": "json.id",
                        "created_resource": "json"
                    }
                },
                {
                    "name": f"get_{resource}",
                    "url": f"{base_url}/{resource}/$created_id",
                    "headers": {"Accept": "application/json"},
                    "save": {
                        "retrieved_resource": "json"
                    }
                },
                {
                    "name": f"update_{resource}",
                    "url": f"{base_url}/{resource}/$created_id",
                    "data": {"updated": True},
                    "headers": {"Content-Type": "application/json"}
                },
                {
                    "name": f"delete_{resource}",
                    "url": f"{base_url}/{resource}/$created_id",
                    "headers": {"Accept": "application/json"}
                }
            ]
        }

class JMESPathHelper:
    """Helper class for JMESPath operations"""
    
    @staticmethod
    def validate_expression(expression: str) -> Dict[str, Any]:
        """Validate a JMESPath expression"""
        try:
            jmespath.compile(expression)
            return {"valid": True, "error": None}
        except Exception as e:
            return {"valid": False, "error": str(e)}
    
    @staticmethod
    def test_expression(expression: str, data: Any) -> Dict[str, Any]:
        """Test a JMESPath expression against sample data"""
        try:
            compiled = jmespath.compile(expression)
            result = compiled.search(data)
            return {"valid": True, "result": result, "error": None}
        except Exception as e:
            return {"valid": False, "result": None, "error": str(e)}
    
    @staticmethod
    def suggest_expressions(data_structure: str) -> List[str]:
        """Suggest common JMESPath expressions based on data structure"""
        suggestions = []
        
        if "array" in data_structure.lower() or "list" in data_structure.lower():
            suggestions.extend([
                "length(@)",
                "[0]",
                "[-1]",
                "[*].id",
                "[?id > `10`]"
            ])
        
        if "object" in data_structure.lower() or "dict" in data_structure.lower():
            suggestions.extend([
                "json.id",
                "json.name",
                "json.status",
                "headers.content-type",
                "status_code"
            ])
        
        # Common response patterns
        suggestions.extend([
            "json",
            "text",
            "headers",
            "status_code"
        ])
        
        return suggestions

@app.list_tools()
async def handle_list_tools() -> list[Tool]:
    """List available tools for the MCP server"""
    return [
        Tool(
            name="generate_basic_test",
            description="Generate a basic HTTP test scenario",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name for the test stage"
                    },
                    "url": {
                        "type": "string",
                        "description": "Optional URL for HTTP request"
                    }
                },
                "required": ["name"]
            }
        ),
        Tool(
            name="generate_multistage_test",
            description="Generate a multi-stage HTTP test scenario",
            inputSchema={
                "type": "object",
                "properties": {
                    "stages": {
                        "type": "array",
                        "description": "List of stage configurations",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "url": {"type": "string"},
                                "data": {"type": "object"},
                                "params": {"type": "object"},
                                "headers": {"type": "object"},
                                "save": {"type": "object"}
                            }
                        }
                    }
                },
                "required": ["stages"]
            }
        ),
        Tool(
            name="generate_crud_test",
            description="Generate a CRUD (Create, Read, Update, Delete) test scenario",
            inputSchema={
                "type": "object",
                "properties": {
                    "base_url": {
                        "type": "string",
                        "description": "Base URL for the API"
                    },
                    "resource": {
                        "type": "string",
                        "description": "Resource name (e.g., 'users', 'posts')"
                    }
                },
                "required": ["base_url", "resource"]
            }
        ),
        Tool(
            name="validate_test_scenario",
            description="Validate a test scenario against the pytest-http schema",
            inputSchema={
                "type": "object",
                "properties": {
                    "scenario": {
                        "type": "object",
                        "description": "Test scenario JSON to validate"
                    }
                },
                "required": ["scenario"]
            }
        ),
        Tool(
            name="validate_jmespath",
            description="Validate a JMESPath expression",
            inputSchema={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "JMESPath expression to validate"
                    }
                },
                "required": ["expression"]
            }
        ),
        Tool(
            name="test_jmespath",
            description="Test a JMESPath expression against sample data",
            inputSchema={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "JMESPath expression to test"
                    },
                    "data": {
                        "type": "object",
                        "description": "Sample data to test against"
                    }
                },
                "required": ["expression", "data"]
            }
        ),
        Tool(
            name="suggest_jmespath",
            description="Suggest JMESPath expressions based on data structure",
            inputSchema={
                "type": "object",
                "properties": {
                    "data_structure": {
                        "type": "string",
                        "description": "Description of the data structure (e.g., 'array of objects', 'nested object')"
                    }
                },
                "required": ["data_structure"]
            }
        ),
        Tool(
            name="create_test_template",
            description="Create a test template with common patterns",
            inputSchema={
                "type": "object",
                "properties": {
                    "template_type": {
                        "type": "string",
                        "enum": ["api_test", "crud_test", "multistage_test", "simple_test"],
                        "description": "Type of template to create"
                    },
                    "options": {
                        "type": "object",
                        "description": "Additional options for template customization"
                    }
                },
                "required": ["template_type"]
            }
        ),
        Tool(
            name="optimize_test_scenario",
            description="Suggest optimizations for a test scenario",
            inputSchema={
                "type": "object",
                "properties": {
                    "scenario": {
                        "type": "object",
                        "description": "Test scenario to optimize"
                    }
                },
                "required": ["scenario"]
            }
        )
    ]

@app.call_tool()
async def handle_call_tool(name: str, arguments: dict | None) -> CallToolResult:
    """Handle tool calls"""
    try:
        if name == "generate_basic_test":
            stage_name = arguments.get("name")
            url = arguments.get("url")
            
            scenario = TestScenarioGenerator.generate_basic_scenario(stage_name, url)
            
            return CallToolResult(
                content=[TextContent(
                    type="text",
                    text=json.dumps(scenario, indent=2)
                )]
            )
        
        elif name == "generate_multistage_test":
            stages = arguments.get("stages", [])
            
            scenario = TestScenarioGenerator.generate_multistage_scenario(stages)
            
            return CallToolResult(
                content=[TextContent(
                    type="text",
                    text=json.dumps(scenario, indent=2)
                )]
            )
        
        elif name == "generate_crud_test":
            base_url = arguments.get("base_url")
            resource = arguments.get("resource")
            
            scenario = TestScenarioGenerator.generate_crud_scenario(base_url, resource)
            
            return CallToolResult(
                content=[TextContent(
                    type="text",
                    text=json.dumps(scenario, indent=2)
                )]
            )
        
        elif name == "validate_test_scenario":
            scenario_data = arguments.get("scenario")
            
            try:
                # Validate using Pydantic model
                scenario = Scenario.model_validate(scenario_data)
                
                return CallToolResult(
                    content=[TextContent(
                        type="text",
                        text=json.dumps({
                            "valid": True,
                            "message": "Test scenario is valid",
                            "parsed_scenario": scenario.model_dump()
                        }, indent=2)
                    )]
                )
            
            except ValidationError as e:
                return CallToolResult(
                    content=[TextContent(
                        type="text",
                        text=json.dumps({
                            "valid": False,
                            "errors": str(e),
                            "details": e.errors() if hasattr(e, 'errors') else None
                        }, indent=2)
                    )]
                )
        
        elif name == "validate_jmespath":
            expression = arguments.get("expression")
            
            result = JMESPathHelper.validate_expression(expression)
            
            return CallToolResult(
                content=[TextContent(
                    type="text",
                    text=json.dumps(result, indent=2)
                )]
            )
        
        elif name == "test_jmespath":
            expression = arguments.get("expression")
            data = arguments.get("data")
            
            result = JMESPathHelper.test_expression(expression, data)
            
            return CallToolResult(
                content=[TextContent(
                    type="text",
                    text=json.dumps(result, indent=2)
                )]
            )
        
        elif name == "suggest_jmespath":
            data_structure = arguments.get("data_structure")
            
            suggestions = JMESPathHelper.suggest_expressions(data_structure)
            
            return CallToolResult(
                content=[TextContent(
                    type="text",
                    text=json.dumps({
                        "suggestions": suggestions,
                        "data_structure": data_structure
                    }, indent=2)
                )]
            )
        
        elif name == "create_test_template":
            template_type = arguments.get("template_type")
            options = arguments.get("options", {})
            
            templates = {
                "simple_test": {
                    "stages": [
                        {
                            "name": "simple_test",
                            "data": {}
                        }
                    ]
                },
                "api_test": {
                    "stages": [
                        {
                            "name": "api_call",
                            "url": "https://api.example.com/endpoint",
                            "headers": {"Accept": "application/json"},
                            "save": {
                                "response_data": "json"
                            }
                        }
                    ]
                },
                "crud_test": TestScenarioGenerator.generate_crud_scenario(
                    options.get("base_url", "https://api.example.com"),
                    options.get("resource", "items")
                ),
                "multistage_test": {
                    "stages": [
                        {
                            "name": "setup_stage",
                            "data": {"setup": True}
                        },
                        {
                            "name": "main_stage",
                            "url": "https://api.example.com/test",
                            "headers": {"Accept": "application/json"}
                        },
                        {
                            "name": "cleanup_stage",
                            "data": {"cleanup": True}
                        }
                    ]
                }
            }
            
            template = templates.get(template_type, templates["simple_test"])
            
            return CallToolResult(
                content=[TextContent(
                    type="text",
                    text=json.dumps(template, indent=2)
                )]
            )
        
        elif name == "optimize_test_scenario":
            scenario_data = arguments.get("scenario")
            
            suggestions = []
            
            try:
                scenario = Scenario.model_validate(scenario_data)
                
                # Check for optimization opportunities
                if len(scenario.stages) == 1 and not scenario.stages[0].url:
                    suggestions.append("Consider adding an HTTP request URL if this is meant to test an API")
                
                for i, stage in enumerate(scenario.stages):
                    if stage.url and not stage.headers:
                        suggestions.append(f"Stage '{stage.name}': Consider adding headers like 'Accept: application/json'")
                    
                    if stage.url and stage.url.startswith("http://"):
                        suggestions.append(f"Stage '{stage.name}': Consider using HTTPS instead of HTTP")
                    
                    if stage.save and not stage.url:
                        suggestions.append(f"Stage '{stage.name}': Saving variables without HTTP request - ensure this is intentional")
                
                if not scenario.fixtures and len(scenario.stages) > 1:
                    suggestions.append("Consider using fixtures for shared test data across stages")
                
                optimization_result = {
                    "valid": True,
                    "suggestions": suggestions,
                    "stage_count": len(scenario.stages),
                    "has_http_requests": any(stage.url for stage in scenario.stages)
                }
                
            except ValidationError as e:
                optimization_result = {
                    "valid": False,
                    "error": "Cannot optimize invalid scenario. Please validate first.",
                    "validation_errors": str(e)
                }
            
            return CallToolResult(
                content=[TextContent(
                    type="text",
                    text=json.dumps(optimization_result, indent=2)
                )]
            )
        
        else:
            return CallToolResult(
                content=[TextContent(
                    type="text",
                    text=f"Unknown tool: {name}"
                )],
                isError=True
            )
    
    except Exception as e:
        logger.error(f"Error in tool {name}: {e}")
        return CallToolResult(
            content=[TextContent(
                type="text",
                text=f"Error executing tool {name}: {str(e)}"
            )],
            isError=True
        )

@app.list_resources()
async def handle_list_resources() -> list[Resource]:
    """List available resources"""
    return [
        Resource(
            uri="schema://pytest-http/scenario",
            name="Test Scenario Schema",
            description="JSON schema for pytest-http test scenarios",
            mimeType="application/json"
        ),
        Resource(
            uri="docs://pytest-http/examples",
            name="Test Examples",
            description="Example test scenarios for different use cases",
            mimeType="text/markdown"
        )
    ]

@app.read_resource()
async def handle_read_resource(uri: str) -> str:
    """Read resource content"""
    if uri == "schema://pytest-http/scenario":
        # Return the schema based on the Pydantic models
        schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "title": "pytest-http Test Scenario",
            "type": "object",
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
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "data": {"type": "object"},
                            "url": {"type": "string"},
                            "params": {"type": "object"},
                            "headers": {"type": "object"},
                            "save": {
                                "type": "object",
                                "description": "Variables to save using JMESPath expressions"
                            }
                        },
                        "required": ["name", "data"]
                    }
                }
            },
            "required": ["stages"]
        }
        return json.dumps(schema, indent=2)
    
    elif uri == "docs://pytest-http/examples":
        examples = """
# pytest-http Test Examples

## Basic Test
```json
{
    "stages": [
        {
            "name": "basic_test",
            "data": {}
        }
    ]
}
```

## HTTP API Test
```json
{
    "stages": [
        {
            "name": "get_users",
            "url": "https://jsonplaceholder.typicode.com/users",
            "headers": {"Accept": "application/json"},
            "save": {
                "user_count": "length(@)",
                "first_user": "[0]"
            }
        }
    ]
}
```

## Multi-stage Test with Variable Substitution
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

## Test with Fixtures and Marks
```json
{
    "fixtures": ["api_client", "test_data"],
    "marks": ["slow", "integration"],
    "stages": [
        {
            "name": "test_with_fixtures",
            "url": "$api_client/endpoint",
            "data": "$test_data"
        }
    ]
}
```
"""
        return examples
    
    else:
        raise ValueError(f"Unknown resource: {uri}")

async def run_server():
    """Run the MCP server"""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream, 
            write_stream, 
            InitializationOptions(
                server_name="pytest-http-mcp",
                server_version="1.0.0",
                capabilities=app.get_capabilities(
                    notification_options=None,
                    experimental_capabilities={}
                )
            )
        )

def main():
    """Main entry point for the MCP server"""
    parser = argparse.ArgumentParser(description="pytest-http MCP Server")
    parser.add_argument("--log-level", default="INFO", help="Log level")
    args = parser.parse_args()
    
    logging.basicConfig(level=getattr(logging, args.log_level.upper()))
    
    asyncio.run(run_server())

if __name__ == "__main__":
    main()