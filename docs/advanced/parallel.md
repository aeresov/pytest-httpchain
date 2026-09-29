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

Over HTTP/1.1, `max_concurrency` is what bounds the connections: the scenario's HTTP client opens
as many as there are requests in flight, unless its
[`client.max_connections`](../usage/scenarios.md#client-configuration) sets a limit. httpx's own
default of 100 connections, which the client used to keep, held a stage with a higher
`max_concurrency` to 100 requests at a time, the rest waiting for a connection: 150 concurrent
requests to an endpoint answering in a second took over two seconds.

HTTP/2 works differently. The client offers it by default, and an HTTPS server that negotiates it
gets all the requests to it on a single connection, as streams, of which httpx keeps at most 100
open at once (fewer if the server allows fewer). A stage above that runs 100 requests at a time
whatever its `max_concurrency`, the rest waiting for a stream, and no `client.max_connections`
changes that. To have more in flight against such a server, set
[`client.http2`](../usage/scenarios.md#client-configuration) to `false`: every request then gets
an HTTP/1.1 connection of its own. That also sidesteps a weakness of httpx's HTTP/2 connection
under threads: with close to 100 iterations opening streams on it at once, one of them now and then
fails with an HTTP/2 protocol error or a reset stream, through no fault of the server.

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
| `max_concurrency` | integer | 10 | Maximum concurrent requests (and HTTP/1.1 connections, unless `client.max_connections` is lower); at most 100 run at once over HTTP/2 |
| `calls_per_sec` | integer | null | Rate limit (requests per second) |
| `max_rate_limit_delay` | integer | 60 | Max seconds a request waits for a rate-limit slot before failing |
| `collect_saves` | boolean | false | Keep every iteration's saves: each saved name becomes a list, one entry per iteration (see [Collecting every iteration's saves](#collecting-every-iterations-saves)) |
| `thresholds` | object | null | Limits on the stage's success ratio, latency and throughput, checked once every iteration has ended (see [Thresholds](#thresholds)) |
| `stats_as` | string | null | Save the stage's stats as an object under this name, for the stages after it (see [Saving the stats](#saving-the-stats)) |

> The total number of iterations a single stage may run (`repeat`, or the
> product of its `foreach` parameter sets) is capped by the `httpchain_max_parallel_iterations`
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

The limit is shared by the stage's concurrent iterations, and by them only: each stage execution gets a fresh in-process budget, so consecutive stages, other scenarios, and pytest-xdist workers are not throttled against each other (with `-n 4`, four scenarios using `calls_per_sec: 100` can together reach 400 rps against one API). When a request cannot get a slot, it waits up to `max_rate_limit_delay` seconds (default 60); if none frees up in that window the request fails with a `Rate limit exceeded` error.

A `calls_per_sec` template that renders to `null` fails the stage before any request is sent, rather than running it unthrottled (see [Templates that render to `null`](../usage/substitutions.md#templates-that-render-to-null)).

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

As in stage [`parametrize`](parametrization.md#template-expressions-in-parameters),
the list can also be one template, such as `"combinations": "{{ matrix }}"` with
`matrix` a list of objects in scenario `vars`. It is resolved each time the stage
runs.

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
                        "last_accessed": "{{ now() }}"
                    }
                }
            }
        }
    ]
}
```

> The request renders when the iteration starts, and anew for each attempt a stage's [`retry`](retry.md) makes, so each call's `now()` reads the clock then. With `calls_per_sec`, that is before the attempt waits for its rate-limit slot: the request goes out up to `max_rate_limit_delay` seconds (60 by default) after its `now()` or `timestamp()` was read, which a signature with a freshness window has to allow for. For one timestamp shared by every call, bind it in the stage's `substitutions`, which render once, before the iterations start: `{"vars": {"started": "{{ now() }}"}}`.

## Collecting every iteration's saves

By default the iterations' saves merge into one value per name, so a stage that
creates several resources keeps the id of only one of them. With
`"collect_saves": true`, every name any iteration saves becomes a list with one
entry per iteration, which is what a later stage needs to clean them all up:

```json
{
    "client": {"base_url": "https://api.example.com"},
    "stages": [
        {
            "name": "create_resources",
            "parallel": {
                "foreach": [{"individual": {"name": ["alpha", "beta", "gamma"]}}],
                "collect_saves": true
            },
            "request": {
                "method": "POST",
                "url": "/resources",
                "body": {"json": {"name": "{{ name }}"}}
            },
            "response": [
                {"verify": {"status": 201}},
                {"save": {"jmespath": {"created_ids": "id"}}}
            ]
        },
        {
            "name": "list_resources",
            "request": {"url": "/resources"},
            "response": [
                {"verify": {"status": 200, "expressions": ["{{ len(created_ids) == 3 }}"]}}
            ]
        },
        {
            "name": "delete_resources",
            "always_run": "{{ exists('created_ids') }}",
            "parallel": {
                "foreach": [{"individual": {"id": "{{ created_ids }}"}}]
            },
            "request": {"method": "DELETE", "url": "/resources/{{ id }}"},
            "response": [
                {"verify": {"status": 204}}
            ]
        }
    ]
}
```

`created_ids` is a list of the three ids, and `delete_resources` sends one
`DELETE` for each, in parallel. Its `always_run` lets it run after a later stage
fails too, as long as `create_resources` passed. If `create_resources` itself fails,
it saves nothing (see the notes below), so `created_ids` does not exist, and
`exists()` skips the cleanup instead of failing it on an undefined name, as
`"always_run": true` would.

- **One entry per iteration, in iteration order**: entry `i` is iteration `i`'s,
  whichever iteration finished first, so it lines up with the iteration's
  parameters (above, `created_ids[0]` is `alpha`'s id). A `repeat` counts its
  iterations from 0. A `foreach` crosses its steps with the first step's values
  going fastest: `[{"individual": {"a": [1, 2]}}, {"individual": {"b": ["x", "y"]}}]`
  runs `(1, x)`, `(2, x)`, `(1, y)`, `(2, y)`.
- **`null` where an iteration saved nothing under a name** that another one
  saved, as a user function's save may return different names each time. A
  saved `null` (a JMESPath save of a missing key) looks the same, and so does
  an iteration that failed in a stage a `min_success_ratio` below 1 lets pass
  (see [Thresholds](#thresholds)).
- **A list even for one iteration**, such as a `foreach` over a list with one
  item: the shape does not depend on how many items there are.
- **Inside the stage nothing changes**: an iteration's later response steps read
  the value it saved itself, not the list. The stages after it read the lists,
  and `pytest-httpchain show` and `graph` list the names as this stage's saves,
  as for any stage.
- `collect_saves` can be a template, rendered with the rest of the `parallel`
  config, before any request. It sees what the stage's `skip_if` sees, the
  stage's own substitutions included, but not its `foreach` parameters: the
  whole stage collects or none of it does, and `validate` reports a `foreach`
  parameter there as undefined (`HTTPCHAIN003`). It must render to `true` or
  `false`, and the numbers `1` and `0` count as `true` and `false`, as they do
  in `client.http2` and the other boolean settings (a `skip_if` template is
  stricter: it is a condition, and must evaluate to a boolean itself). Any other
  value, `null` or text such as `"true"` included, fails the stage before any
  request is sent.

## Stats and thresholds

A parallel stage is often a small load test, where passing is not all there
is to know. Every parallel stage measures its iterations, sums them up in its
report, and can be held to limits on the numbers.

### The Parallel Summary

The stage's report gets a `Parallel Summary` section, shown where its
`HTTP Request` and `HTTP Response` sections are: in a failed stage's report,
and in a passed one's with `-rP` or `-rA`, under pytest-xdist too. A stage
without `parallel` has none.

```text
------------------------------- Parallel Summary -------------------------------
Iterations:     9 (6 passed, 3 failed, 0 cancelled)
Success ratio:  0.6667
Wall time:      34.25 ms
Throughput:     262.79 completed iterations/s, 175.19 passed iterations/s
Latency (ms):   min 6.01, mean 8.94, p50 7.75, p95 12.85, p99 12.85, max 12.85
Thresholds:     min_success_ratio 0.6: met, 0.666667 (6 of 9 iterations passed)
                max_p95_ms 60000: met, 12.8483 ms
