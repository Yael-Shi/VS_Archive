from documents.services.htr_adapters.base import (
    EnginePermanentError,
    EngineRetryableError,
    HtrResult,
    UnsupportedEngineError,
)

__all__ = [
    "EnginePermanentError",
    "EngineRetryableError",
    "HtrResult",
    "UnsupportedEngineError",
    "get_htr_adapter",
]


def __getattr__(name: str):
    if name == "get_htr_adapter":
        from documents.services.htr_adapters.registry import get_htr_adapter

        return get_htr_adapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
