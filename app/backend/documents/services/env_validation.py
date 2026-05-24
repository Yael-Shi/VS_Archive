from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional


class EnvConfigError(RuntimeError):
    """Raised when required environment variables are missing/invalid."""


_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


def _get(name: str) -> Optional[str]:
    v = os.getenv(name)
    if v is None:
        return None
    v = v.strip()
    return v if v else None


def _require(name: str) -> str:
    v = _get(name)
    if v is None:
        raise EnvConfigError(f"Missing required env var: {name}")
    return v


def _require_int(
    name: str, *, min_value: Optional[int] = None, max_value: Optional[int] = None
) -> int:
    raw = _require(name)
    try:
        val = int(raw)
    except ValueError as e:
        raise EnvConfigError(f"Env var {name} must be an int. Got: {raw!r}") from e

    if min_value is not None and val < min_value:
        raise EnvConfigError(f"Env var {name} must be >= {min_value}. Got: {val}")
    if max_value is not None and val > max_value:
        raise EnvConfigError(f"Env var {name} must be <= {max_value}. Got: {val}")
    return val


def _require_float(
    name: str, *, min_value: Optional[float] = None, max_value: Optional[float] = None
) -> float:
    raw = _require(name)
    try:
        val = float(raw)
    except ValueError as e:
        raise EnvConfigError(f"Env var {name} must be a float. Got: {raw!r}") from e

    if min_value is not None and val < min_value:
        raise EnvConfigError(f"Env var {name} must be >= {min_value}. Got: {val}")
    if max_value is not None and val > max_value:
        raise EnvConfigError(f"Env var {name} must be <= {max_value}. Got: {val}")
    return val


def _get_int(
    name: str,
    *,
    default: Optional[int] = None,
    min_value: Optional[int] = None,
    max_value: Optional[int] = None,
) -> Optional[int]:
    raw = _get(name)
    if raw is None:
        return default
    try:
        val = int(raw)
    except ValueError as e:
        raise EnvConfigError(f"Env var {name} must be an int. Got: {raw!r}") from e

    if min_value is not None and val < min_value:
        raise EnvConfigError(f"Env var {name} must be >= {min_value}. Got: {val}")
    if max_value is not None and val > max_value:
        raise EnvConfigError(f"Env var {name} must be <= {max_value}. Got: {val}")
    return val


def _get_float(
    name: str,
    *,
    default: Optional[float] = None,
    min_value: Optional[float] = None,
    max_value: Optional[float] = None,
) -> Optional[float]:
    raw = _get(name)
    if raw is None:
        return default
    try:
        val = float(raw)
    except ValueError as e:
        raise EnvConfigError(f"Env var {name} must be a float. Got: {raw!r}") from e

    if min_value is not None and val < min_value:
        raise EnvConfigError(f"Env var {name} must be >= {min_value}. Got: {val}")
    if max_value is not None and val > max_value:
        raise EnvConfigError(f"Env var {name} must be <= {max_value}. Got: {val}")
    return val


def _require_time_hhmm(name: str) -> str:
    v = _require(name)
    if not _TIME_RE.match(v):
        raise EnvConfigError(f"Env var {name} must be HH:MM (24h). Got: {v!r}")
    hh, mm = v.split(":")
    h = int(hh)
    m = int(mm)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise EnvConfigError(f"Env var {name} must be valid time HH:MM. Got: {v!r}")
    return v


def _get_bool(name: str, *, default: bool = False) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in ("1", "true", "yes", "y", "on"):
        return True
    if v in ("0", "false", "no", "n", "off"):
        return False
    raise EnvConfigError(
        f"Env var {name} must be a boolean (true/false). Got: {raw!r}"
    )


