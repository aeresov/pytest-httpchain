# Parallel Execution

Parallel execution allows running multiple HTTP requests concurrently within a single stage. This is useful for load testing, stress testing, or bulk operations.

## Repeat Mode

Execute the same request N times in parallel:

```json
{
    "stages": [
        {
            "name": "load_test",
            "parallel": {
                "repeat": 100,
                "max_concurrency": 10
            },
            "request": {
                "url": "https://api.example.com/health"
            },
            "response": [
                {"verify": {"status": 200}}
            ]
        }
    ]
}
```

This sends 100 requests with up to 10 concurrent connections.

## Foreach Mode

Execute a request for each parameter combination in parallel:

```json
{
    "stages": [
        {
            "name": "bulk_fetch",
            "parallel": {
                "foreach": [
                    {
                        "individual": {
                            "user_id": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
                        }
                    }
                ],
                "max_concurrency": 5
            },
            "request": {
                "url": "https://api.example.com/users/{{ user_id }}"
            },
            "response": [
                {"verify": {"status": 200}}
            ]
        }
    ]
}
```

## Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `repeat` | integer | - | Number of times to repeat the request |
| `foreach` | array | - | Parameter sets to iterate over |
| `max_concurrency` | integer | 10 | Maximum concurrent requests |
| `calls_per_sec` | integer | null | Rate limit (requests per second) |
| `max_rate_limit_delay` | integer | 60 | Max seconds a request waits for a rate-limit slot before failing |

> The total number of iterations a single stage may run (`repeat`, or the
> product of its `foreach` parameter sets) is capped by the `max_parallel_iterations`
> ini option (default `10000`); a stage that exceeds it fails at runtime (the
> stage errors before any request is sent). This is
> a project-wide setting, not a `parallel` block field — set it in `pytest.ini` or
> `pyproject.toml` (see Getting Started).

## Rate Limiting

Control request rate to avoid overwhelming the target server:

```json
{
    "parallel": {
        "repeat": 1000,
        "max_concurrency": 50,
        "calls_per_sec": 100
    }
}
```

This sends 1000 requests with:
- Up to 50 concurrent connections
- Maximum 100 requests per second

The limit is global across all workers. When a request cannot get a slot, it waits up to `max_rate_limit_delay` seconds (default 60); if none frees up in that window the request fails with a `Rate limit exceeded` error.

## Foreach with Combinations

```json
{
    "stages": [
        {
            "name": "test_matrix",
            "parallel": {
                "foreach": [
                    {
                        "combinations": [
                            {"env": "dev", "region": "us-east"},
                            {"env": "dev", "region": "eu-west"},
                            {"env": "staging", "region": "us-east"},
                            {"env": "staging", "region": "eu-west"}
                        ]
                    }
                ],
                "max_concurrency": 4
            },
            "request": {
                "url": "https://{{ env }}.example.com/{{ region }}/health"
            }
        }
    ]
}
```

## Dynamic Values with Templates

```json
{
    "substitutions": [
        {
            "vars": {
                "user_ids": [101, 102, 103, 104, 105]
            }
        }
    ],
    "stages": [
        {
            "name": "parallel_updates",
            "fixtures": ["now_utc"],
            "parallel": {
                "foreach": [
                    {
                        "individual": {
                            "id": "{{ user_ids }}"
                        }
                    }
                ],
                "max_concurrency": 3
            },
            "request": {
                "url": "https://api.example.com/users/{{ id }}",
                "method": "PATCH",
                "body": {
                    "json": {
                        "last_accessed": "{{ str(now_utc) }}"
                    }
                }
            }
        }
    ]
}
```

> `datetime` is not available inside `{{ }}` expressions. For a timestamp, expose a fixture (e.g. `now_utc` returning `datetime.now()` from a `conftest.py`) and reference it with `fixtures: ["now_utc"]`, as shown above.

## Load Testing Example

```json
{
    "description": "API load test scenario",
    "substitutions": [
        {
            "vars": {
                "base_url": "https://api.example.com",
                "total_requests": 500,
                "concurrent": 25,
                "rate": 50
            }
        }
    ],
    "stages": [
        {
            "name": "warmup",
            "parallel": {
                "repeat": 10,
                "max_concurrency": 2
            },
            "request": {
                "url": "{{ base_url }}/health"
            }
        },
        {
            "name": "sustained_load",
            "parallel": {
                "repeat": "{{ total_requests }}",
                "max_concurrency": "{{ concurrent }}",
                "calls_per_sec": "{{ rate }}"
            },
            "request": {
                "url": "{{ base_url }}/api/endpoint",
                "method": "POST",
                "body": {
                    "json": {"test": true}
                }
            },
            "response": [
                {
                    "verify": {
                        "status": 200
                    }
                }
            ]
        }
    ]
}
```

## Notes

- Parallel execution runs within a single stage
- Response verification applies to all parallel requests
- Save operations may behave differently in parallel mode (last write wins)
- Use rate limiting to avoid overwhelming servers or hitting rate limits
- Monitor memory usage with very high concurrency values
