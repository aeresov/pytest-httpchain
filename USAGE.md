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

In this example:

-   scenario has class markers `xfail` and `usefixtures('prepare_boilerplate')`
-   stage `dummy` has function marker `skip(reason='not implemented')`
-   stage `dummy` got fixtures `string_value` (from scenario) and `int_value` (individual)

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

## Declarative format

Example of using `$ref` and greedy props merge.

`requests.json`
```json
{
    "login": {
        "request": {
            "url": "https://api.example.com/login"
        }
    }
}
```

`stages.json`
```json
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

`test_scenario.http.json`
```json
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

Stages are executed in the order they are listed.\
In case a stage fails, the rest of chain is stopped.\
If `always_run` field is set, the stage is executed regardless of previous errors (useful for cleanup).

```json
{
    "stages": [
        {
            "name": "login",
            "request": {
                "url": "https://api.example.com/login"
            },
            "verify": {
                "status": 200
            }
        },
        {
            "name": "operation",
            "request": {
                "url": "https://api.example.com/operation",
                "method": "POST"
            },
            "verify": {
                "status": 200
            }
        },
        {
            "name": "logout",
            "always_run": true,
            "request": {
                "url": "https://api.example.com/logout"
            }
        }
    ]
}
```

## Common data context and variable substitution

Common data context is a key-value storage available throughout scenario exection.\
In this example, common data context is seeded with var `id` at the beginning of scenario execution.\
Down the stages chain, common data context can be used in jinja-style variable substitutions.

```json
{
    "vars": {
        "id": 42
    },
    "stages": [
        {
            "name": "use resource",
            "request": {
                "url": "https://api.example.com/operation/{{ id }}",
                "method": "POST"
            },
            "verify": {
                "status": 200
            }
        }
    ]
}
```

## User functions

### Exctracting response data

```python
# utilities/save.py
import pytest
import requests
import xml.etree.ElementTree as ET

def extract_xml(response: requests.Response) -> dict[str, Any]
    content_type = response.headers.get("Content-Type", "").lower()
    is_xml_content = any(xml_type in content_type for xml_type in ["application/xml","text/xml"])
    if not is_xml_content:
        raise ValueError("not an XML response")
    if not response.content:
        raise ValueError("no content")
    root = ET.fromstring(response.text)
    first_author = root.find('.//book/author').text
    first_title = root.find('.//book/title').text
    return {"author": first_author, "title": first_title}
```

```json
{
    "stages": [
        {
            "name": "get book data",
            "request": {
                "url": "https://api.example.com"
            },
            "save": {
                "functions": ["utilities.save:extract_xml"]
            },
            "verify": {
                "vars": {
                    "author": "Jack London"
                }
            }
        }
    ]
}
```

### Verification

```python
# utilities/verify.py
import pytest
import requests
import xml.etree.ElementTree as ET

def check_xml(response: requests.Response, desired_author: str) -> bool
    content_type = response.headers.get("Content-Type", "").lower()
    is_xml_content = any(xml_type in content_type for xml_type in ["application/xml","text/xml"])
    if not is_xml_content:
        raise ValueError("not an XML response")
    if not response.content:
        raise ValueError("no content")
    root = ET.fromstring(response.text)
    first_author = root.find('.//book/author').text
    return first_author == desired_author
```

```json
{
    "vars": {
        "author": "Jack London"
    },
    "stages": [
        {
            "name": "get book data",
            "request": {
                "url": "https://api.example.com"
            },
            "verify": {
                "functions": [
                    {
                        "function": "utilities.verify:check_xml",
                        "kwargs": {
                            "desired_author": "{{ author }}"
                        }
                    }
                ]
            }
        }
    ]
}
```

### Authentication

```python
# utilities/auth.py
import boto3
import requests.auth
from requests_aws4auth import AWS4Auth

def dummy() -> AuthBase:
    return HTTPBasicAuth("dummy_user", "dummy_password")

def aws_sigv4(service: str, region: str) -> AuthBase
    session = boto3.Session()
    credentials = session.get_credentials()
    return AWS4Auth(credentials.access_key, credentials.secret_key, region, service, session_token=credentials.token)
```

```json
{
    "auth": "utilities.auth:dummy",
    "stages": [
        {
            "name": "internal operation",
            "request": {
                "url": "https://api.example.com"
            }
        },
        {
            "name": "aws operation",
            "request": {
                "url": "https://some_service.some_region.amazonaws.com",
                "method": "POST",
                "auth": {
                    "function": "utilities.auth:aws_sigv4",
                    "kwargs": {
                        "service": "some_service",
                        "region": "some_region"
                    }
                }
            },
            "verify": {
                "status": 200
            }
        }
    ]
}
```

## JMESPath support

In this example, stage extracts value directly from response JSON body and saves it into common data context.

```json
{
    "stages": [
        {
            "name": "internal operation",
            "request": {
                "url": "https://api.example.com"
            },
            "save": {
                "vars": {
                    "id": "collection[0].entity.id"
                }
            }
        }
    ]
}
```

## JSON schema support

In this example we verify response body using inline JSON schema.

```json
{
    "stages": [
        {
            "name": "internal operation",
            "request": {
                "url": "https://api.example.com"
            },
            "verify": {
                "body": {
                    "schema": {
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "type": "object",
                        "properties": {
                            "message": {
                                "type": "string"
                            }
                        },
                        "required": ["message"],
                        "additionalProperties": false
                    }
                }
            }
        }
    ]
}
```

## Troubleshooting

[Common errors and solutions]