@dataclass(frozen=True)
class WorkerEnvConfig:
    gemini_api_key: str
    gemini_confidence_threshold: float
    min_text_length: int
    max_retries: int
    retry_delay_seconds_1: int
    retry_delay_seconds_2: int

    report_window_start: str
    report_send_time: str

    free_tier_alert_pct: int
    gemini_free_daily_request_limit: int
    gemini_free_daily_image_limit: int
    transkribus_free_monthly_credits: int

    enable_hybrid_htr: bool
    enable_daily_report: bool

    # Optional when daily report is disabled
    smtp_host: Optional[str]
    smtp_port: Optional[int]
    smtp_username: Optional[str]
    smtp_password: Optional[str]
    default_from_email: Optional[str]

    # Transkribus credentials
    transkribus_api_token: Optional[str]
    transkribus_username: Optional[str]
    transkribus_password: Optional[str]

    # Gemini hardening
    gemini_temperature: float
    gemini_top_k: int
    gemini_top_p: float
    gemini_max_output_tokens: Optional[int]
    gemini_double_pass: bool
    gemini_consistency_min_ratio: float

    # Transkribus PR #2 (optional; validated in TranskribusAdapter when gate is on)
    transkribus_use_existing_server_document: bool = field(default=False)
    transkribus_dev_upload_mode: bool = field(default=False)
    transkribus_dev_existing_document_id: Optional[str] = field(default=None)
    transkribus_collection_id: Optional[str] = field(default=None)
    transkribus_model_id: Optional[str] = field(default=None)
    transkribus_dev_existing_pages: Optional[str] = field(default=None)
    transkribus_force_reprocess: bool = field(default=False)
    transkribus_recognition_only_retry: bool = field(default=False)


