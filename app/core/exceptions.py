"""Domain exceptions.

Services raise these instead of fastapi.HTTPException, so they stay independent of
the web layer. A single handler in app.main turns them into JSON responses of the
form {"detail": message}, which is the shape the frontend already reads.
"""


class DomainError(Exception):
    """Base class. `message` is always safe to show to the user."""

    status_code = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class InvalidInput(DomainError):
    status_code = 400


class NotFound(DomainError):
    status_code = 404


class Conflict(DomainError):
    status_code = 409


class LimitReached(DomainError):
    """A plan limit was hit. `message` is a machine-readable code like QUERY_LIMIT_REACHED."""

    status_code = 403


class UpstreamFailure(DomainError):
    """An external service (e.g. the AI model) failed; the request may succeed on retry."""

    status_code = 502
