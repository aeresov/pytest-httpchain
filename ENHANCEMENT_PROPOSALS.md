# pytest-http Enhancement Proposals

This document outlines potential enhancements for the pytest-http plugin to expand its capabilities and improve usability for HTTP API testing.

## Current State Analysis

### Existing Strengths
- Declarative JSON-based test definitions
- Jinja2 template variable substitution
- JMESPath for response data extraction
- Multi-stage test scenarios with data passing
- AWS SigV4 authentication support
- Custom user functions for validation
- JSON reference ($ref) support
- Full pytest integration (fixtures, marks)

### Identified Limitations
- Limited request body format support (JSON only)
- Basic response validation capabilities
- Single authentication method (AWS only)
- No request configuration options (timeouts, retries)
- Missing conditional execution logic
- No built-in data generation utilities
- Limited error handling customization
- No environment-specific configurations

## Enhancement Proposals

### 🎯 High Priority

#### Flexible Authentication Support

**Problem**: Only AWS SigV4 authentication is supported.

**Solution**: Add multiple authentication methods:

```json
{
  "auth": {
    // Bearer token
    "type": "bearer",
    "token": "{{ env.API_TOKEN }}",
    
    // Basic authentication
    "type": "basic",
    "username": "{{ username }}",
    "password": "{{ password }}",
    
    // API key
    "type": "api_key",
    "header": "X-API-Key",
    "value": "{{ api_key }}",
    
    // OAuth2
    "type": "oauth2",
    "flow": "client_credentials",
    "token_url": "https://auth.example.com/token",
    "client_id": "{{ client_id }}",
    "client_secret": "{{ client_secret }}",
    
    // Custom auth handler
    "type": "custom",
    "handler": "myproject.auth.CustomAuth"
  }
}
```

**Implementation Notes**:
- Create auth handler interface for extensibility
- Cache OAuth2 tokens across stages
- Support auth inheritance from global config

### 🚀 Medium Priority

#### Built-in Data Generators

**Problem**: No easy way to generate dynamic test data.

**Solution**: Add generator functions:

```json
{
  "request": {
    "json": {
      "id": "{{ uuid() }}",
      "timestamp": "{{ now() }}",
      "date": "{{ today() }}",
      "random_int": "{{ random(1, 100) }}",
      "random_choice": "{{ choice(['A', 'B', 'C']) }}",
      "fake_name": "{{ faker.name() }}",
      "fake_email": "{{ faker.email() }}",
      "hash": "{{ md5(data) }}",
      "encoded": "{{ base64(data) }}"
    }
  }
}
```

**Implementation Notes**:
- Create secure random generators
- Optional faker library integration
- Cache generated values in variable context

#### Loop and Iteration Support

**Problem**: No way to repeat requests or iterate over data.

**Solution**: Add loop constructs:

```json
{
  "stages": [
    {
      "name": "poll_status",
      "repeat": {
        "count": 10,
        "delay": 1000,
        "until": "{{ response.status == 'complete' }}"
      },
      "request": { "url": "/job/{{ job_id }}/status" }
    },
    {
      "name": "process_users",
      "foreach": {
        "items": "{{ users }}",
        "as": "user",
        "parallel": true,
        "max_workers": 5
      },
      "request": { 
        "url": "/users/{{ user.id }}/process",
        "method": "POST"
      }
    }
  ]
}
```

**Implementation Notes**:
- Implement retry with exponential backoff
- Support parallel execution with thread pool
- Collect results from all iterations

#### Enhanced Reporting

- HTML test reports with request/response details
- Performance metrics dashboard
- Integration with APM tools
- Request/response logging options
- Failure screenshots for UI tests

## Configuration

New features should support configuration at multiple levels:
1. Global: `pytest.ini` or `pyproject.toml`
2. File: Test-specific settings
3. Stage: Per-request overrides
4. CLI: Command-line options

Example:
```ini
[tool.pytest.ini_options]
http_timeout = 30
http_retry_count = 3
http_environment = "dev"
```
