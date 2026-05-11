from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

import requests

from documents.services.page_extraction import PageImage

TRP_REST_BASE = "https://transkribus.eu/TrpServer/rest"
_TRP_SESSION_COOKIE_DOMAIN = urlparse(TRP_REST_BASE).hostname or "transkribus.eu"
_TRP_SESSION_COOKIE_NAME = "JSESSIONID"
DEFAULT_HTTP_TIMEOUT_SEC = 60
POLL_INTERVAL_SEC = 2.0
POLL_MAX_WAIT_SEC = 900.0

PAGE_XML_NS = "http://schema.primaresearch.org/PAGE/gts/pagecontent/2013-07-15"


class TranskribusPermanentError(Exception):
    """Non-retryable Transkribus client failure (mapped to EnginePermanentError in adapter)."""


class TranskribusRetryableError(Exception):
    """Retryable Transkribus client failure (mapped to EngineRetryableError in adapter)."""


def _http_permanent(message: str) -> TranskribusPermanentError:
    return TranskribusPermanentError(message)


def _http_retryable(message: str) -> TranskribusRetryableError:
    return TranskribusRetryableError(message)


def _check_response_status(resp: requests.Response, context: str) -> None:
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        code = resp.status_code
        if code in (429, 502, 503, 504):
            raise _http_retryable(f"{context}: HTTP {code}") from exc
        raise _http_permanent(f"{context}: HTTP {code}") from exc


def _session_request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    context: str,
    **kwargs: Any,
) -> requests.Response:
    try:
        resp = session.request(method, url, **kwargs)
    except requests.Timeout as exc:
        raise _http_retryable(f"{context}: request timed out") from exc
    except requests.ConnectionError as exc:
        raise _http_retryable(f"{context}: connection failed") from exc
    except requests.RequestException as exc:
        raise _http_retryable(f"{context}: request failed ({type(exc).__name__})") from exc
    _check_response_status(resp, context)
    return resp


def _bare_request(
    method: str,
    url: str,
    *,
    context: str,
    **kwargs: Any,
) -> requests.Response:
    try:
        resp = requests.request(method, url, **kwargs)
    except requests.Timeout as exc:
        raise _http_retryable(f"{context}: request timed out") from exc
    except requests.ConnectionError as exc:
        raise _http_retryable(f"{context}: connection failed") from exc
    except requests.RequestException as exc:
        raise _http_retryable(f"{context}: request failed ({type(exc).__name__})") from exc
    _check_response_status(resp, context)
    return resp


def _extract_session_id_from_login_xml(xml_text: str) -> Optional[str]:
    """Return first non-empty sessionId element text, or None. Never log this value."""
    if not xml_text or not xml_text.strip():
        return None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    for el in root.iter():
        tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if tag == "sessionId" and el.text:
            stripped = el.text.strip()
            if stripped:
                return stripped
    return None


def _establish_trp_session_after_login(
    session: requests.Session, login_response_text: str
) -> None:
    """
    Ensure the session can authenticate follow-up TrpServer requests.

    Prefer Set-Cookie from the login response. If none, apply sessionId from XML
    as JSESSIONID (TrpServer convention) without logging that value.
    """
    if len(session.cookies) > 0:
        return
    session_id = _extract_session_id_from_login_xml(login_response_text)
    if session_id:
        session.cookies.set(
            _TRP_SESSION_COOKIE_NAME,
            session_id,
            domain=_TRP_SESSION_COOKIE_DOMAIN,
            path="/",
        )
        return
    raise _http_permanent(
        "Transkribus login succeeded but did not establish a usable session "
        "(no session cookie and no sessionId in response)."
    )


@dataclass(frozen=True)
class TrpPageMetadata:
    """One element of the JSON array from GET /collections/{colId}/{docId}/pages."""

    page_nr: Optional[int]
    page_id: Optional[int]
    doc_id: Optional[int]
    page_url: Optional[str]
    transcripts: List[dict]

    @classmethod
    def from_item(cls, item: dict) -> TrpPageMetadata:
        ts_list = item.get("tsList") or {}
        raw_transcripts = ts_list.get("transcripts")
        if isinstance(raw_transcripts, list):
            transcripts = [t for t in raw_transcripts if isinstance(t, dict)]
        else:
            transcripts = []
        return cls(
            page_nr=item.get("pageNr"),
            page_id=item.get("pageId"),
            doc_id=item.get("docId"),
            page_url=item.get("url"),
            transcripts=transcripts,
        )


