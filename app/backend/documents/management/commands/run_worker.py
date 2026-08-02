import hashlib
import json
import logging
import os
import time
from dataclasses import replace
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from documents.models import Document, DocumentTextResult
from documents.s3 import get_object_bytes
from documents.services.env_validation import (
    EnvConfigError,
    WorkerEnvConfig,
    validate_required_env,
)
from documents.services.gemini_engine import translate_text_to_hebrew_with_gemini
from documents.services.non_hebrew_hebrew_translation import (
    persist_hebrew_translation_result,
)
from documents.services.processing_state import (
    update_document_processing_state_for_engine,
)
from documents.services.gemini_models import DEFAULT_GEMINI_MODEL
from documents.services.htr_adapters.base import (
    EnginePageCheckpointBusyError,
    EnginePageCheckpointPersistenceRetryableError,
    EnginePageIncompleteError,
    UnsupportedEngineError,
    TranskribusLocalPersistenceRetryableError,
)
from documents.services.hebrew_translation_retry import (
    PROCESS_DOCUMENT_OPERATION_KEY,
    RETRY_HEBREW_TRANSLATION_OPERATION,
    execute_hebrew_translation_retry,
)
from documents.services.htr_engine import transcribe_pages
from documents.services.ocr_reprocess import (
    OCR_RETRY_MODE_PAYLOAD_KEY,
    OcrRetryMode,
    SOURCE_TRANSKRIBUS_RUN_ID_PAYLOAD_KEY,
)
from documents.services.ocr_routing import OcrRouteConfig, select_ocr_route
from documents.services.process_document_outcome import (
    ProcessDocumentDisposition,
    ProcessDocumentOutcome,
)
from documents.services.process_document_request_worker import (
    PROCESS_DOCUMENT_REQUEST_ID_PAYLOAD_KEY,
    handle_process_document_request,
)
from documents.services.review_reasons import (
    AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW,
    HAS_UNCLEAR,
    MIN_TEXT_LENGTH,
    NEEDS_REVIEW_FLAG,
)
from documents.services.page_extraction import extract_pages, source_file_bytes_to_page
from documents.services.source_files import (
    MultiImageSourceFilesError,
    get_ordered_source_files_for_processing,
    is_multi_image_document,
)
from documents.services.sqs import SYNC_TRANSKRIBUS_CORRECTED_CURRENT
from documents.services.transkribus_corrected_current_sync_worker import (
    handle_sync_transkribus_corrected_current,
)
from documents.services.transkribus_local_completion import (
    complete_transkribus_local_success,
)

logger = logging.getLogger(__name__)

UNRESOLVED_ROUTE_METADATA = "UNRESOLVED"


