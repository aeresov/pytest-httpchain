import httpx


class HttpChainError(Exception):
    """Base exception for all pytest-httpchain errors."""


class StageExecutionError(HttpChainError):
    """Base exception for stage execution errors.

    Optionally carries HTTP request/response for debugging failed stages.
    """

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
    """A response ``save`` step failed: the body was not the expected JSON,
    a JMESPath expression errored, a substitutions save failed to resolve, a
    save user function raised or returned a non-dict, or the reserved-name
    (HTTPCHAIN027) runtime warning was promoted under ``filterwarnings =
    error``."""


class VerificationError(StageExecutionError):
    """A response ``verify`` step failed: status/header/body expectation not
    met, schema validation failed (or the schema itself was unusable), an
    expression was falsy, or a verify user function raised/returned falsy."""
