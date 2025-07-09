"""
Test the plugin's handling of kwargs functionality for functions.
"""

import json
from unittest.mock import Mock, patch

from pytest_http.pytest_plugin import call_function_with_kwargs


def test_call_function_with_kwargs():
    """Test the call_function_with_kwargs function."""
    # Mock response
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.text = "Hello World"
    mock_response.headers = {"content-type": "application/json"}
    
    # Mock function that accepts kwargs
    def test_function(response, expected_status=200, expected_text=""):
        return (response.status_code == expected_status and 
                expected_text in response.text)
    
    # Test with kwargs
    with patch('importlib.import_module') as mock_import:
        mock_module = Mock()
        mock_module.test_function = test_function
        mock_import.return_value = mock_module
        
        result = call_function_with_kwargs(
            "test_module:test_function",
            mock_response,
            {"expected_status": 200, "expected_text": "Hello"}
        )
        
        assert result is True
    
    # Test without kwargs (backward compatibility)
    with patch('importlib.import_module') as mock_import:
        mock_module = Mock()
        mock_module.test_function = test_function
        mock_import.return_value = mock_module
        
        result = call_function_with_kwargs(
            "test_module:test_function",
            mock_response
        )
        
        assert result is True


def test_call_function_with_kwargs_error_handling():
    """Test error handling in call_function_with_kwargs."""
    mock_response = Mock()
    
    # Test with non-existent function
    with patch('importlib.import_module') as mock_import:
        mock_import.side_effect = ImportError("Module not found")
        
        with pytest.raises(Exception) as exc_info:
            call_function_with_kwargs("nonexistent:function", mock_response)
        
        assert "Error executing function 'nonexistent:function'" in str(exc_info.value)
    
    # Test with function that raises an exception
    def failing_function(response, **kwargs):
        raise ValueError("Function failed")
    
    with patch('importlib.import_module') as mock_import:
        mock_module = Mock()
        mock_module.failing_function = failing_function
        mock_import.return_value = mock_module
        
        with pytest.raises(Exception) as exc_info:
            call_function_with_kwargs("test_module:failing_function", mock_response)
        
        assert "Error executing function 'test_module:failing_function'" in str(exc_info.value)


def test_function_validation_with_kwargs():
    """Test that function validation works with kwargs."""
    from pytest_http.models import validate_python_function_name
    
    # Test valid function name
    valid_func = "test_module:valid_function"
    result = validate_python_function_name(valid_func)
    assert result == valid_func
    
    # Test invalid function name (missing colon)
    with pytest.raises(ValueError, match="must use 'module:function' syntax"):
        validate_python_function_name("invalid_function")
    
    # Test invalid function name (missing module)
    with pytest.raises(ValueError, match="is missing module path"):
        validate_python_function_name(":function")
    
    # Test invalid function name (missing function)
    with pytest.raises(ValueError, match="is missing function name"):
        validate_python_function_name("module:")


# Mock pytest for testing
pytest = Mock()
pytest.raises = Mock()