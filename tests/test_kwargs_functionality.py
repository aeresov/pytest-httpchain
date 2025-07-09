"""
Test the new kwargs functionality for verify and save functions.
"""

import json
import pytest
from unittest.mock import Mock, patch

from pytest_http.models import FunctionCall, SaveConfig, Verify, Stage, Scenario


def test_function_call_model():
    """Test that FunctionCall model works correctly."""
    func_call = FunctionCall(
        function="test_module:test_function",
        kwargs={"param1": "value1", "param2": 42}
    )
    
    assert func_call.function == "test_module:test_function"
    assert func_call.kwargs == {"param1": "value1", "param2": 42}


def test_function_call_model_no_kwargs():
    """Test that FunctionCall model works without kwargs."""
    func_call = FunctionCall(function="test_module:test_function")
    
    assert func_call.function == "test_module:test_function"
    assert func_call.kwargs is None


def test_verify_with_function_call():
    """Test that Verify model accepts FunctionCall objects."""
    verify = Verify(
        status=200,
        functions=[
            "test_module:simple_function",
            FunctionCall(
                function="test_module:function_with_kwargs",
                kwargs={"expected_status": 200, "timeout": 5.0}
            )
        ]
    )
    
    assert len(verify.functions) == 2
    assert verify.functions[0] == "test_module:simple_function"
    assert isinstance(verify.functions[1], FunctionCall)
    assert verify.functions[1].function == "test_module:function_with_kwargs"
    assert verify.functions[1].kwargs == {"expected_status": 200, "timeout": 5.0}


def test_save_config_with_function_call():
    """Test that SaveConfig model accepts FunctionCall objects."""
    save_config = SaveConfig(
        vars={"test_var": "json.field"},
        functions=[
            "test_module:simple_function",
            FunctionCall(
                function="test_module:function_with_kwargs",
                kwargs={"field_path": "data.items", "default_value": "unknown"}
            )
        ]
    )
    
    assert len(save_config.functions) == 2
    assert save_config.functions[0] == "test_module:simple_function"
    assert isinstance(save_config.functions[1], FunctionCall)
    assert save_config.functions[1].function == "test_module:function_with_kwargs"
    assert save_config.functions[1].kwargs == {"field_path": "data.items", "default_value": "unknown"}


def test_stage_with_function_calls():
    """Test that Stage model works with FunctionCall objects."""
    stage = Stage(
        name="test_stage",
        url="https://example.com",
        verify=Verify(
            functions=[
                FunctionCall(
                    function="test_module:verify_function",
                    kwargs={"expected_text": "Hello", "case_sensitive": False}
                )
            ]
        ),
        save=SaveConfig(
            functions=[
                FunctionCall(
                    function="test_module:save_function",
                    kwargs={"extract_fields": ["title", "author"]}
                )
            ]
        )
    )
    
    assert stage.verify.functions[0].function == "test_module:verify_function"
    assert stage.verify.functions[0].kwargs == {"expected_text": "Hello", "case_sensitive": False}
    assert stage.save.functions[0].function == "test_module:save_function"
    assert stage.save.functions[0].kwargs == {"extract_fields": ["title", "author"]}


def test_scenario_with_function_calls():
    """Test that Scenario model works with FunctionCall objects."""
    scenario_data = {
        "stages": [
            {
                "name": "test_stage",
                "url": "https://example.com",
                "verify": {
                    "functions": [
                        {
                            "function": "test_module:verify_function",
                            "kwargs": {
                                "expected_status": 200,
                                "timeout": 5.0
                            }
                        }
                    ]
                },
                "save": {
                    "functions": [
                        {
                            "function": "test_module:save_function",
                            "kwargs": {
                                "field_path": "data.items",
                                "default_value": "unknown"
                            }
                        }
                    ]
                }
            }
        ]
    }
    
    scenario = Scenario.model_validate(scenario_data)
    
    assert len(scenario.stages) == 1
    stage = scenario.stages[0]
    
    # Test verify function
    verify_func = stage.verify.functions[0]
    assert isinstance(verify_func, FunctionCall)
    assert verify_func.function == "test_module:verify_function"
    assert verify_func.kwargs == {"expected_status": 200, "timeout": 5.0}
    
    # Test save function
    save_func = stage.save.functions[0]
    assert isinstance(save_func, FunctionCall)
    assert save_func.function == "test_module:save_function"
    assert save_func.kwargs == {"field_path": "data.items", "default_value": "unknown"}


def test_backward_compatibility():
    """Test that existing string function names still work."""
    scenario_data = {
        "stages": [
            {
                "name": "test_stage",
                "url": "https://example.com",
                "verify": {
                    "functions": ["test_module:simple_function"]
                },
                "save": {
                    "functions": ["test_module:save_function"]
                }
            }
        ]
    }
    
    scenario = Scenario.model_validate(scenario_data)
    
    assert len(scenario.stages) == 1
    stage = scenario.stages[0]
    
    # Test that string functions are preserved
    assert stage.verify.functions[0] == "test_module:simple_function"
    assert stage.save.functions[0] == "test_module:save_function"


def test_mixed_function_formats():
    """Test that mixing string and FunctionCall formats works."""
    scenario_data = {
        "stages": [
            {
                "name": "test_stage",
                "url": "https://example.com",
                "verify": {
                    "functions": [
                        "test_module:simple_function",
                        {
                            "function": "test_module:function_with_kwargs",
                            "kwargs": {"param": "value"}
                        }
                    ]
                },
                "save": {
                    "functions": [
                        "test_module:simple_save_function",
                        {
                            "function": "test_module:save_with_kwargs",
                            "kwargs": {"field": "data"}
                        }
                    ]
                }
            }
        ]
    }
    
    scenario = Scenario.model_validate(scenario_data)
    
    assert len(scenario.stages) == 1
    stage = scenario.stages[0]
    
    # Test mixed verify functions
    verify_functions = stage.verify.functions
    assert verify_functions[0] == "test_module:simple_function"
    assert isinstance(verify_functions[1], FunctionCall)
    assert verify_functions[1].function == "test_module:function_with_kwargs"
    assert verify_functions[1].kwargs == {"param": "value"}
    
    # Test mixed save functions
    save_functions = stage.save.functions
    assert save_functions[0] == "test_module:simple_save_function"
    assert isinstance(save_functions[1], FunctionCall)
    assert save_functions[1].function == "test_module:save_with_kwargs"
    assert save_functions[1].kwargs == {"field": "data"}