"""Tests for the MCP server functionality."""

import json
import pytest
from pathlib import Path

from pytest_http.mcp_server import (
    validate_test_file,
    create_test_template,
    list_test_files,
    write_test_file,
    get_test_documentation,
    HTTPTestValidationResult,
    HTTPTestFileInfo,
)


class TestMCPServerTools:
    """Test MCP server tools functionality."""

    def test_validate_test_file_valid_json(self):
        """Test validation of a valid test file."""
        test_content = json.dumps({
            "stages": [
                {
                    "name": "test_stage",
                    "data": {"test": "data"}
                }
            ]
        })
        
        result = validate_test_file(test_content)
        assert isinstance(result, HTTPTestValidationResult)
        assert result.valid is True
        assert result.error is None
        assert result.scenario is not None

    def test_validate_test_file_invalid_json(self):
        """Test validation of invalid JSON."""
        test_content = "{ invalid json }"
        
        result = validate_test_file(test_content)
        assert isinstance(result, HTTPTestValidationResult)
        assert result.valid is False
        assert "Invalid JSON" in result.error
        assert result.scenario is None

    def test_validate_test_file_invalid_schema(self):
        """Test validation of JSON that doesn't match schema."""
        test_content = json.dumps({
            "stages": [
                {
                    "name": "test_stage"
                    # Missing required "data" field
                }
            ]
        })
        
        result = validate_test_file(test_content)
        assert isinstance(result, HTTPTestValidationResult)
        assert result.valid is False
        assert "Validation error" in result.error
        assert result.scenario is None

    def test_create_test_template_basic(self):
        """Test creating a basic test template."""
        template = create_test_template("my_test", "basic")
        data = json.loads(template)
        
        assert "stages" in data
        assert len(data["stages"]) == 1
        assert data["stages"][0]["name"] == "my_test_stage"
        assert "data" in data["stages"][0]

    def test_create_test_template_http(self):
        """Test creating an HTTP test template."""
        template = create_test_template("api_test", "http")
        data = json.loads(template)
        
        assert "stages" in data
        assert len(data["stages"]) == 1
        stage = data["stages"][0]
        assert stage["name"] == "api_test_request"
        assert "url" in stage
        assert "headers" in stage
        assert "save" in stage

    def test_create_test_template_multistage(self):
        """Test creating a multistage test template."""
        template = create_test_template("complex_test", "multistage")
        data = json.loads(template)
        
        assert "fixtures" in data
        assert "stages" in data
        assert len(data["stages"]) == 3
        assert data["fixtures"] == ["api_base_url"]

    def test_list_test_files_nonexistent_directory(self):
        """Test listing test files in a nonexistent directory."""
        result = list_test_files("nonexistent_directory")
        assert isinstance(result, list)
        assert len(result) == 0

    def test_list_test_files_examples_directory(self):
        """Test listing test files in the examples directory."""
        result = list_test_files("tests/examples")
        assert isinstance(result, list)
        
        # Should find some test files in examples
        if len(result) > 0:
            for test_file in result:
                assert isinstance(test_file, HTTPTestFileInfo)
                assert test_file.path.endswith('.http.json')
                assert test_file.name is not None

    def test_write_test_file_valid(self, tmp_path):
        """Test writing a valid test file."""
        test_content = json.dumps({
            "stages": [
                {
                    "name": "test_stage",
                    "data": {"test": "data"}
                }
            ]
        })
        
        file_path = tmp_path / "my_test.http.json"
        result = write_test_file(str(file_path), test_content)
        
        assert result["success"] is True
        assert "file_path" in result
        
        # File should exist and contain correct content
        created_file = Path(result["file_path"])
        assert created_file.exists()
        assert created_file.name.startswith("test_")
        assert created_file.suffix == ".json"

    def test_write_test_file_invalid(self, tmp_path):
        """Test writing an invalid test file."""
        test_content = "{ invalid json }"
        
        file_path = tmp_path / "invalid_test.http.json"
        result = write_test_file(str(file_path), test_content)
        
        assert result["success"] is False
        assert "error" in result
        assert "Invalid test content" in result["error"]

    def test_get_test_documentation(self):
        """Test getting test documentation."""
        docs = get_test_documentation()
        assert isinstance(docs, str)
        assert len(docs) > 0
        assert "pytest-http" in docs
        assert "JSON" in docs
        assert "stages" in docs