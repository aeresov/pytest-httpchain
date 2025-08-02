# Usage Examples

## Basic Setup

Install normally via package manager of your choice from PyPi:

```bash
pip install pytest-http
```

## Pytest features

For pytest, scenario acts like a test module with one test class; stages act like test methods of that class.\
Scenario-level pytest markers act like they are applied to python test class.\
Stage-level pytest markers act like they are applied to python test function.\
Scenario-level pytest fixtures act like they are applied to each test method.\
Stage-level pytest fixtures act like they are applied to python test method.

```python
# conftest.py
import pytest

@pytest.fixture
def prepare_boilerplate():
    # setup actions
    yield
    # teardown actions

@pytest.fixture
def string_value():
    return "answer"

@pytest.fixture
def int_value():
    return 42
```

```json
{
    "marks": ["xfail", "usefixtures('prepare_boilerplate')"],
    "fixtures": ["string_value"],
    "stages": [
        {
            "name": "dummy",
            "marks": ["skip(reason='not implemented')"],
            "fixtures": ["int_value"],
            "request": {
                "url": "https://api.example.com"
            }
        }
    ]
}
```

In this example:

-   scenario has class markers `xfail` and `usefixtures('prepare_boilerplate')`
-   stage `dummy` has function marker `skip(reason='not implemented')`
-   stage `dummy` got fixtures `string_value` (from scenario) and `int_value` (individual)

## Declarative format

Example of using `$ref` and greedy props merge.

```json
// requests.json
{
    "login": {
        "request": {
            "url": "https://api.example.com/login"
        }
    }
}
```

```json
// stages.json
{
    "auth": {
        "$ref": "requests.json#/login",
        "request": {
            "params": {
                "username": "John Dow"
            }
        }
    }
}
```

```json
// test_scenario.http.json
{
    "stages": [
        {
            "name": "Startup stage",
            "$ref": "stages.json#/auth"
        }
    ]
}
```

## Multi-stage tests

## Common data context and variable substitution

## User functions

## JMESPath support

## JSON schema support

## Troubleshooting

[Common errors and solutions]
