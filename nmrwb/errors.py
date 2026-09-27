class FormatError(Exception):
    """Raised for explicit input format violations (HTTP 422)."""


class DomainError(Exception):
    """Raised when a domain operation is not allowed in the current state."""


class NotFoundError(Exception):
    """Raised when a referenced object does not exist (HTTP 404)."""


class ConflictError(Exception):
    """Raised when an import collides with existing data (HTTP 409)."""
