class CollectorError(Exception):
    """A user-readable collection error."""


class AuthenticationRequired(CollectorError):
    pass


class UnsupportedQuery(CollectorError):
    pass


class QueryError(CollectorError):
    def __init__(self, message, *, retryable=False, retry_after=None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after
