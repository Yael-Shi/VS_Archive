from __future__ import annotations

from typing import List, Optional

from documents.services import transkribus_engine as tr
from documents.services.htr_adapters.base import (
    EnginePermanentError,
    EngineRetryableError,
    HtrResult,
)
from documents.services.page_extraction import PageImage

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

        if use_existing:
            return self._execute_existing_server_document(worker_env, pages)

        return self._execute_dev_upload(worker_env, pages)

    def _execute_existing_server_document(
        self, worker_env: object, pages: List[PageImage]
    ) -> HtrResult:
        self._validate_existing_document_config(worker_env)

        username = getattr(worker_env, "transkribus_username", None) or ""
        password = getattr(worker_env, "transkribus_password", None) or ""
        bearer = getattr(worker_env, "transkribus_api_token", None) or ""
        collection_id = getattr(worker_env, "transkribus_collection_id", None) or ""
        model_id = getattr(worker_env, "transkribus_model_id", None) or ""
        doc_id = getattr(worker_env, "transkribus_dev_existing_document_id", None) or ""
        pages_query = getattr(worker_env, "transkribus_dev_existing_pages", None) or ""

        try:
            text, review_reasons = tr.transcribe_existing_server_document(
                username=username,
                password=password,
                bearer_token=bearer,
                collection_id=collection_id,
                model_id=model_id,
                dev_document_id=doc_id,
                dev_pages_query=pages_query,
            )
        except tr.TranskribusRetryableError as exc:
            raise EngineRetryableError(str(exc)) from exc
        except tr.TranskribusPermanentError as exc:
            raise EnginePermanentError(str(exc)) from exc

        needs_review = bool(review_reasons)
        return HtrResult(
            text=text,
            needs_review=needs_review,
            engine_name=f"transkribus-pylaia:{model_id}",
            review_reasons=list(review_reasons),
        )

    def _execute_dev_upload(
        self, worker_env: object, pages: List[PageImage]
    ) -> HtrResult:
        self._validate_upload_dev_config(worker_env)

        username = getattr(worker_env, "transkribus_username", None) or ""
        password = getattr(worker_env, "transkribus_password", None) or ""
        bearer = getattr(worker_env, "transkribus_api_token", None) or ""
        collection_id = getattr(worker_env, "transkribus_collection_id", None) or ""
        model_id = getattr(worker_env, "transkribus_model_id", None) or ""

        try:
            return tr.upload_then_transcribe_page_images_with_pylaia(
                username=username,
                password=password,
                bearer_token=bearer,
                collection_id=collection_id,
                model_id=model_id,
                pages=pages,
            )
        except tr.TranskribusRetryableError as exc:
            raise EngineRetryableError(str(exc)) from exc
        except tr.TranskribusPermanentError as exc:
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
