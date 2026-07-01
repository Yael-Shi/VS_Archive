from __future__ import annotations

from documents.services.htr_adapters.base import (
    HtrEngineAdapter,
    UnsupportedEngineError,
)
from documents.services.htr_adapters.antigravity_adapter import AntigravityAdapter
from documents.services.htr_adapters.gemini_adapter import GeminiAdapter
from documents.services.htr_adapters.transkribus_adapter import TranskribusAdapter


_ADAPTERS: dict[str, HtrEngineAdapter] = {
    GeminiAdapter.engine_key: GeminiAdapter(),
    TranskribusAdapter.engine_key: TranskribusAdapter(),
    AntigravityAdapter.engine_key: AntigravityAdapter(),
}


def get_htr_adapter(engine_key: str) -> HtrEngineAdapter:
    adapter = _ADAPTERS.get((engine_key or "").strip().upper())
    if adapter is None:
        raise UnsupportedEngineError(engine_key)
    return adapter
