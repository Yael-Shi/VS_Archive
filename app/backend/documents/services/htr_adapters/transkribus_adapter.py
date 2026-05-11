from __future__ import annotations

from typing import List, Optional

from documents.services import transkribus_engine as tr
from documents.services.htr_adapters.base import (
    EnginePermanentError,
    EngineRetryableError,
    HtrResult,
)
from documents.services.page_extraction import PageImage

_EXISTING_DOC_GATE_MESSAGE = (
    "Transkribus existing-server-document mode is disabled. "
    "Set TRANSKRIBUS_USE_EXISTING_SERVER_DOCUMENT=true for dev/demo only. "
    "VS-Archive upload to Transkribus and production routing are deferred (PR #3+)."
)


class TranskribusAdapter:
    """
    Transkribus Legacy TrpServer / PyLaia adapter (PR #2).

    When ``TRANSKRIBUS_USE_EXISTING_SERVER_DOCUMENT`` is false (default), fails fast
    before any HTTP. Full upload from ``PageImage[]`` is not implemented in this PR.
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
        if not use_existing:
            raise EnginePermanentError(_EXISTING_DOC_GATE_MESSAGE)

        if not pages:
            raise EnginePermanentError(
                "TranskribusAdapter requires at least one PageImage for validation. "
                "Server-side page numbers for PyLaia come from TRANSKRIBUS_DEV_EXISTING_PAGES, "
                "not from PageImage.page_index (PR #2)."
            )

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
