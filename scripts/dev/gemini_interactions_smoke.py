#!/usr/bin/env python3
"""
Local spike: verify GEMINI_API_KEY against the Gemini Interactions API.

Uses the May 2026 Interactions schema (steps, Api-Revision: 2026-05-20).
See: https://ai.google.dev/gemini-api/docs/interactions-breaking-changes-may-2026

Does NOT touch production OCR routing or gemini_engine.py.

Run from repo root (uses app/backend Poetry env for requests/python-dotenv):

  cd app/backend && poetry run python ../../scripts/dev/gemini_interactions_smoke.py \\
    --env-file .env --check model

See scripts/dev/README-gemini-interactions-spike.md for curl equivalents and image OCR notes.
"""

from __future__ import annotations

import argparse
import base64
import mimetypes
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

ANTIGRAVITY_AGENT = "antigravity-preview-05-2026"
DEFAULT_MODEL = "gemini-2.5-flash-lite"
INTERACTIONS_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
INTERACTIONS_API_REVISION = "2026-05-20"


class SmokeError(RuntimeError):
    pass


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_env_file(env_file: Path | None) -> None:
    if env_file is None:
        return
    if not env_file.is_file():
        raise SmokeError(f"Env file not found: {env_file}")
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise SmokeError(
            "python-dotenv is required for --env-file (install via app/backend Poetry env)"
        ) from exc
    load_dotenv(env_file, override=False)


def _api_key() -> str:
    key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not key:
        raise SmokeError(
            "Missing GEMINI_API_KEY. Export it or pass --env-file pointing at app/backend/.env"
        )
    return key


def _request_headers(api_key: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
        "Api-Revision": INTERACTIONS_API_REVISION,
    }


def _raise_for_api_error(response: requests.Response) -> None:
    if response.ok:
        return
    message = response.text
    try:
        payload = response.json()
        error = payload.get("error") or {}
        message = error.get("message") or message
    except ValueError:
        pass
    raise SmokeError(f"HTTP {response.status_code}: {message}")


def _output_text_from_steps(steps: list[dict[str, Any]] | None) -> str | None:
    chunks: list[str] = []
    for step in steps or []:
        if step.get("type") != "model_output":
            continue
        for content in step.get("content") or []:
            if content.get("type") == "text":
                text = content.get("text")
                if text:
                    chunks.append(text)
    return "\n".join(chunks) if chunks else None


def _summarize_interaction(interaction: dict[str, Any]) -> dict[str, Any]:
    steps = interaction.get("steps") or []
    output_text = _output_text_from_steps(steps)
    return {
        "id": interaction.get("id"),
        "status": interaction.get("status"),
        "agent": interaction.get("agent"),
        "model": interaction.get("model"),
        "environment_id": interaction.get("environment_id"),
        "output_text_preview": (output_text or "")[:500] or None,
        "step_count": len(steps),
    }


def _create_interaction(
    api_key: str,
    *,
    payload: dict[str, Any],
) -> dict[str, Any]:
    response = requests.post(
        INTERACTIONS_BASE_URL,
        headers=_request_headers(api_key),
        json=payload,
        timeout=120,
    )
    _raise_for_api_error(response)
    body = response.json()
    if not isinstance(body, dict):
        raise SmokeError("Unexpected non-object Interactions API response")
    return body


def _get_interaction(api_key: str, interaction_id: str) -> dict[str, Any]:
    response = requests.get(
        f"{INTERACTIONS_BASE_URL}/{interaction_id}",
        headers=_request_headers(api_key),
        timeout=60,
    )
    _raise_for_api_error(response)
    body = response.json()
    if not isinstance(body, dict):
        raise SmokeError("Unexpected non-object Interactions API response")
    return body


def _poll_until_done(
    api_key: str,
    interaction: dict[str, Any],
    *,
    poll_seconds: float,
    timeout_seconds: float,
) -> dict[str, Any]:
    interaction_id = interaction.get("id")
    if not interaction_id:
        raise SmokeError("Interaction response missing id")

    deadline = time.monotonic() + timeout_seconds
    current = interaction
    while current.get("status") == "in_progress":
        if time.monotonic() >= deadline:
            raise SmokeError(
                f"Timed out after {timeout_seconds}s waiting for interaction {interaction_id}"
            )
        time.sleep(poll_seconds)
        current = _get_interaction(api_key, interaction_id)
    return current


def _run_model_smoke(api_key: str, *, model: str, prompt: str) -> dict[str, Any]:
    print(f"[model] Creating interaction with model={model!r}")
    return _create_interaction(
        api_key,
        payload={
            "model": model,
            "input": prompt,
        },
    )


