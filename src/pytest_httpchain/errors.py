from datetime import datetime

import httpx


class HttpChainError(Exception):
    """Base exception for all pytest-httpchain errors."""


class SchemaFileError(HttpChainError):
    """A referenced JSON Schema file could not be read or parsed. Raised by
    ``utils.read_json_schema_file`` so the validator and the runtime can word
    the failure their own way without re-deriving what counts as one."""


class StageExecutionError(HttpChainError):
    """A stage failed. Carries the HTTP request/response when one was made, for
    the failure report and the HAR file; ``started`` is when the request went on
    the wire, feeding the HAR entry's startedDateTime."""

    def __init__(
        self,
        message: str,
        request: httpx.Request | None = None,
        response: httpx.Response | None = None,
        started: datetime | None = None,
    ):
        super().__init__(message)
        self.request = request
        self.response = response
        self.started = started


class RequestError(StageExecutionError):
    """Building or sending the HTTP request failed: unreadable body files,
    auth-callable errors, transport failures (timeout, connection refused,
    DNS), or a rate-limit slot that never became available."""


class SaveError(StageExecutionError):
    """A response ``save`` step failed: unusable body, failed extraction, or a
    save function that raised or returned a non-dict."""


class VerificationError(StageExecutionError):
    """A response ``verify`` step failed: an expectation was not met, an
    expression was falsy, or a verify function raised or returned falsy."""
