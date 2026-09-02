"""Small, transport-neutral cancellation contract for cover workflows."""
from __future__ import annotations

import threading
from typing import Any


class CancellationToken:
    """Adapter around an Event/callable with one stable service interface."""

    def __init__(self, source: Any = None) -> None:
        self._source = source or threading.Event()

    def is_cancelled(self) -> bool:
        source = self._source
        if callable(source):
            return bool(source())
        checker = getattr(source, "is_set", None)
        return bool(checker()) if callable(checker) else bool(source)

    def cancelled(self) -> bool:
        return self.is_cancelled()

    is_set = is_cancelled

    def wait(self, timeout: float | None = None) -> bool:
        source = self._source
        waiter = getattr(source, "wait", None)
        if callable(waiter):
            return bool(waiter(timeout))
        if timeout:
            import time
            time.sleep(timeout)
        return self.is_cancelled()


def as_cancellation_token(value: Any = None) -> CancellationToken:
    return value if isinstance(value, CancellationToken) else CancellationToken(value)
