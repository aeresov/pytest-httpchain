# Retries and Polling

A stage fails as soon as one of its checks does. Some APIs answer "not yet" before they answer
"yes": a job started in one call finishes in the background, a write shows up in a search a
moment later, a gateway turns a request away while a service restarts. `retry` makes a stage
attempt again after such a failure, waiting a little longer each time, until its response steps
pass or its attempts run out.

## Polling a job until it is done

The first stage starts a job, which the API reports as `pending` until it is done. The second
polls it: while the job is pending its `jmespath` check fails, and the stage waits and polls
again, up to ten times in all.

```json
{
    "client": {"base_url": "https://api.example.com"},
    "stages": [
        {
            "name": "submit",
            "request": {"method": "POST", "url": "/jobs", "body": {"json": {"report": "monthly"}}},
            "response": [
                {"verify": {"status": 202}},
                {"save": {"jmespath": {"job_id": "id"}}}
            ]
        },
        {
            "name": "wait_for_job",
            "retry": {"attempts": 10, "delay": 0.5, "backoff": 2, "max_delay": 5, "on": "verify"},
            "request": {"url": "/jobs/{{ job_id }}"},
            "response": [
                {"verify": {"status": 200, "jmespath": {"status": "done"}}},
                {"save": {"jmespath": {"result_url": "result.url"}}}
            ]
        },
        {
            "name": "download",
            "request": {"url": "{{ result_url }}"},
            "response": [{"verify": {"status": 200}}]
        }
    ]
}
```

The waits between the polls are 0.5, 1, 2 and 4 seconds, then 5 seconds each, the `max_delay`:
32.5 seconds of waiting at most, over the nine waits of ten attempts. Once the job is done,
`wait_for_job` passes like any stage, and `download` reads the `result_url` its last poll saved.

