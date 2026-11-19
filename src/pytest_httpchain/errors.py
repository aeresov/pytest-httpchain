from datetime import datetime

import httpx
import pytest

# Raised by user code a call runs, these stop the run: never taken for a
# stage's failure (at collection, plugin.JsonModule.collect still reports one as
# the file's error). pytest.exit()'s is an Exception, so an `except Exception` must let
# these through first. Defined here, at the bottom of the layers, as the
# domain packages that call user code (userfunc, templates) must too.
INTERRUPTS = (KeyboardInterrupt, SystemExit, pytest.exit.Exception)


class HttpChainError(Exception):
    """Base exception for all pytest-httpchain errors."""


class SchemaFileError(HttpChainError):
    """A referenced JSON Schema file could not be read or parsed. Raised by
    ``body_schema.read_json_schema_file`` so the validator and the runtime
    can word the failure their own way without re-deriving what counts as
    one."""


class SchemaPointerError(HttpChainError):
    """A ``body.schema`` file reference's JSON pointer (``openapi.json#/...``)
    leads nowhere in the file it names. Raised by ``body_schema``, and worded
    by the runtime and ``validate --deep`` each their own way, as
    `SchemaFileError` is."""


# One wire exchange: the request, its response (None when none arrived), and
# when it went on the wire (None falls back to HAR write time). What a failure
# carries (`StageExecutionError.earlier_exchanges`) and the HAR export writes
# (`har_writer`, which re-exports the name).
type Exchange = tuple[httpx.Request, httpx.Response | None, datetime | None]


class StageExecutionError(HttpChainError):
    """A stage failed. Carries the HTTP request/response when one was made, for
    the failure report and the HAR file; ``started`` is when the request went on
    the wire, feeding the HAR entry's startedDateTime.

    ``retryable`` is whether a stage's ``retry`` may make another attempt after
    this failure, when its ``on`` names the failure's kind: whether the next
    response, or the next try on the network, may end otherwise. It is not
    for a failure of the scenario's own templates, which every attempt would
    repeat. Each kind has its default, which a raise site overrides.

    ``attempt`` is which attempt of the stage's ``retry`` ended in this failure
    (1 without one), and ``earlier_exchanges`` are the exchanges of the
    attempts before it, oldest first, when the HAR file records them: the
    carrier sets both.
    """

    retryable: bool = False

    def __init__(
        self,
        message: str,
        request: httpx.Request | None = None,
        response: httpx.Response | None = None,
        started: datetime | None = None,
        *,
        retryable: bool | None = None,
    ):
        super().__init__(message)
        self.request = request
        self.response = response
        self.started = started
        if retryable is not None:
            self.retryable = retryable
        self.attempt = 1
        self.earlier_exchanges: tuple[Exchange, ...] = ()


class RequestError(StageExecutionError):
    """Building or sending the HTTP request failed: unreadable body files,
    auth-callable errors, transport failures (timeout, connection refused,
    DNS), or a rate-limit slot that never became available.

    Only a transport failure is retryable (the carrier marks it): the others
    fail every attempt alike."""


class SaveError(StageExecutionError):
    """A response ``save`` step failed: unusable body, failed extraction, or a
    save function that raised or returned a non-dict. Retryable unless the
    step's templates failed to render or rendered what the step cannot use,
    or the function could not be called or crashed: a save function raises
    this, or a `VerificationError`, to have its attempt retried, and with
    ``retryable=False`` to end the stage at once."""

    retryable = True


class VerificationError(StageExecutionError):
    """A response ``verify`` step failed: an expectation was not met, an
    expression was falsy, or a verify function raised or returned falsy.
    Retryable unless one of the step's failures is the scenario's own (a value
    that failed to render or rendered what its check cannot take, a body
    schema that cannot be read, a function that could not be called or
    crashed) or a function ended the step with ``pytest.fail()``. A verify
    function that raises this, rather than returning false, is retried too,
    and one that raises it with ``retryable=False`` ends the stage at once
    (a job that failed, which no later poll will find done)."""

    retryable = True
