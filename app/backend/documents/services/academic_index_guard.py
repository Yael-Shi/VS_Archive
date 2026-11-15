"""Process-local flag for academic-index state writes.

Direct SQL is not affected. This module imports nothing from ``documents.models``.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

_state_write_allowed: ContextVar[bool] = ContextVar(
    "academic_index_state_write_allowed",
    default=False,
)


def academic_index_state_write_allowed() -> bool:
    return bool(_state_write_allowed.get())


@contextmanager
def academic_index_state_write():
    token = _state_write_allowed.set(True)
    try:
        yield
    finally:
        _state_write_allowed.reset(token)
