class JobAlreadyRunningError(RuntimeError):
    """Raised when a live lease already owns the same scheduled job."""


class JobLeaseLostError(RuntimeError):
    """Raised when a worker attempts to write after its lease expired or was recovered."""
