"""Exception classes for HTTP chain test execution.

This module defines the exception hierarchy used throughout the
pytest-httpchain test execution flow.
"""


class StageExecutionError(Exception):
    """Base exception for all stage execution errors.

    This is the base class for all exceptions that can occur during
    stage execution. Catching this will catch all stage-related errors.
    """


class RequestError(StageExecutionError):
    """Error during HTTP request preparation or execution.

    Raised when:
    - Request preparation fails (auth, file opening, etc.)
    - HTTP request times out
    - Connection errors occur
    - Other request-related issues
    """


class SaveError(StageExecutionError):
    """Error during response processing (save operations).

    Raised when:
    - JMESPath expression fails
    - User save function fails
    - Variable extraction fails
    """


class VerificationError(StageExecutionError):
    """Error during response verification.

    Raised when:
    - Status code doesn't match expected
    - Headers don't match expected
    - Response body validation fails
    - User verify function returns False
    - JSON schema validation fails
    """
