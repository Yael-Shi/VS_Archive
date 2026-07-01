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
IMAGE_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
}


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
    timeout_seconds: float = 120,
) -> dict[str, Any]:
    response = requests.post(
        INTERACTIONS_BASE_URL,
        headers=_request_headers(api_key),
        json=payload,
        timeout=timeout_seconds,
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


def _images_from_dir(image_dir: Path) -> list[Path]:
    if not image_dir.is_dir():
        raise SmokeError(f"Image directory not found: {image_dir}")
    paths = sorted(
        p
        for p in image_dir.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    )
    if not paths:
        raise SmokeError(f"No images found in {image_dir}")
    return paths


def _resolve_image_paths(
    *,
    cli_images: list[Path],
    image_dir: Path | None,
) -> list[Path]:
    paths: list[Path] = []
    if image_dir is not None:
        paths.extend(_images_from_dir(image_dir))
    paths.extend(cli_images)
    if not paths:
        raise SmokeError("No images specified")
    for path in paths:
        if not path.is_file():
            raise SmokeError(f"Image not found: {path}")
    return paths


def _antigravity_ocr_prompt(image_paths: list[Path]) -> str:
    filenames = [path.name for path in image_paths]
    image_order = "\n".join(
        f"- IMAGE {index}: {name}" for index, name in enumerate(filenames, start=1)
    )
    heading_examples = "\n\n".join(
        f"[IMAGE {index}: {name}]\n<transcription for image {index}>"
        for index, name in enumerate(filenames, start=1)
    )
    return (
        "You are transcribing historical archive document page images.\n"
        "TASK: OCR/transcription only. Do not translate.\n"
        "RULES:\n"
        "- Preserve Arabic, Hebrew, and Latin scripts exactly as written.\n"
        "- Preserve names, dates, page numbers, document numbers, and punctuation.\n"
        "- Include cover/catalog page text when present.\n"
        "- Include occasional handwritten additions when visible.\n"
        "- Prefer [UNCLEAR] over inventing confident text.\n"
        "- Do not browse the web, run code, or use tools unless strictly needed for OCR.\n"
        "- Reply with plain text only.\n"
        "\n"
        f"Images are attached in this order ({len(filenames)} total):\n"
        f"{image_order}\n"
        "\n"
        "Return one section per image, in order, using headings exactly like:\n"
        f"{heading_examples}"
    )


def _build_multimodal_input(
    prompt: str, image_paths: list[Path]
) -> list[dict[str, Any]]:
    input_blocks: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for path in image_paths:
        input_blocks.append(
            {
                "type": "image",
                "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                "mime_type": _image_mime_type(path),
            }
        )
    return input_blocks


def _run_antigravity_ocr_smoke(
    api_key: str,
    *,
    label: str,
    image_paths: list[Path],
    background: bool,
    poll_seconds: float,
    timeout_seconds: float,
) -> dict[str, Any]:
    total_bytes = sum(path.stat().st_size for path in image_paths)
    names = ", ".join(path.name for path in image_paths)
    print(
        f"[{label}] Creating multimodal interaction "
        f"(images={len(image_paths)}, bytes={total_bytes}, files=[{names}])"
    )
    interaction = _create_interaction(
        api_key,
        payload={
            "agent": ANTIGRAVITY_AGENT,
            "input": _build_multimodal_input(
                _antigravity_ocr_prompt(image_paths),
                image_paths,
            ),
            "environment": "remote",
            "background": background,
        },
        timeout_seconds=max(120.0, 30.0 * len(image_paths)),
    )
    if background or interaction.get("status") == "in_progress":
        print(f"[{label}] Polling interaction id={interaction.get('id')!r}")
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


def _print_ocr_summary(
    label: str,
    interaction: dict[str, Any],
    image_paths: list[Path],
) -> None:
    summary = _summarize_interaction(interaction)
    print(f"\n== {label} summary ==")
    print(f"interaction_id: {summary['id']}")
    print(f"status: {summary['status']}")
    print(f"step_count: {summary['step_count']}")
    print(
        f"images: {len(image_paths)} ({', '.join(path.name for path in image_paths)})"
    )
    preview = summary.get("output_text_preview")
    print(f"output_preview:\n{preview or '(empty)'}")
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
        choices=(
            "model",
            "antigravity",
            "antigravity-image",
            "antigravity-images",
            "all",
        ),
        default="model",
        help="model=fast check; antigravity*=managed agent; *-image(s)=OCR spike.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Model for --check model (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--image",
        action="append",
        default=[],
        metavar="PATH",
        help="Image path; repeat for multiple images (antigravity-images).",
    )
    parser.add_argument(
        "--image-dir",
        type=Path,
        help="Directory of images, read in filename sort order (antigravity-images).",
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

    checks: list[str]
    if args.check == "all":
        checks = ["model", "antigravity"]
        if args.image_dir or len(args.image) > 1:
            checks.append("antigravity-images")
        elif args.image:
            checks.append("antigravity-image")
    else:
        checks = [args.check]

    if args.check == "antigravity-image":
        if args.image_dir is not None:
            raise SmokeError(
                "--check antigravity-image does not support --image-dir; "
                "use --check antigravity-images"
            )
        if len(args.image) != 1:
            raise SmokeError("--check antigravity-image requires exactly one --image")

    if "antigravity-images" in checks and not args.image and not args.image_dir:
        raise SmokeError(
            "--check antigravity-images requires --image and/or --image-dir"
        )

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
            image_paths = _resolve_image_paths(
                cli_images=args.image,
                image_dir=None,
            )
            interaction = _run_antigravity_ocr_smoke(
                api_key,
                label="antigravity-image",
                image_paths=image_paths,
                background=args.background,
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.timeout_seconds,
            )
            _print_ocr_summary("antigravity-image", interaction, image_paths)

        if "antigravity-images" in checks:
            image_paths = _resolve_image_paths(
                cli_images=args.image,
                image_dir=args.image_dir,
            )
            interaction = _run_antigravity_ocr_smoke(
                api_key,
                label="antigravity-images",
                image_paths=image_paths,
                background=args.background,
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.timeout_seconds,
            )
            _print_ocr_summary("antigravity-images", interaction, image_paths)
    except Exception as exc:
        print(f"\nSmoke test failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print("\nSmoke test completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
