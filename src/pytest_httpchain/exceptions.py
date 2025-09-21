class StageExecutionError(Exception):
    pass


class RequestError(StageExecutionError):
    pass


class SaveError(StageExecutionError):
    pass


class VerificationError(StageExecutionError):
    pass
