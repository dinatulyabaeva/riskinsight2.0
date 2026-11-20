"""Public, redacted errors shared by the agent and its provider adapters."""


class AgentError(RuntimeError):
    def __init__(self, message, *, status_code=None, retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
