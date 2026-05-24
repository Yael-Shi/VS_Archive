from __future__ import annotations

import requests
from typing import List, Optional

from documents.models import TranskribusRun
from documents.services import transkribus_engine as tr
from documents.services.htr_adapters.base import (
    EnginePermanentError,
    EngineRetryableError,
    HtrResult,
)
from documents.services.page_extraction import PageImage
from documents.services import transkribus_run_persistence as trp

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