```

What it counts and measures:

- **Iterations**, by how each ended: *passed*, its response steps all
  passed; *failed*; *cancelled*, stopped by another iteration that had ended
  the stage, before it sent its request or while it waited for a
  rate-limit slot or to retry; and *skipped*, listed only when a user
  function skipped or xfailed one.
- An iteration's **duration** is the time its requests spent in the HTTP
  client: from handing one to it to having its whole response, redirects and
  an auth flow's round trips included, summed over the attempts a
  [`retry`](retry.md) makes. It leaves out the wait for a `calls_per_sec`
  slot, which would otherwise make a rate-limited server read as a slow one,
  the wait between attempts, rendering the request, and the response steps.
- The client's own waits count in the duration too, though the server
  causes none of them: opening a connection (TCP, TLS, a proxy's tunnel),
  and waiting for one, which an iteration does when
  [`client.max_connections`](../usage/scenarios.md#client-configuration) is
  below `max_concurrency`, or for an HTTP/2 stream, past 100 at a time (see
  [Repeat Mode](#repeat-mode)). For a latency that is the server's alone,
  leave `max_connections` unset or at least `max_concurrency`, set
  `max_keepalive_connections` at least as high, so that connections are
  reused rather than opened anew, and keep an HTTP/2 stage within 100 at a
  time or set `client.http2` to `false`.
- **Latency** is the durations of the iterations that passed. A failure's
  duration, a timeout's whole wait or a refused connection's next to nothing,
  says nothing of how fast the server answers. Its percentiles are
  *nearest-rank*: the p-th percentile is the smallest duration that at least
  p % of them are at most, always one that was measured, never an
  interpolation. So with fewer than 20 passed iterations p95 is the maximum,
  and with fewer than 100 p99 is.
- **Wall time** runs from the first iteration's start to the last one's end,
  every wait included.
- **Throughput** is the iterations per second of wall time: the completed
  ones, every iteration that ran to its end, passed, failed or skipped, all
  but the cancelled; and the passed ones, which `min_rps` holds, as a failed
  iteration adds no latency either.
- **Success ratio** is the passed iterations' share of all the stage's
  iterations.

### Thresholds

`thresholds` fails a stage whose numbers do not reach its limits, checked
once every iteration has ended:

```json
{
    "client": {"base_url": "https://api.example.com"},
    "stages": [
        {
            "name": "sustained_load",
            "parallel": {
                "repeat": 500,
                "max_concurrency": 25,
                "thresholds": {
                    "min_success_ratio": 0.99,
                    "max_p95_ms": 300,
                    "max_p99_ms": 800,
                    "min_rps": 50
                }
            },
            "request": {"url": "/health"},
            "response": [{"verify": {"status": 200}}]
        }
    ]
}
```

| Threshold | Limit | Met when |
|-----------|-------|----------|
| `min_success_ratio` | a number from 0 to 1 | at least this share of the iterations passed |
| `max_mean_ms` | milliseconds, above 0 | the passed iterations' mean latency is at most this |
| `max_p50_ms` | milliseconds, above 0 | their median latency is at most this |
| `max_p95_ms` | milliseconds, above 0 | their 95th percentile latency is at most this |
| `max_p99_ms` | milliseconds, above 0 | their 99th percentile latency is at most this |
| `min_rps` | iterations per second, above 0 | the passed iterations' throughput is at least this |

Each is optional, and a limit is met at equality. A latency limit with no
passed iteration to measure it over is not met. A stage that misses any
fails naming every one it missed, with the value measured and the limit,
as a verify step lists its failed checks:

```text
2 parallel thresholds not met:
  1. min_success_ratio: 0.666667 (6 of 9 iterations passed), below the limit 0.9
  2. max_p95_ms: 19.2544 ms, above the limit 0.001 ms
