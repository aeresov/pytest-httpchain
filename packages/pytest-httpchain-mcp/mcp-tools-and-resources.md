# MCP Server Tools and Resources for pytest-httpchain

This document outlines the proposed MCP server tools and resources that would be beneficial for AI code agents working with the pytest-httpchain HTTP API testing framework.

## Core Testing Tools

### 1. validate_scenario
- Validate JSON test scenario syntax and structure
- Check for required fields, proper stage definitions
- Verify JSONRef paths are valid

### 2. run_test_scenario
- Execute a specific test scenario file
- Return execution results with detailed stage outcomes
- Support dry-run mode for validation without execution

### 3. create_test_scenario
- Generate test scenario JSON from natural language description
- Support templates for common patterns (CRUD, auth flows)
- Auto-detect required fixtures from context

### 4. analyze_api_from_openapi
- Parse OpenAPI/Swagger specs to generate test scenarios
- Create comprehensive test coverage for all endpoints
- Generate edge case and error scenarios

## Variable & Context Management

### 5. extract_variables
- Parse scenario to identify all variable references
- Show variable flow between stages
- Detect undefined variables

### 6. suggest_jmespath
- Given JSON response, suggest JMESPath expressions
- Test JMESPath expressions against sample data
- Convert between dot notation and JMESPath

### 7. manage_fixtures
- List available pytest fixtures in project
- Suggest appropriate fixtures for scenarios
- Create fixture boilerplate code

## Response Verification Tools

### 8. generate_json_schema
- Create JSON schema from sample response
- Validate response against schema
- Convert between different schema formats

### 9. create_verification_function
- Generate Python verification functions
- Support complex assertions and business logic
- Template common verification patterns

### 10. suggest_assertions
- Analyze response data to suggest verifications
- Recommend status codes, headers, body checks
- Generate regex patterns for text validation

## Request Building Tools

### 11. build_request_body
- Convert between different body formats (JSON, form, multipart)
- Generate sample request payloads
- Validate against API specifications

### 12. manage_authentication
- Generate auth configuration (Bearer, Basic, OAuth)
- Create custom auth function templates
- Manage credential storage patterns

## Debugging & Analysis

### 13. trace_execution
- Show detailed execution flow with variable substitution
- Display HTTP request/response details
- Identify stage failures and their causes

### 14. compare_responses
- Diff responses between test runs
- Identify regression issues
- Generate change reports

### 15. analyze_test_coverage
- Map which API endpoints are tested
- Identify untested paths and methods
- Generate coverage reports

## Code Generation Tools

### 16. convert_from_postman
- Import Postman collections to pytest-httpchain format
- Preserve environment variables and pre-scripts
- Handle authentication flows

### 17. convert_from_curl
- Parse curl commands to create test stages
- Support batch conversion from shell history
- Preserve headers and authentication

### 18. generate_conftest
- Create conftest.py with common fixtures
- Generate helper functions for scenarios
- Setup test environment configuration

## Resources (Read-only)

### 19. scenario_examples
- Provide categorized examples (auth, CRUD, pagination)
- Show best practices and patterns
- Include anti-patterns to avoid

### 20. jinja_templates
- Common Jinja2 expressions for httpchain
- Variable manipulation patterns
- Date/time formatting examples

### 21. error_explanations
- Detailed explanations of common errors
- Troubleshooting guides with solutions
- Debug strategies for different failure types

## Project Management Tools

### 22. scaffold_project
- Create project structure with best practices
- Generate initial test scenarios
- Setup CI/CD configurations

### 23. migrate_scenarios
- Update scenarios to newer pytest-httpchain versions
- Convert between different test formats
- Refactor common patterns

### 24. optimize_scenarios
- Identify redundant stages or requests
- Suggest performance improvements
- Recommend parallel execution strategies

## Integration Tools

### 25. generate_github_workflow
- Create GitHub Actions for test execution
- Setup matrix testing for different environments
- Configure result reporting

### 26. docker_compose_generator
- Generate docker-compose for test environments
- Include mock servers and databases
- Setup network configurations

## Implementation Priority

The MCP server should prioritize implementing tools in the following order:

1. **High Priority** (Core functionality)
   - validate_scenario
   - create_test_scenario
   - run_test_scenario
   - extract_variables
   - scenario_examples

2. **Medium Priority** (Enhanced productivity)
   - suggest_jmespath
   - generate_json_schema
   - convert_from_curl
   - trace_execution
   - manage_fixtures

3. **Low Priority** (Advanced features)
   - analyze_api_from_openapi
   - convert_from_postman
   - optimize_scenarios
   - docker_compose_generator
   - analyze_test_coverage

## Benefits for AI Agents

These tools would significantly enhance an AI agent's ability to:
- Create and validate test scenarios efficiently
- Debug and fix failing tests
- Convert existing API tests from other formats
- Maintain and refactor test suites
- Provide intelligent suggestions for test improvements
- Understand test execution flow and dependencies
- Generate comprehensive test coverage from API specifications