def _run_antigravity_smoke(
    api_key: str,
    *,
    prompt: str,
    background: bool,
    poll_seconds: float,
    timeout_seconds: float,
) -> dict[str, Any]:
    print(f"[antigravity] Creating interaction with agent={ANTIGRAVITY_AGENT!r}")
    interaction = _create_interaction(
        api_key,
        payload={
            "agent": ANTIGRAVITY_AGENT,
            "input": prompt,
            "environment": "remote",
            "background": background,
        },
    )
    if background or interaction.get("status") == "in_progress":
        print(f"[antigravity] Polling interaction id={interaction.get('id')!r}")
        interaction = _poll_until_done(
            api_key,
            interaction,
            poll_seconds=poll_seconds,
            timeout_seconds=timeout_seconds,
        )
    return interaction


def _image_mime_type(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "application/octet-stream"


def _run_antigravity_image_smoke(
    api_key: str,
    *,
    image_path: Path,
    prompt: str,
    background: bool,
    poll_seconds: float,
    timeout_seconds: float,
) -> dict[str, Any]:
    if not image_path.is_file():
        raise SmokeError(f"Image not found: {image_path}")
    image_bytes = image_path.read_bytes()
    mime_type = _image_mime_type(image_path)
    print(
        f"[antigravity-image] Creating multimodal interaction "
        f"(file={image_path.name!r}, bytes={len(image_bytes)}, mime={mime_type!r})"
    )
    interaction = _create_interaction(
        api_key,
        payload={
            "agent": ANTIGRAVITY_AGENT,
            "input": [
                {"type": "text", "text": prompt},
                {
                    "type": "image",
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                    "mime_type": mime_type,
                },
            ],
            "environment": "remote",
            "background": background,
        },
    )
    if background or interaction.get("status") == "in_progress":
        print(f"[antigravity-image] Polling interaction id={interaction.get('id')!r}")
        interaction = _poll_until_done(
            api_key,
            interaction,
            poll_seconds=poll_seconds,
            timeout_seconds=timeout_seconds,
        )
    return interaction


def _print_result(label: str, interaction: dict[str, Any]) -> None:
    summary = _summarize_interaction(interaction)
    print(f"\n== {label} ==")
    for key, value in summary.items():
        print(f"{key}: {value}")
    status = summary.get("status")
    if status not in {"completed", None}:
        raise SmokeError(f"{label} finished with status={status!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Smoke-test GEMINI_API_KEY against Gemini Interactions / Antigravity."
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=_repo_root() / "app" / "backend" / ".env",
        help="Optional dotenv file (default: app/backend/.env if present).",
    )
    parser.add_argument(
        "--check",
        choices=("model", "antigravity", "antigravity-image", "all"),
        default="model",
        help="model=fast Interactions API check; antigravity*=managed agent.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Model for --check model (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--image",
        type=Path,
        help="Local image path for --check antigravity-image.",
    )
    parser.add_argument(
        "--background",
        action="store_true",
        help="Start Antigravity interactions in background and poll (recommended).",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=5.0,
        help="Poll interval for background Antigravity interactions.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=300.0,
        help="Max wait time when polling Antigravity interactions.",
    )
    args = parser.parse_args(argv)

    env_file = args.env_file if args.env_file and args.env_file.is_file() else None
    if env_file:
        _load_env_file(env_file)
        print(f"Loaded env file: {env_file}")

    api_key = _api_key()

    model_prompt = "Reply with exactly: interactions-api-ok"
    antigravity_prompt = (
        "Reply with exactly: antigravity-ok. Do not browse the web or run code."
    )
    image_prompt = (
        "Transcribe all visible text in the image faithfully. "
        "Reply with plain text only."
    )

    checks: list[str]
    if args.check == "all":
        checks = ["model", "antigravity"]
        if args.image:
            checks.append("antigravity-image")
    else:
        checks = [args.check]

    if "antigravity-image" in checks and not args.image:
        raise SmokeError("--image is required for --check antigravity-image")

    try:
        if "model" in checks:
            interaction = _run_model_smoke(
                api_key, model=args.model, prompt=model_prompt
            )
            _print_result("model", interaction)

        if "antigravity" in checks:
            interaction = _run_antigravity_smoke(
                api_key,
                prompt=antigravity_prompt,
                background=args.background,
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.timeout_seconds,
            )
            _print_result("antigravity", interaction)

        if "antigravity-image" in checks:
            assert args.image is not None
            interaction = _run_antigravity_image_smoke(
                api_key,
                image_path=args.image,
                prompt=image_prompt,
                background=args.background,
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.timeout_seconds,
            )
            _print_result("antigravity-image", interaction)
    except Exception as exc:
        print(f"\nSmoke test failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print("\nSmoke test completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
