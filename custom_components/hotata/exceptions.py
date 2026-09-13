"""Exceptions raised by the Hotata client."""


class HotataError(Exception):
    """Base client error."""


class HotataAuthError(HotataError):
    """Authentication failed or expired."""


class HotataConnectionError(HotataError):
    """The cloud service could not be reached."""


class HotataRateLimited(HotataError):
    """The cloud throttled us with 403 (操作过于频繁).

    Must never be retried automatically: any request during the penalty may
    extend it. The account enters its silence window and (if configured)
    fails over to the backup account instead.
    """
