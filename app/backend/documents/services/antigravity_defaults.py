from __future__ import annotations

DEFAULT_ANTIGRAVITY_AGENT_ID = "antigravity-preview-05-2026"
INTERACTIONS_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
INTERACTIONS_API_REVISION = "2026-05-20"

# Unary create timeout remains computed in antigravity_engine from page count.
DEFAULT_POLL_SECONDS = 5.0
# Overall in_progress poll deadline. Finite and below the worker 45-minute lease.
DEFAULT_TIMEOUT_SECONDS = 1200.0
# Per-request timeout for unary GET /interactions/{id} polls.
DEFAULT_POLL_GET_TIMEOUT_SECONDS = 120.0