def validate_required_env() -> WorkerEnvConfig:
    enable_hybrid_htr = _get_bool("ENABLE_HYBRID_HTR", default=False)
    enable_daily_report = _get_bool("ENABLE_DAILY_REPORT", default=False)

    gemini_api_key = _require("GEMINI_API_KEY")
    gemini_confidence_threshold = _get_float(
        "GEMINI_CONFIDENCE_THRESHOLD", default=0.7, min_value=0.0, max_value=1.0
    )
    
    min_text_length = _get_int("MIN_TEXT_LENGTH", default=20, min_value=0)

    max_retries = _get_int("MAX_RETRIES", default=3, min_value=0)
    retry_delay_seconds_1 = _get_int("RETRY_DELAY_SECONDS_1", default=30, min_value=0)
    retry_delay_seconds_2 = _get_int("RETRY_DELAY_SECONDS_2", default=300, min_value=0)

    report_window_start = _get("REPORT_WINDOW_START") or "00:00"
    report_send_time = _get("REPORT_SEND_TIME") or "08:00"

    free_tier_alert_pct = _get_int("FREE_TIER_ALERT_PCT", default=80, min_value=1, max_value=100)
    gemini_free_daily_request_limit = _get_int("GEMINI_FREE_DAILY_REQUEST_LIMIT", default=1500)
    gemini_free_daily_image_limit = _get_int("GEMINI_FREE_DAILY_IMAGE_LIMIT", default=1000)
    transkribus_free_monthly_credits = _get_int("TRANSKRIBUS_FREE_MONTHLY_CREDITS", default=500)

    # --- Gemini Hardening (Defaults adjusted for HTR) ---
    gemini_temperature = _get_float("GEMINI_TEMPERATURE", default=0.2, min_value=0.0, max_value=2.0)
    gemini_top_k = _get_int("GEMINI_TOP_K", default=40, min_value=1, max_value=64)
    gemini_top_p = _get_float("GEMINI_TOP_P", default=0.95, min_value=0.0, max_value=1.0)
    gemini_max_output_tokens = _get_int("GEMINI_MAX_OUTPUT_TOKENS", default=2048)
    
    # For handwriting, False as the default to avoid disqualifications for minor inconsistencies
    gemini_double_pass = _get_bool("GEMINI_DOUBLE_PASS", default=False)
    gemini_consistency_min_ratio = _get_float("GEMINI_CONSISTENCY_MIN_RATIO", default=0.7)

    # --- Email & Transkribus (Keep original logic) ---
    smtp_host = _get("SMTP_HOST")
    smtp_port_raw = _get("SMTP_PORT")
    smtp_port = int(smtp_port_raw) if smtp_port_raw and smtp_port_raw.isdigit() else None
    smtp_username = _get("SMTP_USERNAME")
    smtp_password = _get("SMTP_PASSWORD")
    default_from_email = _get("DEFAULT_FROM_EMAIL")

    if enable_daily_report:
        if not all([smtp_host, smtp_port, smtp_username, smtp_password, default_from_email]):
            raise EnvConfigError("ENABLE_DAILY_REPORT is true but SMTP vars are missing.")

    transkribus_api_token = _get("TRANSKRIBUS_API_TOKEN")
    transkribus_username = _get("TRANSKRIBUS_USERNAME")
    transkribus_password = _get("TRANSKRIBUS_PASSWORD")

    transkribus_use_existing_server_document = _get_bool(
        "TRANSKRIBUS_USE_EXISTING_SERVER_DOCUMENT", default=False
    )
    transkribus_dev_upload_mode = _get_bool("TRANSKRIBUS_DEV_UPLOAD_MODE", default=False)
    transkribus_dev_existing_document_id = _get("TRANSKRIBUS_DEV_EXISTING_DOCUMENT_ID")
    transkribus_collection_id = _get("TRANSKRIBUS_COLLECTION_ID")
    transkribus_model_id = _get("TRANSKRIBUS_MODEL_ID")
    transkribus_dev_existing_pages = _get("TRANSKRIBUS_DEV_EXISTING_PAGES")
    transkribus_force_reprocess = _get_bool("TRANSKRIBUS_FORCE_REPROCESS", default=False)
    transkribus_recognition_only_retry = _get_bool(
        "TRANSKRIBUS_RECOGNITION_ONLY_RETRY", default=False
    )

    if enable_hybrid_htr and not (transkribus_api_token or (transkribus_username and transkribus_password)):
        raise EnvConfigError("ENABLE_HYBRID_HTR is true but Transkribus credentials missing.")

    return WorkerEnvConfig(
        gemini_api_key=gemini_api_key,
        gemini_confidence_threshold=float(gemini_confidence_threshold),
        min_text_length=int(min_text_length),
        max_retries=int(max_retries),
        retry_delay_seconds_1=int(retry_delay_seconds_1),
        retry_delay_seconds_2=int(retry_delay_seconds_2),
        report_window_start=report_window_start,
        report_send_time=report_send_time,
        free_tier_alert_pct=int(free_tier_alert_pct),
        gemini_free_daily_request_limit=int(gemini_free_daily_request_limit),
        gemini_free_daily_image_limit=int(gemini_free_daily_image_limit),
        transkribus_free_monthly_credits=int(transkribus_free_monthly_credits),
        enable_hybrid_htr=enable_hybrid_htr,
        enable_daily_report=enable_daily_report,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_username=smtp_username,
        smtp_password=smtp_password,
        default_from_email=default_from_email,
        transkribus_api_token=transkribus_api_token,
        transkribus_username=transkribus_username,
        transkribus_password=transkribus_password,
        gemini_temperature=float(gemini_temperature),
        gemini_top_k=int(gemini_top_k),
        gemini_top_p=float(gemini_top_p),
        gemini_max_output_tokens=gemini_max_output_tokens,
        gemini_double_pass=gemini_double_pass,
        gemini_consistency_min_ratio=float(gemini_consistency_min_ratio),
        transkribus_use_existing_server_document=transkribus_use_existing_server_document,
        transkribus_dev_upload_mode=transkribus_dev_upload_mode,
        transkribus_dev_existing_document_id=transkribus_dev_existing_document_id,
        transkribus_collection_id=transkribus_collection_id,
        transkribus_model_id=transkribus_model_id,
        transkribus_dev_existing_pages=transkribus_dev_existing_pages,
        transkribus_force_reprocess=transkribus_force_reprocess,
        transkribus_recognition_only_retry=transkribus_recognition_only_retry,
    )
