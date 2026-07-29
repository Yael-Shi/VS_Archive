"""Resolve controlled worker scaling for deployment operations."""

from __future__ import annotations

from constructs import Construct


def resolve_worker_desired_count(scope: Construct) -> int:
    """
    Resolve the worker desired count from CDK context.

    The normal default remains one worker. Deployment operations may set the
    count to zero while bootstrapping database migrations for a new image.
    Other values are rejected to prevent accidental scaling or invalid input.
    """
    raw_value = scope.node.try_get_context("worker_desired_count")
    if isinstance(raw_value, bool):
        raise TypeError(f"worker_desired_count must be 0 or 1, got {raw_value!r}")
    if raw_value is None or raw_value in (1, "1"):
        return 1
    if raw_value in (0, "0"):
        return 0
    raise ValueError(f"worker_desired_count must be 0 or 1, got {raw_value!r}")
