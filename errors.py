"""Exceptions raised on purpose by the platform."""


class WfdpError(Exception):
    """Base class for every deliberate platform error."""


class ConditionError(WfdpError):
    """A condition in a workflow config is malformed."""


class ConfigError(WfdpError):
    """A workflow config failed validation. `.errors` lists every problem."""

    def __init__(self, errors):
        if isinstance(errors, str):
            errors = [errors]
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


class WorkflowNotFound(WfdpError):
    pass


class RequestNotFound(WfdpError):
    pass


class IdempotencyConflict(WfdpError):
    """Idempotency key re-used with a different payload or workflow."""


class InvalidState(WfdpError):
    """Operation not allowed in the request's current state."""


class BadRequest(WfdpError):
    pass


class ConcurrencyError(WfdpError):
    """Optimistic lock failed: another writer changed the request first."""


class IllegalTransition(WfdpError):
    """The store refused a status transition that the lifecycle forbids."""


class DependencyError(WfdpError):
    """Permanent failure of an external dependency."""


class TransientDependencyError(DependencyError):
    """Temporary failure of an external dependency; worth retrying."""
