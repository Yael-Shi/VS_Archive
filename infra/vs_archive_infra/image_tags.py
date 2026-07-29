"""Resolve backward-compatible web and worker image tags."""

from __future__ import annotations

from constructs import Construct


def resolve_image_tags(scope: Construct) -> tuple[str, str]:
    """
    Resolve independent deploy tags while preserving the legacy shared context.

    ``web_image_tag`` and ``worker_image_tag`` override their respective
    service. Missing service-specific values fall back to ``image_tag`` and
    finally to the historical ``dev`` default.
    """
    shared_image_tag = str(scope.node.try_get_context("image_tag") or "dev")
    web_image_tag = str(scope.node.try_get_context("web_image_tag") or shared_image_tag)
    worker_image_tag = str(
        scope.node.try_get_context("worker_image_tag") or shared_image_tag
    )
    return web_image_tag, worker_image_tag
