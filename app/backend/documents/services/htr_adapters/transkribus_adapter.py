from __future__ import annotations

import logging
import requests
from typing import List, Optional

from documents.models import DocumentTextResult, TranskribusRun
from documents.services import transkribus_engine as tr
from documents.services.htr_adapters.base import (
    EnginePermanentError,
    EngineRetryableError,
    HtrResult,
)
from documents.services.page_extraction import PageImage
from documents.services import transkribus_run_persistence as trp

logger = logging.getLogger(__name__)

_BOTH_DEV_MODES_MESSAGE = (
    "Transkribus dev modes are mutually exclusive: do not enable both "
    "TRANSKRIBUS_USE_EXISTING_SERVER_DOCUMENT and TRANSKRIBUS_DEV_UPLOAD_MODE."
)

_NO_DEV_MODE_MESSAGE = (
    "Transkribus adapter is disabled. Enable exactly one dev mode: "
    "TRANSKRIBUS_USE_EXISTING_SERVER_DOCUMENT=true for dev/demo against a "
    "pre-existing TrpServer document, or TRANSKRIBUS_DEV_UPLOAD_MODE=true to "
    "upload PageImage[] into a new Transkribus document then run PyLaia. "
    "Production routing and VS-Archive-wide upload defaults remain deferred."
)

_MISSING_DOCUMENT_ID_MESSAGE = (
    "TranskribusAdapter requires document_id (supplied by run_worker via transcribe_pages)."
)