def login_trp_server(
    session: requests.Session,
    *,
    username: str,
    password: str,
    timeout_sec: float = DEFAULT_HTTP_TIMEOUT_SEC,
) -> None:
    url = f"{TRP_REST_BASE}/auth/login"
    resp = _session_request(
        session,
        "POST",
        url,
        context="Transkribus login",
        data={"user": username, "pw": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=timeout_sec,
    )
    _establish_trp_session_after_login(session, resp.text or "")


def start_pylaia_recognition(
    session: requests.Session,
    *,
    collection_id: str,
    model_id: str,
    document_id: str,
    pages_query: str,
    timeout_sec: float = DEFAULT_HTTP_TIMEOUT_SEC,
) -> str:
    url = f"{TRP_REST_BASE}/pylaia/{collection_id}/{model_id}/recognition"
    params: dict[str, Any] = {
        "id": document_id,
        "pages": pages_query,
        "credits": "USER_ONLY",
        "writeKwsIndex": "false",
        "clearLines": "false",
        "doWordSeg": "false",
        "useExistingLinePolygons": "false",
        "doLinePolygonSimplification": "true",
        "languageModel": "",
    }
    resp = _session_request(
        session,
        "POST",
        url,
        context="Transkribus PyLaia recognition start",
        params=params,
        data="",
        headers={
            "Content-Type": "text/plain; charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
        },
        timeout=timeout_sec,
    )
    job_id = (resp.text or "").strip()
    if not job_id:
        raise _http_permanent("Transkribus PyLaia start returned empty job id")
    return job_id


def get_job(
    session: requests.Session,
    job_id: str,
    *,
    timeout_sec: float = DEFAULT_HTTP_TIMEOUT_SEC,
) -> dict:
    url = f"{TRP_REST_BASE}/jobs/{job_id}"
    resp = _session_request(
        session,
        "GET",
        url,
        context="Transkribus get job",
        timeout=timeout_sec,
    )
    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        raise _http_permanent("Transkribus job response is not JSON") from exc
    if not isinstance(data, dict):
        raise _http_permanent("Transkribus job JSON is not an object")
    return data


def _job_terminal_success(job: dict) -> bool:
    return job.get("success") is True


def _job_terminal_failure(job: dict) -> bool:
    if job.get("success") is True:
        return False
    state = (job.get("state") or "").upper()
    failed_states = {"FAILED", "ERROR", "CANCELLED"}
    if state in failed_states:
        return True
    if job.get("success") is False and state in {"DONE", "FINISHED", "COMPLETED"}:
        return True
    errors = job.get("nrOfErrors")
    if isinstance(errors, int) and errors > 0 and state not in {
        "",
        "CREATED",
        "RUNNING",
        "WAITING",
    }:
        return True
    return False


def poll_job_until_done(
    session: requests.Session,
    job_id: str,
    *,
    poll_interval_sec: float = POLL_INTERVAL_SEC,
    max_wait_sec: float = POLL_MAX_WAIT_SEC,
    timeout_sec: float = DEFAULT_HTTP_TIMEOUT_SEC,
) -> dict:
    deadline = time.monotonic() + max_wait_sec
    while time.monotonic() < deadline:
        job = get_job(session, job_id, timeout_sec=timeout_sec)
        if _job_terminal_success(job):
            return job
        if _job_terminal_failure(job):
            desc = job.get("description") or job.get("state") or "unknown"
            raise _http_permanent(
                f"Transkribus job {job_id} failed: {desc}"
            )
        time.sleep(poll_interval_sec)
    raise _http_retryable(
        f"Transkribus job {job_id} polling exceeded {max_wait_sec}s"
    )


def fetch_pages_metadata(
    session: requests.Session,
    *,
    collection_id: str,
    document_id: str,
    pages_query: str,
    timeout_sec: float = DEFAULT_HTTP_TIMEOUT_SEC,
) -> List[TrpPageMetadata]:
    url = f"{TRP_REST_BASE}/collections/{collection_id}/{document_id}/pages"
    resp = _session_request(
        session,
        "GET",
        url,
        context="Transkribus fetch pages metadata",
        params={"pages": pages_query},
        timeout=timeout_sec,
    )
    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        raise _http_permanent("Transkribus pages response is not JSON") from exc
    if not isinstance(data, list):
        raise _http_permanent("Transkribus pages JSON is not an array")
    return [TrpPageMetadata.from_item(item) for item in data if isinstance(item, dict)]


# --- Legacy TrpServer /uploads (document ingest) — PR #3 engine helpers only -----------------
# Contract sources: official upload article (JSON descriptor, uploadId, PUT multipart img/xml,
# jobId after last PUT, poll GET /jobs/{id}) and WADL resources under /uploads.
# docId extraction is provisional (see parse_doc_id_from_successful_trp_job) until an
# authenticated redacted job response confirms the shape for VS-Archive’s TrpServer usage.


def build_document_upload_descriptor_json(
    pages: List[PageImage],
    *,
    title: Optional[str] = None,
) -> dict[str, Any]:
    """
    Build the JSON body for POST /uploads?collId=… (createUploadDocStructure).

    Pages are emitted in **stable ascending order by PageImage.page_index** (sorted copy;
    input list order is ignored).

    Omits pageXmlName and checksum fields so each page can be satisfied with an **img**-only
    multipart PUT (per Transkribus REST upload documentation).
    """
    if not pages:
        raise _http_permanent("Transkribus upload descriptor requires at least one PageImage")
    sorted_pages = sorted(pages, key=lambda p: p.page_index)
    seen: set[int] = set()
    page_entries: List[dict[str, Any]] = []
    for pi in sorted_pages:
        if pi.page_index in seen:
            raise _http_permanent(
                f"Duplicate PageImage.page_index in upload batch: {pi.page_index}"
            )
        seen.add(pi.page_index)
        file_name = f"vs_archive_p{pi.page_index:06d}.png"
        page_entries.append({"fileName": file_name, "pageNr": int(pi.page_index)})
    body: dict[str, Any] = {"pageList": {"pages": page_entries}}
    if title is not None and str(title).strip():
        body["md"] = {"title": str(title).strip()}
    return body


def parse_upload_create_json_upload_id(payload: dict[str, Any]) -> int:
    """Parse uploadId from POST /uploads JSON response (narrow: top-level int only)."""
    raw = payload.get("uploadId")
    if isinstance(raw, bool) or raw is None:
        raise _http_permanent("Transkribus upload create response missing uploadId")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    raise _http_permanent("Transkribus upload create response has non-integer uploadId")


def create_trp_upload_doc_structure(
    session: requests.Session,
    *,
    collection_id: str,
    descriptor: Mapping[str, Any],
    timeout_sec: float = DEFAULT_HTTP_TIMEOUT_SEC,
) -> int:
    """
    POST /uploads?collId=… with application/json body; returns uploadId.
    """
    try:
        coll_int = int(str(collection_id).strip())
    except ValueError as exc:
        raise _http_permanent(
            f"Transkribus upload requires numeric collId, got {collection_id!r}"
        ) from exc
    url = f"{TRP_REST_BASE}/uploads"
    resp = _session_request(
        session,
        "POST",
        url,
        context="Transkribus upload create structure",
        params={"collId": coll_int},
        json=dict(descriptor),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=timeout_sec,
    )
    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        raise _http_permanent(
            "Transkribus upload create response is not JSON (expected application/json)"
        ) from exc
    if not isinstance(data, dict):
        raise _http_permanent("Transkribus upload create JSON is not an object")
    return parse_upload_create_json_upload_id(data)


def put_trp_upload_page_image_only(
    session: requests.Session,
    upload_id: int,
    *,
    file_name: str,
    image_bytes: bytes,
    timeout_sec: float = DEFAULT_HTTP_TIMEOUT_SEC,
) -> Optional[str]:
    """
    PUT /uploads/{uploadId} with multipart form: single part **img** (image/png octet-stream).

    Per REST upload docs, **xml** is required only when pageXmlName was set on that page in
    the descriptor; this helper is for img-only pages.

    Returns **jobId** string when the JSON body includes it (typically the final page’s PUT
    after ingest starts); otherwise None.

    Callers and future edits must **not** log image bytes, caller-supplied filenames that may
    carry sensitive metadata, cookies, session ids, tokens, usernames, or passwords. This
    implementation performs no logging of request bodies or auth material.
    """
    url = f"{TRP_REST_BASE}/uploads/{int(upload_id)}"
    files = {
        "img": (
            file_name,
            BytesIO(image_bytes),
            "application/octet-stream",
        ),
    }
    resp = _session_request(
        session,
        "PUT",
        url,
        context="Transkribus upload PUT page",
        files=files,
        timeout=timeout_sec,
    )
    return parse_upload_put_json_job_id_if_present(resp)


def parse_upload_put_json_job_id_if_present(resp: requests.Response) -> Optional[str]:
    """If PUT body is non-empty JSON object with jobId, return it as str; else None."""
    text = (resp.text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise _http_permanent("Transkribus upload PUT returned non-JSON body")
    if not isinstance(data, dict):
        raise _http_permanent("Transkribus upload PUT JSON is not an object")
    jid = data.get("jobId")
    if jid is None:
        return None
    s = str(jid).strip()
    return s if s else None


def parse_doc_id_from_successful_trp_job(job: dict[str, Any]) -> str:
    """
    Read TrpServer document id from a **successful** GET /jobs/{jobId} payload.

    **Provisional / narrow:** reads **top-level** ``docId`` only (int or non-empty string).
    This is a working hypothesis for ingest jobs; it is **not** proven for the project’s
    Transkribus account or job types until **Yael** supplies an **authenticated redacted**
    successful job JSON—then either confirm this field or adjust the parser narrowly.

    Third-party client docs (e.g. TranskribusPyClient ``getJobStatus`` key lists) are hints
    only, not a guarantee of server behavior for this deployment.
    """
    if not _job_terminal_success(job):
        raise _http_permanent(
            "Transkribus job is not in terminal success state; cannot read docId"
        )
    raw = job.get("docId")
    if isinstance(raw, bool) or raw is None:
        raise _http_permanent(
            "Transkribus ingest job JSON missing top-level docId (provisional parser); "
            "provide a redacted successful job payload to confirm or adjust the field path."
        )
    if isinstance(raw, int):
        return str(raw)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    raise _http_permanent("Transkribus job docId is not a usable int or string")


def strict_map_page_index_to_trp_page_nr(
    page_images: List[PageImage],
    pages_meta: List[TrpPageMetadata],
) -> Dict[int, int]:
    """
    Pair VS-Archive pages with TrpServer page metadata by **sorted order** (page_index vs pageNr).

    Does not require page_index == pageNr; use when counts match and ordering is stable.
    """
    sorted_imgs = sorted(page_images, key=lambda p: p.page_index)
    sorted_meta = sorted(
        pages_meta,
        key=lambda m: (m.page_nr is None, m.page_nr if m.page_nr is not None else 0),
    )
    if len(sorted_imgs) != len(sorted_meta):
        raise _http_permanent(
            f"Transkribus page count mismatch: {len(sorted_imgs)} PageImage vs "
            f"{len(sorted_meta)} server pages"
        )
    out: Dict[int, int] = {}
    for img, meta in zip(sorted_imgs, sorted_meta, strict=True):
        if meta.page_nr is None:
            raise _http_permanent("Transkribus page metadata missing pageNr for mapping")
        out[img.page_index] = int(meta.page_nr)
    return out


def format_trp_pages_query_from_page_nrs(page_nrs: List[int]) -> str:
    """Build `pages=` query value for GET …/pages or PyLaia from a list of Transkribus pageNr."""
    uniq = sorted({int(p) for p in page_nrs})
    if not uniq:
        raise _http_permanent("Cannot format empty Transkribus pages query")
    if len(uniq) == 1:
        return str(uniq[0])
    lo, hi = uniq[0], uniq[-1]
    if uniq == list(range(lo, hi + 1)):
        return f"{lo}-{hi}"
    return ",".join(str(p) for p in uniq)


def _transcript_newest_rank(t: dict) -> Tuple[int, int]:
    """
    Sort key for "newest first": higher tuple = newer.
    Primary: timestamp-like fields (descending when using max()).
    Secondary: tsId (descending).
    """
    raw_ts = (
        t.get("timestamp")
        or t.get("time")
        or t.get("created")
        or t.get("uploadTimestamp")
    )
    ts_val = 0
    if isinstance(raw_ts, (int, float)):
        ts_val = int(raw_ts)
    elif isinstance(raw_ts, str):
        raw_ts = raw_ts.strip()
        if raw_ts.isdigit():
            ts_val = int(raw_ts)
    tid_raw = t.get("tsId")
    try:
        tid_val = int(tid_raw) if tid_raw is not None else 0
    except (TypeError, ValueError):
        tid_val = 0
    return (ts_val, tid_val)


def _select_newest_transcript(candidates: List[dict]) -> dict:
    return max(candidates, key=_transcript_newest_rank)


def pick_transcript(
    transcripts: List[dict],
    *,
    job_id: str,
    model_id: str,
) -> Optional[dict]:
    jid = str(job_id)
    mid = str(model_id)

    by_job_and_model = [
        t
        for t in transcripts
        if str(t.get("jobId", "")) == jid and str(t.get("modelId", "")) == mid
    ]
    if by_job_and_model:
        return _select_newest_transcript(by_job_and_model)

    by_job = [t for t in transcripts if str(t.get("jobId", "")) == jid]
    if by_job:
        return _select_newest_transcript(by_job)

    by_model = [t for t in transcripts if str(t.get("modelId", "")) == mid]
    if by_model:
        return _select_newest_transcript(by_model)

    return None


def ordered_transcript_urls(
    pages_meta: List[TrpPageMetadata],
    *,
    job_id: str,
    model_id: str,
) -> List[Tuple[Optional[int], str]]:
    sorted_pages = sorted(
        pages_meta,
        key=lambda p: (p.page_nr is None, p.page_nr if p.page_nr is not None else 0),
    )
    out: List[Tuple[Optional[int], str]] = []
    for pm in sorted_pages:
        chosen = pick_transcript(pm.transcripts, job_id=job_id, model_id=model_id)
        if chosen is None:
            raise _http_permanent(
                f"No transcript matched job/model for page {pm.page_nr}"
            )
        url = chosen.get("url")
        if not url or not isinstance(url, str):
            raise _http_permanent(
                f"Transcript URL missing for page {pm.page_nr}"
            )
        out.append((pm.page_nr, url))
    return out


def fetch_transcript_xml(
    transcript_url: str,
    *,
    bearer_token: str,
    timeout_sec: float = DEFAULT_HTTP_TIMEOUT_SEC,
) -> bytes:
    resp = _bare_request(
        "GET",
        transcript_url,
        context="Transkribus transcript fetch",
        headers={"Authorization": f"Bearer {bearer_token}"},
        timeout=timeout_sec,
    )
    return resp.content


def parse_page_xml_to_text(page_xml: bytes) -> str:
    try:
        root = ET.fromstring(page_xml)
    except ET.ParseError as exc:
        raise _http_permanent("Invalid PAGE XML from transcript") from exc

    ns = {"pc": PAGE_XML_NS}
    lines: List[str] = []
    for text_line in root.findall(".//pc:TextLine", ns):
        unicode_el = text_line.find("pc:TextEquiv/pc:Unicode", ns)
        if unicode_el is not None and unicode_el.text:
            lines.append(unicode_el.text)
        else:
            unicode_el = text_line.find("TextEquiv/Unicode")
            if unicode_el is not None and unicode_el.text:
                lines.append(unicode_el.text)
    return "\n".join(lines)


def transcribe_existing_server_document(
    *,
    username: str,
    password: str,
    bearer_token: str,
    collection_id: str,
    model_id: str,
    dev_document_id: str,
    dev_pages_query: str,
) -> Tuple[str, List[str]]:
    """
    Full PyLaia flow for a document that already exists on Transkribus (PR #2 dev/demo path).

    Returns (plain text, review_reasons).
    """
    session = requests.Session()
    login_trp_server(session, username=username, password=password)
    job_id = start_pylaia_recognition(
        session,
        collection_id=collection_id,
        model_id=model_id,
        document_id=dev_document_id,
        pages_query=dev_pages_query,
    )
    poll_job_until_done(session, job_id)
    pages_meta = fetch_pages_metadata(
        session,
        collection_id=collection_id,
        document_id=dev_document_id,
        pages_query=dev_pages_query,
    )
    if not pages_meta:
        raise _http_permanent("Transkribus pages metadata returned empty list")

    pairs = ordered_transcript_urls(
        pages_meta,
        job_id=job_id,
        model_id=model_id,
    )
    page_texts: List[str] = []
    review_reasons: List[str] = []
    for _page_nr, t_url in pairs:
        xml_bytes = fetch_transcript_xml(t_url, bearer_token=bearer_token)
        text = parse_page_xml_to_text(xml_bytes)
        if not text.strip():
            review_reasons.append("EMPTY_TRANSCRIPT_PAGE")
        page_texts.append(text)

    full_text = "\n\n".join(page_texts)
    return full_text, review_reasons