def _dedupe_strings_preserve_order(items: List[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _is_hebrew_language(language: Optional[str]) -> bool:
    lang = (language or "").strip().lower()
    return lang in ("he", "heb", "hebrew")


def _outcome_for_final_processing_state(
    processing_state: str,
) -> ProcessDocumentOutcome:
    """Map final Document state without conflating PARTIAL with FAILED."""

    if processing_state == Document.ProcessingState.READY:
        return ProcessDocumentOutcome(ProcessDocumentDisposition.COMPLETED)
    if processing_state == Document.ProcessingState.PARTIAL:
        return ProcessDocumentOutcome(
            ProcessDocumentDisposition.PARTIAL,
            failure_code="PROCESS_DOCUMENT_PARTIAL",
        )
    return ProcessDocumentOutcome(
        ProcessDocumentDisposition.FAILED,
        failure_code="PROCESS_DOCUMENT_INCOMPLETE",
        failure_message=(
            f"Unexpected final processing_state_user={processing_state!r}."
        ),
    )


class Command(BaseCommand):
    help = "Run the async worker: poll SQS and process document jobs."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--sleep-seconds", type=int, default=2)
        parser.add_argument("--max-messages", type=int, default=1)
        parser.add_argument("--wait-seconds", type=int, default=20)

    def handle(self, *args, **options):
        try:
            self._cfg: WorkerEnvConfig = validate_required_env()
        except EnvConfigError as e:
            self.stderr.write(self.style.ERROR(f"[run_worker] env error: {e}"))
            raise SystemExit(1)

        queue_url = _env("SQS_QUEUE_URL")
        region = os.getenv("AWS_REGION") or "eu-central-1"
        sqs = boto3.client("sqs", region_name=region)

        self.stdout.write(
            self.style.SUCCESS(f"[run_worker] starting | queue={queue_url}")
        )

        while True:
            msg = self._receive_one(
                sqs,
                queue_url,
                options["max_messages"],
                options["wait_seconds"],
            )

            if msg is None:
                if options["once"]:
                    return
                time.sleep(options["sleep_seconds"])
                continue

            ok = self._process_message(msg, sqs=sqs, queue_url=queue_url)

            if ok:
                self._delete_message(sqs, queue_url, msg)

            if options["once"]:
                return

    # ------------------------------------------------------------------ SQS Helpers

    def _receive_one(self, sqs, queue_url, max_msgs, wait):
        try:
            resp = sqs.receive_message(
                QueueUrl=queue_url,
                MaxNumberOfMessages=max(1, min(max_msgs, 10)),
                WaitTimeSeconds=max(0, min(wait, 20)),
                VisibilityTimeout=300,
            )
            msgs = resp.get("Messages") or []
            return msgs[0] if msgs else None
        except (BotoCoreError, ClientError) as e:
            self.stderr.write(self.style.ERROR(f"SQS receive error: {e}"))
            return None

    def _delete_message(self, sqs, queue_url, msg):
        try:
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])
        except Exception as e:
            self.stderr.write(f"SQS delete error: {e}")

    def _is_invalid_ocr_retry_mode(self, payload: Dict[str, Any]) -> bool:
        if OCR_RETRY_MODE_PAYLOAD_KEY not in payload:
            return False
        return (
            payload.get(OCR_RETRY_MODE_PAYLOAD_KEY)
            != OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY.value
        )

    def _recognition_only_payload_error(self, payload: Dict[str, Any]) -> Optional[str]:
        if (
            payload.get(OCR_RETRY_MODE_PAYLOAD_KEY)
            != OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY.value
        ):
            return None
        run_id = payload.get(SOURCE_TRANSKRIBUS_RUN_ID_PAYLOAD_KEY)
        if run_id is None:
            return (
                "source_transkribus_run_id is required for transkribus_recognition_only"
            )
        if not isinstance(run_id, int):
            return (
                "source_transkribus_run_id must be int for transkribus_recognition_only, "
                f"got {type(run_id).__name__}"
            )
        return None

    def _effective_worker_env(self, payload: Dict[str, Any]) -> WorkerEnvConfig:
        if (
            payload.get(OCR_RETRY_MODE_PAYLOAD_KEY)
            == OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY.value
        ):
            return replace(
                self._cfg,
                transkribus_recognition_only_retry=True,
            )
        return self._cfg

    # ------------------------------------------------------------------ Core Logic

    def _process_message(
        self,
        msg: Dict[str, Any],
        *,
        sqs=None,
        queue_url: Optional[str] = None,
    ) -> bool:
        try:
            payload = json.loads(msg.get("Body", "{}"))
        except Exception:
            return True

        msg_type = payload.get("type")
        if msg_type == SYNC_TRANSKRIBUS_CORRECTED_CURRENT:
            receipt_handle = msg.get("ReceiptHandle")
            if (
                sqs is None
                or not queue_url
                or not isinstance(receipt_handle, str)
                or not receipt_handle
            ):
                logger.error(
                    "SYNC_TRANSKRIBUS_CORRECTED_CURRENT missing SQS context; "
                    "cannot claim/defer safely"
                )
                return False
            return handle_sync_transkribus_corrected_current(
                payload,
                sqs=sqs,
                queue_url=queue_url,
                receipt_handle=receipt_handle,
                worker_env=self._cfg,
            )

        if msg_type != "PROCESS_DOCUMENT":
            return True

        if PROCESS_DOCUMENT_REQUEST_ID_PAYLOAD_KEY in payload:
            receipt_handle = msg.get("ReceiptHandle")
            if (
                sqs is None
                or not queue_url
                or not isinstance(receipt_handle, str)
                or not receipt_handle
            ):
                logger.error(
                    "Request-aware PROCESS_DOCUMENT missing SQS context; "
                    "cannot claim/defer safely"
                )
                return False

            return handle_process_document_request(
                payload,
                sqs=sqs,
                queue_url=queue_url,
                receipt_handle=receipt_handle,
                execute_payload=self._execute_process_document_payload,
            )

        # Backward compatibility for messages created before durable Requests.
        return self._execute_process_document_payload(payload).should_ack

    def _execute_process_document_payload(
        self,
        payload: Dict[str, Any],
    ) -> ProcessDocumentOutcome:
        document_id = payload.get("document_id")
        if not isinstance(document_id, int):
            return ProcessDocumentOutcome(ProcessDocumentDisposition.NOOP)

        operation = payload.get(PROCESS_DOCUMENT_OPERATION_KEY)
        if operation == RETRY_HEBREW_TRANSLATION_OPERATION:
            return execute_hebrew_translation_retry(
                document_id,
                worker_env=self._cfg,
            )
        if operation is not None:
            self.stderr.write(
                self.style.ERROR(
                    f"Invalid operation for doc {document_id}: {operation!r}"
                )
            )
            logger.error(
                "invalid operation in PROCESS_DOCUMENT payload",
                extra={"document_id": document_id, "operation": operation},
            )
            return ProcessDocumentOutcome(
                ProcessDocumentDisposition.NOOP,
                failure_code="INVALID_PROCESS_DOCUMENT_OPERATION",
                failure_message=repr(operation),
            )

        if self._is_invalid_ocr_retry_mode(payload):
            retry_mode = payload.get(OCR_RETRY_MODE_PAYLOAD_KEY)
            self.stderr.write(
                self.style.ERROR(
                    f"Invalid ocr_retry_mode for doc {document_id}: {retry_mode!r}"
                )
            )
            logger.error(
                "invalid ocr_retry_mode in PROCESS_DOCUMENT payload",
                extra={"document_id": document_id, "ocr_retry_mode": retry_mode},
            )
            return ProcessDocumentOutcome(
                ProcessDocumentDisposition.NOOP,
                failure_code="INVALID_OCR_RETRY_MODE",
                failure_message=repr(retry_mode),
            )

        recognition_only_error = self._recognition_only_payload_error(payload)
        if recognition_only_error is not None:
            self.stderr.write(
                self.style.ERROR(
                    f"Invalid recognition-only payload for doc {document_id}: "
                    f"{recognition_only_error}"
                )
            )
            logger.error(
                "invalid recognition-only PROCESS_DOCUMENT payload",
                extra={
                    "document_id": document_id,
                    OCR_RETRY_MODE_PAYLOAD_KEY: payload.get(OCR_RETRY_MODE_PAYLOAD_KEY),
                    SOURCE_TRANSKRIBUS_RUN_ID_PAYLOAD_KEY: payload.get(
                        SOURCE_TRANSKRIBUS_RUN_ID_PAYLOAD_KEY
                    ),
                    "error": recognition_only_error,
                },
            )
            return ProcessDocumentOutcome(
                ProcessDocumentDisposition.NOOP,
                failure_code="INVALID_RECOGNITION_ONLY_PAYLOAD",
                failure_message=recognition_only_error,
            )

        # Phase 1: Mark PROCESSING
        try:
            with transaction.atomic():
                doc = Document.objects.select_for_update().get(id=document_id)
                if doc.upload_status != Document.UploadStatus.UPLOADED:
                    return ProcessDocumentOutcome(ProcessDocumentDisposition.NOOP)
                doc.processing_state_user = Document.ProcessingState.PROCESSING
                doc.save(update_fields=["processing_state_user", "updated_at"])
        except Document.DoesNotExist:
            return ProcessDocumentOutcome(ProcessDocumentDisposition.NOOP)

        # Pre-flight: multi-image source-file validation.
        # Input-integrity failures here are distinct from OCR/HTR failures: mark the document
        # FAILED, do not dispatch to adapters, and do not create misleading DocumentTextResult
        # rows.
        is_multi = is_multi_image_document(doc)
        ordered_sources = None
        if is_multi:
            try:
                ordered_sources = get_ordered_source_files_for_processing(doc)
            except MultiImageSourceFilesError as e:
                self.stderr.write(
                    self.style.ERROR(
                        f"Multi-image validation failed for doc {document_id}: {e}"
                    )
                )
                logger.error(
                    "multi-image source file validation failed",
                    extra={"document_id": document_id},
                )
                try:
                    with transaction.atomic():
                        doc = Document.objects.select_for_update().get(id=document_id)
                        doc.processing_state_user = Document.ProcessingState.FAILED
                        doc.save(update_fields=["processing_state_user"])
                except Document.DoesNotExist:
                    pass
                return ProcessDocumentOutcome(
                    ProcessDocumentDisposition.FAILED,
                    failure_code="MULTI_IMAGE_SOURCE_INVALID",
                    failure_message=str(e),
                )

        # Phase 2: Heavy work
        error: Optional[str] = None
        htr_result = None
        processing_exc: Optional[Exception] = None
        route: Optional[OcrRouteConfig] = None

        try:
            bucket = getattr(settings, "UPLOADS_BUCKET_NAME", "")
            if is_multi:
                pages = self._build_pages_from_source_files(
                    bucket, ordered_sources or []
                )
            else:
                file_bytes, s3_mime = get_object_bytes(
                    bucket=bucket, key=doc.file_s3_key
                )
                effective_mime = (doc.mime_type or s3_mime or "").strip()
                pages = extract_pages(file_bytes=file_bytes, mime_type=effective_mime)
                source_content_fingerprint = hashlib.sha256(file_bytes).hexdigest()
                pages = [
                    replace(
                        page,
                        source_identity=doc.file_s3_key,
                        source_content_fingerprint=source_content_fingerprint,
                    )
                    for page in pages
                ]

            route = select_ocr_route(
                doc.language,
                doc.text_input_type,
                handwriting_type=doc.handwriting_type,
            )
            source_transkribus_run_id: int | None = None
            if (
                payload.get(OCR_RETRY_MODE_PAYLOAD_KEY)
                == OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY.value
            ):
                if route.engine_key != DocumentTextResult.OcrEngineKey.TRANSKRIBUS:
                    raise RuntimeError(
                        f"transkribus_recognition_only retry requested for "
                        f"document_id={document_id} but selected OCR route is "
                        f"{route.engine_key}"
                    )
                source_transkribus_run_id = payload.get(
                    SOURCE_TRANSKRIBUS_RUN_ID_PAYLOAD_KEY
                )
            htr_result = transcribe_pages(
                pages=pages,
                language_hint=doc.language,
                text_input_type=doc.text_input_type,
                handwriting_type=doc.handwriting_type,
                route=route,
                worker_env=self._effective_worker_env(payload),
                document_id=document_id,
                source_transkribus_run_id=source_transkribus_run_id,
            )

        except EnginePageCheckpointBusyError as e:
            logger.info(
                "page checkpoint is held by another worker",
                extra={"document_id": document_id},
            )
            return ProcessDocumentOutcome(
                ProcessDocumentDisposition.RETRYABLE,
                failure_code=e.failure_code,
                failure_message=e.safe_message,
            )
        except EnginePageCheckpointPersistenceRetryableError as e:
            logger.error(
                "page checkpoint persistence failed; leaving SQS message",
                extra={
                    "document_id": document_id,
                    "checkpoint_stage": e.stage,
                    "page_index": e.page_index,
                },
            )
            return ProcessDocumentOutcome(
                ProcessDocumentDisposition.RETRYABLE,
                failure_code=e.failure_code,
                failure_message=e.safe_message,
            )
        except EnginePageIncompleteError as e:
            try:
                with transaction.atomic():
                    doc = Document.objects.select_for_update().get(id=document_id)
                    doc.processing_state_user = Document.ProcessingState.PARTIAL
                    doc.save(update_fields=["processing_state_user", "updated_at"])
            except Document.DoesNotExist:
                return ProcessDocumentOutcome(ProcessDocumentDisposition.NOOP)
            return ProcessDocumentOutcome(
                ProcessDocumentDisposition.PARTIAL,
                failure_code=e.failure_code,
                failure_message=e.safe_message,
            )
        except TranskribusLocalPersistenceRetryableError as e:
            # Durable recognition may already exist; do not persist OCR failure or ack.
            self.stderr.write(
                self.style.ERROR(
                    f"Transkribus local persistence retryable for doc {document_id}: {e}"
                )
            )
            logger.error(
                "transkribus local persistence retryable; leaving SQS message",
                extra={"document_id": document_id},
            )
            return ProcessDocumentOutcome(
                ProcessDocumentDisposition.RETRYABLE,
                failure_code="TRANSKRIBUS_LOCAL_PERSISTENCE_RETRYABLE",
                failure_message=str(e),
            )
        except Exception as e:
            processing_exc = e
            error = str(e)
            self.stderr.write(
                self.style.ERROR(f"Processing error for doc {document_id}: {e}")
            )

        # Phase 3: Save results
        if (
            not error
            and htr_result is not None
            and route is not None
            and htr_result.transkribus_snapshot_id is not None
            and htr_result.transkribus_run_id is not None
        ):
            # Transkribus automatic snapshot: DTR + bindings + mark_succeeded in one
            # dedicated transaction (no S3/HTTP). Failures propagate → no SQS ack.
            complete_transkribus_local_success(
                document_id=document_id,
                run_id=htr_result.transkribus_run_id,
                snapshot_id=htr_result.transkribus_snapshot_id,
                text=htr_result.text,
                engine=htr_result.engine_name,
                route=route,
                needs_review=htr_result.needs_review,
                review_reasons=getattr(htr_result, "review_reasons", None),
                min_text_length=self._cfg.min_text_length,
            )
            doc.refresh_from_db(fields=["processing_state_user"])
            return _outcome_for_final_processing_state(doc.processing_state_user)

        try:
            with transaction.atomic():
                doc = Document.objects.select_for_update().get(id=document_id)
                if htr_result:
                    final_engine = htr_result.engine_name
                elif isinstance(processing_exc, UnsupportedEngineError):
                    final_engine = f"unsupported:{processing_exc.engine_key}"
                else:
                    final_engine = "ocr-dispatch"
                is_he = _is_hebrew_language(doc.language)

                if error:
                    self._save_ocr_failure(doc, final_engine, is_he, error)
                else:
                    if route is None or htr_result is None:
                        raise RuntimeError(
                            "Internal error: OCR success path missing route or HTR result"
                        )
                    self._save_htr_results(doc, final_engine, is_he, htr_result, route)

                self._update_processing_state(doc, final_engine)
                doc.save(update_fields=["processing_state_user"])
                # One sync after final OCR/translation/failure display state.
                # Lock order: Document (held) → ArchiveItem (inside sync).
                from documents.services.archive_search_index import (
                    sync_archive_item_search_index,
                )

                sync_archive_item_search_index(doc.archive_item_id)
        except Document.DoesNotExist:
            return ProcessDocumentOutcome(ProcessDocumentDisposition.NOOP)

        if error:
            return ProcessDocumentOutcome(
                ProcessDocumentDisposition.FAILED,
                failure_code="OCR_PROCESSING_FAILED",
                failure_message=error,
            )

        return _outcome_for_final_processing_state(doc.processing_state_user)

    def _build_pages_from_source_files(self, bucket, ordered_sources):
        """
        Download each ordered ``DocumentSourceFile`` from S3 and build a PageImage list.

        S3 I/O stays in the worker; ``source_file_bytes_to_page`` is the pure conversion that
        normalizes bytes to PNG and assigns the page index. Pages are returned in
        ``order_index`` order.
        """
        pages = []
        for source in ordered_sources:
            file_bytes, _s3_mime = get_object_bytes(
                bucket=bucket, key=source.file_s3_key
            )
            source_content_fingerprint = hashlib.sha256(file_bytes).hexdigest()
            pages.append(
                source_file_bytes_to_page(
                    order_index=source.order_index,
                    file_bytes=file_bytes,
                    mime_type=source.mime_type,
                    source_identity=f"{source.id}:{source.file_s3_key}",
                    source_content_fingerprint=source_content_fingerprint,
                )
            )
        return pages

    # ------------------------------------------------------------------ DB Helpers

    def _derive_review_reasons(
        self,
        text: str,
        adapter_needs_review: bool,
        engine_reasons: Optional[List[str]],
        *,
        include_automatic_policy: bool,
    ) -> List[str]:
        reasons: List[str] = []
        if include_automatic_policy:
            reasons.append(AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW)
        if adapter_needs_review:
            reasons.append(NEEDS_REVIEW_FLAG)

        stripped = (text or "").strip()
        if len(stripped) < self._cfg.min_text_length:
            reasons.append(MIN_TEXT_LENGTH)
        if "[UNCLEAR]" in stripped:
            reasons.append(HAS_UNCLEAR)

        if engine_reasons:
            for r in engine_reasons:
                if r:
                    reasons.append(r)

        return _dedupe_strings_preserve_order(reasons)

    def _save_htr_results(
        self,
        doc: Document,
        engine: str,
        is_he: bool,
        htr,
        route: OcrRouteConfig,
    ):
        status = DocumentTextResult.Status.NEEDS_REVIEW
        review_reasons = self._derive_review_reasons(
            htr.text,
            htr.needs_review,
            getattr(htr, "review_reasons", None),
            include_automatic_policy=True,
        )

        target_types = [DocumentTextResult.ResultType.SOURCE_TEXT]
        if is_he:
            target_types.append(DocumentTextResult.ResultType.HEBREW_TEXT)

        for r_type in target_types:
            DocumentTextResult.objects.update_or_create(
                document=doc,
                result_type=r_type,
                engine=engine,
                defaults={
                    "status": status,
                    "text": htr.text,
                    "engine_key": route.engine_key,
                    "prompt_variant": route.prompt_variant,
                    "verification_status": DocumentTextResult.VerificationStatus.UNVERIFIED,
                    "error_code": None,
                    "error_details": None,
                    "review_reasons": json.dumps(review_reasons),
                },
            )

        if not is_he:
            self._save_non_hebrew_hebrew_translation(doc, engine, htr.text)

    def _save_non_hebrew_hebrew_translation(
        self,
        doc: Document,
        engine: str,
        source_text: str,
    ):
        try:
            translation = translate_text_to_hebrew_with_gemini(
                source_text,
                doc.language,
                model_name=DEFAULT_GEMINI_MODEL,
                min_text_length=self._cfg.min_text_length,
                temperature=self._cfg.gemini_temperature,
                top_k=self._cfg.gemini_top_k,
                top_p=self._cfg.gemini_top_p,
                max_output_tokens=self._cfg.gemini_max_output_tokens,
            )
        except Exception as e:
            # Intentional broad catch: any Hebrew-translation failure is persisted as a
            # failed HEBREW_TEXT row and must not fail the already-successful SOURCE_TEXT
            # OCR persistence (this runs inside the Phase 3 save transaction). See
            # architecture.mdc / docs/ocr-routing-reference.md (non-Hebrew translation
            # failure -> HEBREW_TRANSLATION_FAILED, not an OCR failure).
            persist_hebrew_translation_result(
                doc,
                engine,
                error=e,
                min_text_length=self._cfg.min_text_length,
            )
            return

        persist_hebrew_translation_result(
            doc,
            engine,
            translation=translation,
            min_text_length=self._cfg.min_text_length,
        )

    def _save_ocr_failure(self, doc, engine, is_he, details):
        route_metadata = self._route_metadata_for_failure(doc)
        has_valid_route = route_metadata is not None
        if has_valid_route:
            engine_key, prompt_variant = route_metadata
            error_code = "OCR_FAILED"
        else:
            # Keep failure persistence explicit when routing metadata is invalid.
            # Avoid misleading fallback metadata such as GEMINI/handwritten.
            engine_key = UNRESOLVED_ROUTE_METADATA
            prompt_variant = UNRESOLVED_ROUTE_METADATA
            error_code = "OCR_ROUTING_INVALID"
        target_types = [DocumentTextResult.ResultType.SOURCE_TEXT]
        if is_he:
            target_types.append(DocumentTextResult.ResultType.HEBREW_TEXT)
        for r_type in target_types:
            DocumentTextResult.objects.update_or_create(
                document=doc,
                result_type=r_type,
                engine=engine,
                defaults={
                    "status": DocumentTextResult.Status.FAILED,
                    "text": None,
                    "engine_key": engine_key,
                    "prompt_variant": prompt_variant,
                    "verification_status": DocumentTextResult.VerificationStatus.UNVERIFIED,
                    "error_code": error_code,
                    "error_details": details,
                    "review_reasons": "",
                },
            )

    def _route_metadata_for_failure(self, doc: Document):
        """
        Re-select route on failure paths (no successful HtrResult).
        Success paths use the route selected in Phase 2 and passed into persistence.
        """
        try:
            route = select_ocr_route(
                doc.language,
                doc.text_input_type,
                handwriting_type=doc.handwriting_type,
            )
            return route.engine_key, route.prompt_variant
        except ValueError:
            return None

    def _update_processing_state(self, doc, engine):
        update_document_processing_state_for_engine(doc, engine)