class TranskribusAdapter:
    """
    Transkribus Legacy TrpServer / PyLaia adapter.

    Unless a **dev-only** env gate is enabled, fails fast before any HTTP.

    Modes (mutually exclusive):

    - ``TRANSKRIBUS_USE_EXISTING_SERVER_DOCUMENT``: PyLaia on a fixed server
      document id / pages query (PR #2 path).
    - ``TRANSKRIBUS_DEV_UPLOAD_MODE``: upload ``PageImage[]`` to a new TrpServer
      document, then PyLaia (engine composition; still not production routing).
    """

    engine_key = "TRANSKRIBUS"

    def execute(
        self,
        pages: List[PageImage],
        language_hint: Optional[str],
        prompt_variant: str,
        **kwargs,
    ) -> HtrResult:
        worker_env = kwargs.pop("worker_env", None)
        if worker_env is None:
            raise EnginePermanentError(
                "TranskribusAdapter requires worker_env (supplied by run_worker)."
            )

        use_existing = getattr(
            worker_env, "transkribus_use_existing_server_document", False
        )
        dev_upload = getattr(worker_env, "transkribus_dev_upload_mode", False)

        if use_existing and dev_upload:
            raise EnginePermanentError(_BOTH_DEV_MODES_MESSAGE)

        if not use_existing and not dev_upload:
            raise EnginePermanentError(_NO_DEV_MODE_MESSAGE)

        if not pages:
            raise EnginePermanentError(
                "TranskribusAdapter requires at least one PageImage. "
                "For existing-server-document mode, pages are validation-only; "
                "for dev upload mode, image bytes are uploaded to TrpServer."
            )

        document_id = kwargs.get("document_id")
        if document_id is None:
            raise EnginePermanentError(_MISSING_DOCUMENT_ID_MESSAGE)
        try:
            document_id_int = int(document_id)
        except (TypeError, ValueError) as exc:
            raise EnginePermanentError(
                f"TranskribusAdapter requires a valid integer document_id, got {document_id!r}"
            ) from exc

        if use_existing:
            return self._execute_existing_server_document(
                worker_env, pages, document_id=document_id_int
            )

        return self._execute_dev_upload(
            worker_env, pages, document_id=document_id_int
        )

    def _execute_existing_server_document(
        self,
        worker_env: object,
        pages: List[PageImage],
        *,
        document_id: int,
    ) -> HtrResult:
        self._validate_existing_document_config(worker_env)

        username = getattr(worker_env, "transkribus_username", None) or ""
        password = getattr(worker_env, "transkribus_password", None) or ""
        bearer = getattr(worker_env, "transkribus_api_token", None) or ""
        collection_id = getattr(worker_env, "transkribus_collection_id", None) or ""
        model_id = getattr(worker_env, "transkribus_model_id", None) or ""
        remote_doc_id = (
            getattr(worker_env, "transkribus_dev_existing_document_id", None) or ""
        )
        pages_query = getattr(worker_env, "transkribus_dev_existing_pages", None) or ""
        engine_runtime = f"transkribus-pylaia:{model_id}"

        run = trp.start_run(
            document_id=document_id,
            mode=TranskribusRun.Mode.EXISTING_SERVER,
            collection_id=collection_id,
            model_id=model_id,
            remote_doc_id=remote_doc_id,
            pages_query=pages_query,
        )

        try:
            with requests.Session() as session:
                tr.login_trp_server(session, username=username, password=password)
                recognition_job_id = tr.start_pylaia_recognition(
                    session,
                    collection_id=collection_id,
                    model_id=model_id,
                    document_id=remote_doc_id,
                    pages_query=pages_query,
                )
                trp.mark_recognition_started(run, recognition_job_id=recognition_job_id)
                outcome = tr.complete_pylaia_transcription_after_job(
                    session,
                    recognition_job_id=recognition_job_id,
                    collection_id=collection_id,
                    model_id=model_id,
                    document_id=remote_doc_id,
                    pages_query=pages_query,
                    bearer_token=bearer,
                )
                trp.mark_succeeded(run, engine_runtime=engine_runtime)
                return HtrResult(
                    text=outcome.text,
                    needs_review=bool(outcome.review_reasons),
                    engine_name=engine_runtime,
                    review_reasons=list(outcome.review_reasons),
                )
        except tr.TranskribusRetryableError as exc:
            trp.mark_failed(
                run,
                error_code="TRANSKRIBUS_RECOGNITION_FAILED",
                error_details=str(exc),
            )
            raise EngineRetryableError(str(exc)) from exc
        except tr.TranskribusPermanentError as exc:
            trp.mark_failed(
                run,
                error_code="TRANSKRIBUS_RECOGNITION_FAILED",
                error_details=str(exc),
            )
            raise EnginePermanentError(str(exc)) from exc

    def _execute_dev_upload(
        self,
        worker_env: object,
        pages: List[PageImage],
        *,
        document_id: int,
    ) -> HtrResult:
        self._validate_upload_dev_config(worker_env)

        username = getattr(worker_env, "transkribus_username", None) or ""
        password = getattr(worker_env, "transkribus_password", None) or ""
        bearer = getattr(worker_env, "transkribus_api_token", None) or ""
        collection_id = getattr(worker_env, "transkribus_collection_id", None) or ""
        model_id = getattr(worker_env, "transkribus_model_id", None) or ""
        engine_runtime = f"transkribus-pylaia:{model_id}"

        force_reprocess = getattr(worker_env, "transkribus_force_reprocess", False)
        recognition_only_retry = getattr(
            worker_env, "transkribus_recognition_only_retry", False
        )

        if force_reprocess:
            return self._execute_dev_upload_with_new_trp_document(
                document_id=document_id,
                pages=pages,
                username=username,
                password=password,
                bearer=bearer,
                collection_id=collection_id,
                model_id=model_id,
                engine_runtime=engine_runtime,
            )

        if recognition_only_retry:
            source_run = trp.find_reusable_upload_run(
                document_id=document_id,
                collection_id=collection_id,
                model_id=model_id,
            )
            if source_run is not None:
                return self._execute_dev_recognition_only(
                    document_id=document_id,
                    pages=pages,
                    source_run=source_run,
                    username=username,
                    password=password,
                    bearer=bearer,
                    collection_id=collection_id,
                    model_id=model_id,
                    engine_runtime=engine_runtime,
                )

        blocking = trp.find_blocking_upload_run(
            document_id=document_id,
            collection_id=collection_id,
            model_id=model_id,
        )
        if blocking is not None:
            logger.warning(
                "Transkribus upload blocked for document_id=%s "
                "blocking_run_id=%s blocking_run_status=%s "
                "blocking_remote_doc_id=%s collection_id=%s model_id=%s",
                document_id,
                blocking.id,
                blocking.status,
                blocking.remote_doc_id or "",
                str(collection_id).strip(),
                str(model_id).strip(),
            )
            raise EnginePermanentError(
                trp.format_upload_blocked_error_message(
                    document_id=document_id,
                    collection_id=collection_id,
                    model_id=model_id,
                    blocking_run=blocking,
                )
            )

        return self._execute_dev_upload_with_new_trp_document(
            document_id=document_id,
            pages=pages,
            username=username,
            password=password,
            bearer=bearer,
            collection_id=collection_id,
            model_id=model_id,
            engine_runtime=engine_runtime,
        )

    def _execute_dev_recognition_only(
        self,
        *,
        document_id: int,
        pages: List[PageImage],
        source_run: TranskribusRun,
        username: str,
        password: str,
        bearer: str,
        collection_id: str,
        model_id: str,
        engine_runtime: str,
    ) -> HtrResult:
        self._guard_verified_text_results(document_id=document_id)
        self._guard_page_count_matches_source_mapping(pages=pages, source_run=source_run)

        remote_doc_id = str(source_run.remote_doc_id).strip()
        pages_query = str(source_run.pages_query).strip()

        logger.warning(
            "Transkribus recognition-only retry for document_id=%s "
            "source_run_id=%s source_run_status=%s remote_doc_id=%s "
            "collection_id=%s model_id=%s",
            document_id,
            source_run.id,
            source_run.status,
            remote_doc_id,
            str(collection_id).strip(),
            str(model_id).strip(),
        )

        run = trp.start_run(
            document_id=document_id,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id=collection_id,
            model_id=model_id,
        )
        trp.apply_source_upload_metadata(run, source=source_run)

        try:
            with requests.Session() as session:
                tr.login_trp_server(session, username=username, password=password)
                recognition_job_id = tr.start_pylaia_recognition(
                    session,
                    collection_id=collection_id,
                    model_id=model_id,
                    document_id=remote_doc_id,
                    pages_query=pages_query,
                )
                trp.mark_recognition_started(run, recognition_job_id=recognition_job_id)
                outcome = tr.complete_pylaia_transcription_after_job(
                    session,
                    recognition_job_id=recognition_job_id,
                    collection_id=collection_id,
                    model_id=model_id,
                    document_id=remote_doc_id,
                    pages_query=pages_query,
                    bearer_token=bearer,
                )
                trp.mark_succeeded(run, engine_runtime=engine_runtime)
                return HtrResult(
                    text=outcome.text,
                    needs_review=bool(outcome.review_reasons),
                    engine_name=engine_runtime,
                    review_reasons=list(outcome.review_reasons),
                )
        except tr.TranskribusRetryableError as exc:
            trp.mark_failed(
                run,
                error_code="TRANSKRIBUS_RECOGNITION_FAILED",
                error_details=str(exc),
            )
            raise EngineRetryableError(str(exc)) from exc
        except tr.TranskribusPermanentError as exc:
            trp.mark_failed(
                run,
                error_code="TRANSKRIBUS_RECOGNITION_FAILED",
                error_details=str(exc),
            )
            raise EnginePermanentError(str(exc)) from exc

    def _execute_dev_upload_with_new_trp_document(
        self,
        *,
        document_id: int,
        pages: List[PageImage],
        username: str,
        password: str,
        bearer: str,
        collection_id: str,
        model_id: str,
        engine_runtime: str,
    ) -> HtrResult:
        run = trp.start_run(
            document_id=document_id,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id=collection_id,
            model_id=model_id,
        )

        try:
            with requests.Session() as session:
                tr.login_trp_server(session, username=username, password=password)
                upload_out = tr.run_trp_upload_page_images_through_ingest(
                    session,
                    collection_id=collection_id,
                    pages=pages,
                )
                trp.mark_uploaded(
                    run,
                    remote_doc_id=upload_out.doc_id,
                    upload_id=upload_out.upload_id,
                    ingest_job_id=upload_out.ingest_job_id,
                    pages_query=upload_out.pages_query,
                    page_index_to_page_nr=upload_out.page_index_to_page_nr,
                )
                recognition_job_id = tr.start_pylaia_recognition(
                    session,
                    collection_id=collection_id,
                    model_id=model_id,
                    document_id=upload_out.doc_id,
                    pages_query=upload_out.pages_query,
                )
                trp.mark_recognition_started(run, recognition_job_id=recognition_job_id)
                outcome = tr.complete_pylaia_transcription_after_job(
                    session,
                    recognition_job_id=recognition_job_id,
                    collection_id=collection_id,
                    model_id=model_id,
                    document_id=upload_out.doc_id,
                    pages_query=upload_out.pages_query,
                    bearer_token=bearer,
                )
                trp.mark_succeeded(run, engine_runtime=engine_runtime)
                return HtrResult(
                    text=outcome.text,
                    needs_review=bool(outcome.review_reasons),
                    engine_name=engine_runtime,
                    review_reasons=list(outcome.review_reasons),
                )
        except tr.TranskribusRetryableError as exc:
            error_code = (
                "TRANSKRIBUS_UPLOAD_FAILED"
                if run.status == TranskribusRun.Status.STARTED
                else "TRANSKRIBUS_RECOGNITION_FAILED"
            )
            trp.mark_failed(run, error_code=error_code, error_details=str(exc))
            raise EngineRetryableError(str(exc)) from exc
        except tr.TranskribusPermanentError as exc:
            error_code = (
                "TRANSKRIBUS_UPLOAD_FAILED"
                if run.status == TranskribusRun.Status.STARTED
                else "TRANSKRIBUS_RECOGNITION_FAILED"
            )
            trp.mark_failed(run, error_code=error_code, error_details=str(exc))
            raise EnginePermanentError(str(exc)) from exc

    @staticmethod
    def _guard_verified_text_results(*, document_id: int) -> None:
        if DocumentTextResult.objects.filter(
            document_id=document_id,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
        ).exists():
            raise EnginePermanentError(
                f"Transkribus recognition-only retry blocked: document_id={document_id} "
                "has VERIFIED DocumentTextResult row(s). "
                "Recognition-only retry must not overwrite human-verified text."
            )

    @staticmethod
    def _guard_page_count_matches_source_mapping(
        *,
        pages: List[PageImage],
        source_run: TranskribusRun,
    ) -> None:
        raw_mapping = source_run.page_index_to_page_nr
        if not raw_mapping:
            return
        if isinstance(raw_mapping, dict):
            mapping_len = len(raw_mapping)
        else:
            mapping_len = len(dict(raw_mapping))
        page_count = len(pages)
        if page_count != mapping_len:
            raise EnginePermanentError(
                "Transkribus recognition-only retry blocked: "
                f"current PageImage count ({page_count}) does not match "
                f"stored page mapping count ({mapping_len}) on source TranskribusRun "
                f"id={source_run.id}."
            )

    @staticmethod
    def _validate_existing_document_config(worker_env: object) -> None:
        missing: list[str] = []
        if not (getattr(worker_env, "transkribus_username", None) and getattr(
            worker_env, "transkribus_password", None
        )):
            missing.append("TRANSKRIBUS_USERNAME and TRANSKRIBUS_PASSWORD")
        if not getattr(worker_env, "transkribus_api_token", None):
            missing.append("TRANSKRIBUS_API_TOKEN")
        if not getattr(worker_env, "transkribus_collection_id", None):
            missing.append("TRANSKRIBUS_COLLECTION_ID")
        if not getattr(worker_env, "transkribus_model_id", None):
            missing.append("TRANSKRIBUS_MODEL_ID")
        if not getattr(worker_env, "transkribus_dev_existing_document_id", None):
            missing.append("TRANSKRIBUS_DEV_EXISTING_DOCUMENT_ID")
        if not getattr(worker_env, "transkribus_dev_existing_pages", None):
            missing.append("TRANSKRIBUS_DEV_EXISTING_PAGES")
        if missing:
            raise EnginePermanentError(
                "Transkribus dev/demo configuration incomplete: "
                + ", ".join(missing)
            )

    @staticmethod
    def _validate_upload_dev_config(worker_env: object) -> None:
        missing: list[str] = []
        if not (getattr(worker_env, "transkribus_username", None) and getattr(
            worker_env, "transkribus_password", None
        )):
            missing.append("TRANSKRIBUS_USERNAME and TRANSKRIBUS_PASSWORD")
        if not getattr(worker_env, "transkribus_api_token", None):
            missing.append("TRANSKRIBUS_API_TOKEN")
        if not getattr(worker_env, "transkribus_collection_id", None):
            missing.append("TRANSKRIBUS_COLLECTION_ID")
        if not getattr(worker_env, "transkribus_model_id", None):
            missing.append("TRANSKRIBUS_MODEL_ID")
        if missing:
            raise EnginePermanentError(
                "Transkribus dev upload mode configuration incomplete: "
                + ", ".join(missing)
            )
