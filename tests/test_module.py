def test_function(response):
    return True

def function_with_kwargs(response, expected_status=200, timeout=5.0):
    return response.status_code == expected_status

def verify_function(response, expected_text="Hello", case_sensitive=False):
    return expected_text in response.text

def save_function(response, extract_key="user_id", default_value=0):
    return {extract_key: default_value}

def simple_function(response):
    return True

def simple_save_function(response):
    return {"saved": True}

def save_with_kwargs(response, field="data", default_value="unknown"):
    return {field: default_value}

def valid_function(response):
    return True