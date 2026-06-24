from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from django.core.management.base import BaseCommand, CommandError

from documents.models import Document, DocumentTextResult
from documents.services.env_validation import EnvConfigError, validate_required_env
from documents.services.htr_engine import transcribe_pages
from documents.services.ocr_routing import OcrRouteConfig
from documents.services.page_extraction import extract_pages

_CONFIRM_HELP = (
    "Required. Acknowledges that this command creates a real Transkribus document "
    "and does not clean it up."
)

_NO_CONFIRM_MESSAGE = (
    "Refusing to run: this command creates a real Transkribus document and does not "
    "clean it up.\n"
    "Pass --confirm-create-transkribus-doc to proceed."
)

_SUPPORTED_SUFFIX_TO_MIME = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _mime_type_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    mime = _SUPPORTED_SUFFIX_TO_MIME.get(suffix)
    if mime is None:
        supported = ", ".join(
            sorted({s.lstrip(".") for s in _SUPPORTED_SUFFIX_TO_MIME})
        )
        raise CommandError(
            f"Unsupported file extension {suffix!r}. "
            f"Supported types for this dev command: {supported}."
        )
    return mime


def _normalize_language(raw: str) -> str:
    lang = (raw or "").strip().lower()
    valid = {v for v, _ in Document.Language.choices}
    if lang not in valid:
        raise CommandError(
            f"Invalid --language {raw!r}. Expected one of: {', '.join(sorted(valid))}."
        )
    return lang


def _normalize_text_input_type(raw: str) -> str:
    text_type = (raw or "").strip().upper()
    valid = {v for v, _ in Document.TextInputType.choices}
    if text_type not in valid:
        raise CommandError(
            f"Invalid --text-input-type {raw!r}. "
            f"Expected one of: {', '.join(sorted(valid))}."
        )
    return text_type


def _resolve_prompt_variant(raw: Optional[str]) -> str:
    if raw is None or raw.strip() == "":
        return DocumentTextResult.OcrPromptVariant.HANDWRITTEN
    s = raw.strip()
    s_upper = s.upper()
    if s_upper == "HANDWRITTEN":
        return DocumentTextResult.OcrPromptVariant.HANDWRITTEN
    if s_upper == "PRINTED":
        return DocumentTextResult.OcrPromptVariant.PRINTED
    s_lower = s.lower()
    valid_display = {c for c, _ in DocumentTextResult.OcrPromptVariant.choices}
    if s_lower in valid_display:
        return s_lower
    raise CommandError(
        f"Invalid --prompt-variant {raw!r}. "
        f"Use handwritten, printed, HANDWRITTEN, or PRINTED."
    )


def _require_upload_dev_env(cfg: object) -> None:
    if not getattr(cfg, "transkribus_dev_upload_mode", False):
        raise CommandError(
            "TRANSKRIBUS_DEV_UPLOAD_MODE must be true for this command "
            "(dev upload path; creates a new TrpServer document)."
        )
    missing: list[str] = []
    if not (
        getattr(cfg, "transkribus_username", None)
        and getattr(cfg, "transkribus_password", None)
    ):
        missing.append("TRANSKRIBUS_USERNAME and TRANSKRIBUS_PASSWORD")
    if not getattr(cfg, "transkribus_api_token", None):
        missing.append("TRANSKRIBUS_API_TOKEN")
    if not getattr(cfg, "transkribus_collection_id", None):
        missing.append("TRANSKRIBUS_COLLECTION_ID")
    if not getattr(cfg, "transkribus_model_id", None):
        missing.append("TRANSKRIBUS_MODEL_ID")
    if missing:
        raise CommandError(
            "Transkribus dev upload configuration incomplete: " + ", ".join(missing)
        )


class Command(BaseCommand):
    help = (
        "DEV/STAGING ONLY: transcribe a local image or PDF via htr_engine.transcribe_pages "
        "using an explicit TRANSKRIBUS route (does not use OCR_ROUTES or run_worker). "
        "Creates a real Transkribus document when run for real; no cleanup."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "file_path",
            type=str,
            help="Path to a local image (png, jpeg, webp) or PDF.",
        )
        parser.add_argument(
            "--language",
            default="he",
            help="Language hint passed to transcribe_pages (default: he).",
        )
        parser.add_argument(
            "--text-input-type",
            default="HANDWRITTEN",
            help="text_input_type passed to transcribe_pages (default: HANDWRITTEN).",
        )
        parser.add_argument(
            "--prompt-variant",
            default=None,
            help=(
                "Routing prompt_variant on the explicit OcrRouteConfig "
                "(default: handwritten / HANDWRITTEN)."
            ),
        )
        parser.add_argument(
            "--text-preview-limit",
            type=int,
            default=800,
            help="Max characters of transcribed text to print (default: 800).",
        )
        parser.add_argument(
            "--confirm-create-transkribus-doc",
            action="store_true",
            help=_CONFIRM_HELP,
        )

    def handle(self, *args, **options):
        if not options.get("confirm_create_transkribus_doc"):
            raise CommandError(_NO_CONFIRM_MESSAGE)

        try:
            cfg = validate_required_env()
        except EnvConfigError as exc:
            raise CommandError(str(exc)) from exc

        _require_upload_dev_env(cfg)

        file_path = Path(os.path.expanduser(options["file_path"])).resolve()
        if not file_path.is_file():
            raise CommandError(f"Not a file or file missing: {file_path}")

        mime_type = _mime_type_for_path(file_path)
        language_hint = _normalize_language(options["language"])
        text_input_type = _normalize_text_input_type(options["text_input_type"])
        prompt_variant = _resolve_prompt_variant(options.get("prompt_variant"))
        preview_limit = max(0, int(options["text_preview_limit"]))

        route = OcrRouteConfig(
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=prompt_variant,
        )

        file_bytes = file_path.read_bytes()
        try:
            pages = extract_pages(file_bytes=file_bytes, mime_type=mime_type)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        result = transcribe_pages(
            pages=pages,
            language_hint=language_hint,
            text_input_type=text_input_type,
            route=route,
            worker_env=cfg,
        )

        text = result.text or ""
        preview = text if len(text) <= preview_limit else text[:preview_limit] + "…"

        self.stdout.write(f"engine_name: {result.engine_name}")
        self.stdout.write(f"needs_review: {result.needs_review}")
        self.stdout.write(
            f"review_reasons: {list(getattr(result, 'review_reasons', None) or [])}"
        )
        self.stdout.write(f"text_length: {len(text)}")
        self.stdout.write("--- text preview ---")
        self.stdout.write(preview if preview else "(empty)")
