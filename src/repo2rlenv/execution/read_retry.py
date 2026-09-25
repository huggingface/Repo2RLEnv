"""Small retry bound for observational operations; never use for model or job dispatch."""

from __future__ import annotations

import time
from collections.abc import Callable


def transient_connection_error(error: Exception) -> bool:
    return (
        isinstance(error, (TimeoutError, ConnectionError))
        or getattr(error, "status_code", None) in {408, 429, 500, 502, 503, 504}
        or type(error).__name__
        in {
            "DaytonaConnectionError",
            "DaytonaConnectionTimeoutError",
            "DaytonaTimeoutError",
            "DaytonaRateLimitError",
            "ReadTimeout",
            "ConnectTimeout",
            "ConnectError",
            "ConnectionError",
        }
    )


def retry_read[Result](operation: Callable[[], Result], *, attempts: int = 3) -> Result:
    if not 1 <= attempts <= 3:
        raise ValueError("Read retries allow one to three attempts")
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as error:
            if attempt + 1 == attempts or not transient_connection_error(error):
                raise
            time.sleep(0.5 * 2**attempt)
    raise AssertionError("Unreachable: the last attempt must return or raise")