The job is started in a stage of its own. Retrying the `POST` would start a job on every
attempt: see [Non-idempotent requests](#non-idempotent-requests).

### Stopping when the job has failed

A `jmespath` check cannot tell "not yet" from "never": a job that ends up `failed` fails the
check as a pending one does, so `wait_for_job` polls it through all ten attempts, and 32.5 seconds
of waiting, before the stage fails. A verify function can tell them apart. It returns `False`
while the job is pending, which is retried, and once the job has failed it raises a
`VerificationError` with `retryable=False`, which ends the stage at once, whatever attempts are
left:

```python
# jobs.py
from pytest_httpchain.errors import VerificationError


def job_done(response):
    job = response.json()
    if job["status"] == "failed":
        raise VerificationError(f"job {job['id']} failed: {job.get('error')}", retryable=False)
    return job["status"] == "done"
```

```json
{
    "name": "wait_for_job",
    "retry": {"attempts": 10, "delay": 0.5, "backoff": 2, "max_delay": 5, "on": "verify"},
    "request": {"url": "/jobs/{{ job_id }}"},
    "response": [
        {"verify": {"status": 200, "user_functions": ["jobs:job_done"]}},
        {"save": {"jmespath": {"result_url": "result.url"}}}
    ]
}
```

The stage then fails with the function's message, the attempts made counted at its end:
`... job 7 failed: out of memory (after 3 attempts)`.
A save function gives up the same way, with a `SaveError(..., retryable=False)`, and a
`pytest.fail()` from either ends the stage at once too, as it does without `retry`.

## Settings

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `attempts` | integer, at least 1 | required | Attempts in all, the first included: `1` attempts once, as without `retry` |
| `delay` | number, at least 0 | `1` | Seconds to wait before the second attempt |
| `backoff` | number, at least 1 | `1` | What the wait is multiplied by after each attempt: `2` doubles it, `1` waits `delay` seconds every time |
| `max_delay` | number, at least 0 | `null` | Seconds a single wait lasts at most; `null` for no limit |
| `on` | `"verify"`, `"save"`, `"request"`, or a list of them | all three | The failures that make another attempt (see [Which failures are retried](#which-failures-are-retried)) |

The wait before attempt *n* + 1 is `delay × backoff^(n − 1)` seconds, and never more than
`max_delay`: with `"delay": 1, "backoff": 3, "max_delay": 5` the waits are 1, 3, 5, 5, ...
Each setting but `on` can be a template (see [Templates in retry](#templates-in-retry)).

## What an attempt is

- **The request is rendered anew** for each attempt: `uuid4()`, `now()` and every other template
  in the request are evaluated again, so each attempt sends a fresh request id and a current
  timestamp. What the stage's `substitutions` compute is computed once, before the first attempt:
  bind a value there for every attempt to share it.
- **Every response step runs**, in order, in a context of the attempt's own. A failed attempt's
  saves are discarded with it: the next attempt's steps do not see them, and only the attempt
  that passes commits its saves, as any stage that passes does.
- **The stage decides once**, before the first attempt, what it decides outside its request: its
  `always_run`, `substitutions`, `skip_if`, the `parallel` config and `retry` itself.
- **When an attempt passes**, the stage carries on as without `retry`. **When the last one
  fails**, the stage fails with that attempt's failure, which says how many attempts ran:

    ```
    JMESPath 'status' doesn't match: expected "done", got "pending" (after 10 attempts)
    ```

    Several failed checks are counted as ever, the attempts on the first line:
    `2 verification checks failed (after 10 attempts):`.

## Which failures are retried

`on` names the failures that make another attempt, by where they happen:

- **`verify`**: a verify step whose checks failed: a status, a header, a `jmespath` value, an
  expression, a body check or schema, or a verify function that returned false or raised a
  `VerificationError`.
- **`save`**: a save step that could not take its values from the response: a body that is not
  JSON (a gateway's HTML error page, say), a regex that does not match, a JMESPath expression that
  cannot be evaluated against the body, or a save function that did not return an object or raised
  a `SaveError`.
- **`request`**: the request timed out, its connection could not be made (refused, a host that
  does not resolve) or broke off, or the server closed it without a complete response.

A response is no request failure, whatever its status: to retry while the server answers `503`,
check the status in a verify step, and retry on `verify`, as the default does.

A user function says "not yet" as a check does: a verify function by returning `False`, and a
save function, which has no `False` to return, by raising a `SaveError` (a verify function may
raise a `VerificationError` too). Anything else it raises is a crash, which is not retried:

```python
# jobs.py
from pytest_httpchain.errors import SaveError


def result_url(response):
    job = response.json()
    if job["status"] != "done":
        raise SaveError(f"job {job['id']} is {job['status']}")
    return {"result_url": job["result"]["url"]}
```

To say "never" instead, a function raises the error with `retryable=False`, which ends the stage
at once (see [Stopping when the job has failed](#stopping-when-the-job-has-failed)).

Some failures are never retried, whatever `on` says, because the next attempt would fail the same
way, or wait as long again:

- **A template that cannot be rendered**, or renders a value its field or check does not take,
  `null` included, in the request or in a response step: template text where a status, a number
  or a regular expression belongs, say. A step with such a value is not retried even when others
  of its checks failed too: fix the template. Check what may not be there yet with
  `verify.jmespath`, which reads a missing value as `null` and fails as a check:
  `"jmespath": {"items[0].state": "ready"}`, not the expression
  `{{ items[0]['state'] == 'ready' }}`, which fails to render while a saved `items` is empty.
- **A user function that cannot be called**: its module does not import, has no function of that
  name, or names something that is not callable (`validate --deep` reports these,
  `HTTPCHAIN022`). **Or one that crashed**: raised anything but a `VerificationError` or a
  `SaveError` (a `KeyError`, a `TypeError`), which is the function's to fix. A function that only
  makes sense on a finished job checks for one first, and returns `False` or raises a
  `SaveError` until it sees it.
- **A body schema that cannot be read**: a `verify.body.schema` file that is not there or not a
  JSON Schema, a pointer that leads nowhere in it, or a `$ref` that does not resolve
  (`validate --deep` reports these, `HTTPCHAIN020` and `HTTPCHAIN021`). The body failing the
  schema is a check that failed, and is retried.
- **A request that could not be sent as written**: a header value httpx refuses, too many
  redirects, a body file that cannot be read, an auth function that raised.
- **A rate-limit slot that did not come** within `parallel.max_rate_limit_delay`: raise that
  delay instead.
- **A user function's `pytest.skip()`, `pytest.xfail()` or `pytest.fail()`**, which end the
  stage wherever they are called, as without `retry`.

A failure that is not retried ends the stage at once, on whichever attempt it happens, with the
attempts made so far counted in its message when there were more than one, a `pytest.fail()`'s
included. A skip's or an xfail's reason, which is no failure, is left as the function wrote it.

## With parallel

In a [`parallel`](parallel.md) stage each iteration makes its own attempts, with the same
schedule, and the stage passes once every iteration has passed: an iteration whose job is done
at the first poll does not wait for the others to be done. The first iteration to fail, out of
attempts or on a failure never retried, fails the stage and cancels the others, as a failing
iteration always does: an iteration waiting to retry stops waiting at once, and sends nothing
more.

An iteration waiting to retry keeps its worker: with more iterations than `max_concurrency`, the
iterations still queued start as the ones before them end, not while those wait. With
`calls_per_sec`, each attempt takes a slot of the rate limit, the first attempt of a single
iteration included: retries never send faster than the limit allows.

What the stage saves is each iteration's last attempt's, merged or, with
[`collect_saves`](parallel.md#collecting-every-iterations-saves), one entry per iteration, as
without `retry`.

## Templates in retry

`attempts`, `delay`, `backoff` and `max_delay` can be templates, rendered once per stage, before
the first attempt, against what the stage's `skip_if` sees: fixtures, parametrize parameters,
scenario substitutions, variables saved by earlier stages and the stage's own substitutions.
Not the `parallel.foreach` parameters, nor what the stage's own response saves: every attempt of
every iteration follows one schedule, set before the first response exists. `validate` reports
a name out of that scope (`HTTPCHAIN003`, or `HTTPCHAIN004` for a name the stage itself saves),
as the stage would fail on it.

```json
{
    "retry": {
        "attempts": "{{ int(env('POLL_ATTEMPTS', '30')) }}",
        "delay": "{{ poll_interval }}"
    }
}
```

A setting its stage cannot use, such as text, `0` attempts or a `true` (a flag is no number:
read as `1`, it would attempt once), fails the stage before any request is sent. A `max_delay` template that renders to `null` fails it too, rather than lift the
cap (see [Templates that render to `null`](../usage/substitutions.md#templates-that-render-to-null)).

## Reports, HAR and logs

- **The report shows the last attempt**, the one that passed or the last that failed, and says
  which it was when there were several: `HTTP Response (attempt 3 of 10)`, or in a parallel
  stage `HTTP Response (failing of 4 parallel iterations, attempt 10 of 10)`. A last attempt
  that sent nothing, its request's template having failed, shows no exchange.
- **The [HAR export](../getting-started.md#har-export) records every attempt**, each iteration's
  in the order they were made: each is real traffic, the attempts of a stage a user function's
  `pytest.skip()`, `xfail()` or `fail()` ended included. In a parallel stage it has every
  iteration's, those of an iteration cancelled while it waited to retry, or that failed after
  another failed the stage, included.
- **Each failed attempt that is retried logs one line** at the `INFO` level, the attempt's failure
  and the wait before the next: `Stage 'wait_for_job': attempt 2 of 10 failed, next in 1s: ...`
  (`--log-level=INFO` to see them).

## Factory fixtures

A [factory fixture](../usage/scenarios.md#fixtures) called in the request or a response step is
called again by each attempt, and what it returns as a context manager is entered each time. They
are all exited once the iteration's last attempt is done, last entered first, not attempt by
attempt: an exit is not told whether its attempt failed, and what an iteration enters, it exits
when it ends. To enter one for all the attempts, call the fixture in the stage's `substitutions`.

## Non-idempotent requests

`retry` sends the request again. That is harmless for a `GET`, and for a `PUT` or `DELETE` that
sets the same state each time, but a `POST` retried may create twice: a request that timed out,
which `on: request` retries, may still have reached the server and done its work.

- **Poll in a stage of its own**, as in the example above: start the job with one `POST`, and
  retry the `GET` that checks on it.
- **Retry a `POST` only on what it cannot have done**: with `"on": "verify"`, it is sent again
  only after a response the server answered, which says what it did (a `409` or a `503`, say).
- **Send an idempotency key** where the API deduplicates on one, and bind it in the stage's
  `substitutions`, which render once for every attempt: `{"vars": {"key": "{{ uuid4() }}"}}`
  with `"headers": {"Idempotency-Key": "{{ key }}"}`. A `uuid4()` written in the request itself
  would give each attempt a key of its own, and the server nothing to deduplicate.
