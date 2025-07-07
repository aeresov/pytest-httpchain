"""Tests for the MCP server functionality"""

import json
import pytest
from unittest.mock import patch

# Test the components without requiring MCP to be installed
from pytest_http.models import Scenario


class TestTestScenarioGenerator:
    """Test the TestScenarioGenerator helper class"""
    
    def test_generate_basic_scenario_without_url(self):
        """Test generating a basic scenario without URL"""
        from pytest_http.mcp_server import TestScenarioGenerator
        
        result = TestScenarioGenerator.generate_basic_scenario("test_stage")
        
        expected = {
            "stages": [
                {
                    "name": "test_stage",
                    "data": {}
                }
            ]
        }
        
        assert result == expected
    
    def test_generate_basic_scenario_with_url(self):
        """Test generating a basic scenario with URL"""
        from pytest_http.mcp_server import TestScenarioGenerator
        
        result = TestScenarioGenerator.generate_basic_scenario("test_api", "https://api.example.com/test")
        
        expected = {
            "stages": [
                {
                    "name": "test_api",
                    "data": {},
                    "url": "https://api.example.com/test",
                    "headers": {"Accept": "application/json"}
                }
            ]
        }
        
        assert result == expected
    
    def test_generate_multistage_scenario(self):
        """Test generating a multi-stage scenario"""
        from pytest_http.mcp_server import TestScenarioGenerator
        
        stages = [
            {"name": "first", "url": "https://api.example.com/1"},
            {"name": "second", "data": {"key": "value"}, "save": {"var": "json.id"}}
        ]
        
        result = TestScenarioGenerator.generate_multistage_scenario(stages)
        
        expected = {
            "stages": [
                {
                    "name": "first",
                    "data": {},
                    "url": "https://api.example.com/1",
                    "headers": {"Accept": "application/json"}
                },
                {
                    "name": "second",
                    "data": {"key": "value"},
                    "save": {"var": "json.id"}
                }
            ]
        }
        
        assert result == expected
    
    def test_generate_crud_scenario(self):
        """Test generating a CRUD scenario"""
        from pytest_http.mcp_server import TestScenarioGenerator
        
        result = TestScenarioGenerator.generate_crud_scenario("https://api.example.com", "users")
        
        assert "stages" in result
        assert len(result["stages"]) == 4
        
        stage_names = [stage["name"] for stage in result["stages"]]
        assert "create_users" in stage_names
        assert "get_users" in stage_names
        assert "update_users" in stage_names
        assert "delete_users" in stage_names
        
        # Validate that the generated scenario is valid
        scenario = Scenario.model_validate(result)
        assert len(scenario.stages) == 4


class TestJMESPathHelper:
    """Test the JMESPathHelper class"""
    
    def test_validate_expression_valid(self):
        """Test validating a valid JMESPath expression"""
        from pytest_http.mcp_server import JMESPathHelper
        
        result = JMESPathHelper.validate_expression("json.id")
        
        assert result["valid"] is True
        assert result["error"] is None
    
    def test_validate_expression_invalid(self):
        """Test validating an invalid JMESPath expression"""
        from pytest_http.mcp_server import JMESPathHelper
        
        result = JMESPathHelper.validate_expression("invalid[expression")
        
        assert result["valid"] is False
        assert result["error"] is not None
    
    def test_test_expression_success(self):
        """Test executing a JMESPath expression successfully"""
        from pytest_http.mcp_server import JMESPathHelper
        
        data = {"json": {"id": 123, "name": "test"}}
        result = JMESPathHelper.test_expression("json.id", data)
        
        assert result["valid"] is True
        assert result["result"] == 123
        assert result["error"] is None
    
    def test_test_expression_failure(self):
        """Test executing a JMESPath expression with error"""
        from pytest_http.mcp_server import JMESPathHelper
        
        data = {"json": {"id": 123}}
        result = JMESPathHelper.test_expression("invalid[expression", data)
        
        assert result["valid"] is False
        assert result["result"] is None
        assert result["error"] is not None
    
    def test_suggest_expressions_array(self):
        """Test suggesting expressions for array data"""
        from pytest_http.mcp_server import JMESPathHelper
        
        suggestions = JMESPathHelper.suggest_expressions("array of objects")
        
        assert "length(@)" in suggestions
        assert "[0]" in suggestions
        assert "[-1]" in suggestions
        assert "[*].id" in suggestions
    
    def test_suggest_expressions_object(self):
        """Test suggesting expressions for object data"""
        from pytest_http.mcp_server import JMESPathHelper
        
        suggestions = JMESPathHelper.suggest_expressions("nested object")
        
        assert "json.id" in suggestions
        assert "json.name" in suggestions
        assert "headers.content-type" in suggestions


class TestScenarioValidation:
    """Test scenario validation functionality"""
    
    def test_valid_scenario(self):
        """Test validating a correct scenario"""
        scenario_data = {
            "stages": [
                {
                    "name": "test_stage",
                    "data": {},
                    "url": "https://api.example.com/test"
                }
            ]
        }
        
        # This should not raise an exception
        scenario = Scenario.model_validate(scenario_data)
        assert len(scenario.stages) == 1
        assert scenario.stages[0].name == "test_stage"
    
    def test_invalid_scenario_missing_name(self):
        """Test validating a scenario with missing stage name"""
        scenario_data = {
            "stages": [
                {
                    "data": {},
                    "url": "https://api.example.com/test"
                }
            ]
        }
        
        with pytest.raises(Exception):  # Should be ValidationError but we don't want to import it
            Scenario.model_validate(scenario_data)
    
    def test_invalid_scenario_missing_data(self):
        """Test validating a scenario with missing stage data"""
        scenario_data = {
            "stages": [
                {
                    "name": "test_stage",
                    "url": "https://api.example.com/test"
                }
            ]
        }
        
        with pytest.raises(Exception):  # Should be ValidationError
            Scenario.model_validate(scenario_data)


@pytest.fixture
def sample_scenarios():
    """Provide sample test scenarios for testing"""
    return {
        "basic": {
            "stages": [
                {
                    "name": "basic_test",
                    "data": {}
                }
            ]
        },
        "http": {
            "stages": [
                {
                    "name": "api_test",
                    "data": {},
                    "url": "https://api.example.com/users",
                    "headers": {"Accept": "application/json"},
                    "save": {"user_count": "length(@)"}
                }
            ]
        },
        "multistage": {
            "stages": [
                {
                    "name": "create_user",
                    "data": {"name": "Test User"},
                    "url": "https://api.example.com/users",
                    "save": {"user_id": "json.id"}
                },
                {
                    "name": "get_user", 
                    "data": {},
                    "url": "https://api.example.com/users/$user_id",
                    "headers": {"Accept": "application/json"}
                }
            ]
        }
    }


def test_scenarios_are_valid(sample_scenarios):
    """Test that all sample scenarios are valid"""
    for name, scenario_data in sample_scenarios.items():
        scenario = Scenario.model_validate(scenario_data)
        assert len(scenario.stages) > 0, f"Scenario {name} should have stages"