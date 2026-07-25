import httpx


class HttpChainError(Exception):
    """Base exception for all pytest-httpchain errors."""


class StageExecutionError(HttpChainError):
    """A stage failed. Carries the HTTP request/response when one was made, for
    the failure report and the HAR file."""

    def __init__(
        self,
        message: str,
        request: httpx.Request | None = None,
        response: httpx.Response | None = None,
    ):
        super().__init__(message)
        self.request = request
        self.response = response


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
