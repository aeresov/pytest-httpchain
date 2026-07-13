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
    pass


class SaveError(StageExecutionError):
    pass


class VerificationError(StageExecutionError):
    pass
