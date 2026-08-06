"""Application exception hierarchy."""


class ApplicationError(Exception):
    """Base class for expected application failures."""


class ValidationApplicationError(ApplicationError):
    pass


class NotFoundError(ApplicationError):
    pass


class ConflictError(ApplicationError):
    pass


class ForbiddenError(ApplicationError):
    pass


class ExternalServiceError(ApplicationError):
    pass