3 failed iterations:
  iteration 3: Status code doesn't match: expected 200, got 500
  iteration 5: Status code doesn't match: expected 200, got 500
  iteration 8: Status code doesn't match: expected 200, got 500
```

**Failed iterations.** Without `min_success_ratio`, or with it at 1, a
failing iteration fails the stage as it always has: it cancels the rest, the
stage fails with `Parallel execution failed at iteration N: ...`, and no
threshold is checked. Below 1 the stage runs on after failures instead:

- No iteration is cancelled for another's failure: every one runs.
- Once all have ended, the stage fails if fewer passed than the ratio asks,
  listing the first five failed iterations, lowest index first, after the
  thresholds, and the report shows the exchange of the first one that sent
  a request. Otherwise it passes, counting the failures in its summary (each
  is also logged at `INFO` as it happens). With no iteration passed, the
  report shows that failure's exchange however the stage ends.
- It saves what the passed iterations saved. Merged, the highest *passed*
  iteration index wins a name; with `collect_saves` the lists keep an entry
  per iteration, `null` where one failed, so entry `i` stays iteration `i`'s.
- `0` passes a stage whose every iteration failed: for a run whose numbers
  are all it is for. The names its `jmespath`, `regex` and `substitutions`
  saves declare are saved all the same, `null` merged and a list of `null`s
  with `collect_saves`, so a later stage reading one finds it, as `validate`
  says it will. A name only a `user_functions` save returns is saved only
  if a passed iteration returned it.
- Two things still end the stage at once, as without it: a user function's
  `pytest.skip()`, `xfail()` or `fail()`, and a context manager a factory
  fixture returned raising on exit, since what a failed rollback left
  behind must not pass unnoticed. The failure an exit error ends it with
  lists, after its own message, the iterations that failed on their own,
  the first five, lowest index first (`2 failed iterations tolerated by
  min_success_ratio:`). The [HAR file](../getting-started.md#har-export)
  has every request they sent, however the stage ends.

Each threshold is a number or a template, part of the `parallel` config: it
renders with the rest of it before any request is sent, against what the
stage's `skip_if` sees, so a `foreach` parameter there is undefined, which
`validate` reports (`HTTPCHAIN003`). A limit that renders to `null` (see
[Templates that render to `null`](../usage/substitutions.md#templates-that-render-to-null)),
to text, to `true` or `false`, or out of its range fails the stage before
its iterations send anything, as a limit written so fails validation.

### Saving the stats

`stats_as` saves the stage's stats under a name, as an object the stages
after it read like any saved object:

```json
{
    "client": {"base_url": "https://api.example.com"},
    "stages": [
        {
            "name": "search_load",
            "parallel": {
                "repeat": 200,
                "max_concurrency": 20,
                "thresholds": {"min_success_ratio": 0.95},
                "stats_as": "search"
            },
            "request": {"url": "/search?q=widgets"},
            "response": [{"verify": {"status": 200}}]
        },
        {
            "name": "publish_results",
            "request": {
                "method": "POST",
                "url": "/metrics",
                "body": {"json": {"p95_ms": "{{ search.p95_ms }}", "rps": "{{ search.rps }}", "failed": "{{ search.failed }}"}}
            },
            "response": [{"verify": {"status": 204}}]
        }
    ]
}
```

| Key | Value |
|-----|-------|
| `iterations`, `passed`, `failed` | how many iterations the stage ran, passed and failed |
| `success_ratio` | from 0 to 1 |
| `wall_ms` | the wall time, in milliseconds |
| `rps` | the throughput of passed iterations, per second, which `min_rps` holds |
| `completed_rps` | the throughput of completed iterations, passed or failed, per second |
| `min_ms`, `mean_ms`, `p50_ms`, `p95_ms`, `p99_ms`, `max_ms` | the latency, in milliseconds; `null` when no iteration passed |

The values are not rounded. The stats are saved as the stage's other saves
are: only when it passes, so a stage that fails, a threshold not met
included, saves none, and none of its iterations was cancelled or skipped.
They are one object, never a list, `collect_saves` or not. None of the
stage's own steps can read them, as they exist once every iteration has
ended: `validate` reports a reference to the name there (`HTTPCHAIN004`),
and `show` and `graph` list it among the stage's saves, for the stages after
it. The name is a variable name as written, never a template. Saved after
the response steps' saves, the stats replace a save of the same name, which
`validate` warns of (`HTTPCHAIN040`), and so does the stage, once, when
only a user function's save returned the name, which `validate` cannot see.

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
                "calls_per_sec": "{{ rate }}",
                "thresholds": {"min_success_ratio": 0.99, "max_p95_ms": 500}
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
- Saves merge in **iteration order**, not completion order: when several
  iterations save the same key, the highest iteration index wins, whichever
  iteration finished first. With `collect_saves: true` each key is a list
  instead, one entry per iteration (see
  [Collecting every iteration's saves](#collecting-every-iterations-saves))
- Saves are **all or nothing**: if any iteration fails, the stage commits no
  saves at all, so the context never carries a timing-dependent subset. That
  holds with `collect_saves` too: when one iteration of a stage creating
  resources fails, the ids the others saved are dropped with the rest, and no
  later stage can delete those resources by them. A
  [`min_success_ratio`](#thresholds) below 1 is the exception: a stage it
  lets pass saves what its passed iterations saved
- The first failing iteration cancels the rest: iterations not sent yet are
  never sent (those already in flight run to their end), and the stage fails
  with `Parallel execution failed at iteration N: ...`. A `min_success_ratio`
  below 1 runs every iteration instead, and fails the stage once they have
  all ended if too few passed
- Iterations do not see one another's saves; each resolves against the stage
  context plus its own parameters
- With a stage [`retry`](retry.md#with-parallel), each iteration makes its own
  attempts, and each attempt takes a `calls_per_sec` slot
- Use rate limiting to avoid overwhelming servers or hitting rate limits
- Monitor memory usage with very high concurrency values

## Running scenarios in parallel with pytest-xdist

The `parallel` config above parallelizes iterations *within* one stage. To run
whole **scenarios** in parallel, use [pytest-xdist](https://pytest-xdist.readthedocs.io/)
with a distribution mode that keeps each scenario's stages together on one worker:

```bash
pytest -n 4 --dist loadscope   # groups by class — one scenario per group
pytest -n 4 --dist loadfile    # groups by file — one scenario per file
pytest -n 4 --dist loadgroup   # scenario classes are auto-grouped by the plugin
```

A scenario's stages form an ordered chain over shared state, so they must all
run on the same worker, in order. Modes that distribute tests individually —
`--dist load` (the default when only `-n` is given), `--dist each`, and
`--dist worksteal` — would scatter the chain across workers and are rejected
at collection time (the error names the supported modes). Different scenarios
never share state, so scenario-level distribution is safe under the supported
modes.

The supported modes decide what runs together from each test's node id (plus,
under `--dist loadgroup`, its `xdist_group` marks), so a few things a scenario
puts there are rejected at collection too, rather than letting one stage run
on another worker without the earlier stages' saved values. The coded ones are
validator errors: `pytest-httpchain validate` reports them, and they fail
collection in every run, with or without xdist.

- **`xdist_group` belongs in the scenario's `marks`.** Under `--dist loadgroup`
  the plugin gives each scenario a group of its own. Declare one yourself to
  put several scenarios in the same group, for example to keep the scenarios
  that share a test database on one worker, one after the other:

    ```json
    {
        "marks": ["xdist_group('db')"],
        "stages": []
    }
    ```

    The plugin then adds no group of its own, and every stage inherits
    yours. Keep `]` out of the name
    ([`HTTPCHAIN033`](../diagnostics.md)): xdist ignores a group whose name
    has a `]` after its last `@`, so no two stages would be kept together.
    An `xdist_group` in a *stage's* `marks` is an error
    ([`HTTPCHAIN031`](../diagnostics.md)) unless it repeats a group the
    scenario declares: xdist joins every group on a test into one name, so a
    stage with a group of its own would be scheduled alone.

- **A stage name cannot contain `::`**
  ([`HTTPCHAIN032`](../diagnostics.md)). The name becomes part of the stage's
  node id, where `::` separates its parts: `--dist loadscope` groups a test
  by its node id up to the last `::`, and pytest could not run that stage by
  its node id either. Rename the stage, e.g. `Users: list` or `Users.list`.

- **Under `--dist loadscope`, a test id in brackets cannot contain `::`
  either.** This covers a parametrize step's explicit `ids` and the ids built
  from its values, such as `"::1"`, and likewise the ids of a fixture's
  `params`. pytest handles such an id itself, so only this mode rejects it:
  give the step or the fixture explicit `ids`, or use `loadfile` or
  `loadgroup`.
