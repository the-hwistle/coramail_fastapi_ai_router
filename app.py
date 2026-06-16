from __future__ import annotations

import atexit
import hashlib
import importlib
import json
import mimetypes
import os
import re
import subprocess
import sys
import textwrap
import time
from collections import deque
from dataclasses import asdict, is_dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import Event, Lock, RLock, Thread
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from coramail_db import CoramailDB

APP_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_PATH = APP_DIR / ".env"
STATIC_DIR = APP_DIR / "static"
TEMPLATES_DIR = APP_DIR / "templates"


def current_asset_version() -> str:
    paths = [APP_DIR / "app.py", APP_DIR / "coramail_db.py", STATIC_DIR / "app.css"]
    paths.extend(TEMPLATES_DIR.rglob("*.html"))
    latest_mtime_ns = max(path.stat().st_mtime_ns for path in paths if path.exists())
    return str(latest_mtime_ns)


def load_dotenv_file(env_path: str | Path | None = None) -> Path:
    path = Path(env_path or os.getenv("CORAMAIL_ENV_FILE") or DEFAULT_ENV_PATH).expanduser().resolve()
    if not path.exists():
        return path
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return path


load_dotenv_file()
RUN_DIR = APP_DIR / "runs"
PIPELINE_DIR = Path(os.getenv("CORAMAIL_PIPELINE_DIR", str(APP_DIR / "pipeline"))).resolve()
ATTACHMENTS_DIR = (PIPELINE_DIR / "attachments").resolve()

if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

try:
    from llama_index.embeddings.ollama import OllamaEmbedding
    from llama_index.llms.ollama import Ollama
    from qdrant_client import QdrantClient

    legacy_index = importlib.import_module("index")
    legacy_fetcher = importlib.import_module("gmail_postgres_fetcher")
    legacy_classifier = importlib.import_module("email_classification")
    legacy_search = importlib.import_module("qdrant_hybridsearch")
except Exception as exc:  # noqa: BLE001
    STARTUP_ERROR: str | None = str(exc)
else:
    STARTUP_ERROR = None


app = FastAPI(
    title="CoRA Mail AI Router",
    description="FastAPI app for Gmail ingestion, Qdrant RAG search, and LLM email routing.",
    version="1.0.0",
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:8501",
        "http://127.0.0.1:8502",
        "http://localhost:8501",
        "http://localhost:8502",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def disable_cache_for_ui(request: Request, call_next) -> Response:
    response = await call_next(request)
    if request.url.path == "/static/app.css" or request.url.path.startswith("/ui/") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

_model_lock = Lock()
_qdrant_lock = RLock()
_auto_sync_lock = Lock()
_auto_sync_stop = Event()
_auto_sync_thread: Thread | None = None
_auto_sync_process: subprocess.Popen[str] | None = None
_auto_sync_launching = False
_classification_queue_lock = Lock()
_classification_queue_event = Event()
_classification_stop = Event()
_classification_thread: Thread | None = None
_classification_queue: deque[str] = deque()
_classification_enqueued: set[str] = set()
_classification_runtime: dict[str, dict[str, Any]] = {}
_summary_cache_lock = Lock()
_summary_cache: dict[str, Any] = {
    "qdrant_points": {"emails": None, "attachments": None, "status": "pending"},
    "qdrant_updated_at": 0.0,
}
_mail_cache_lock = Lock()
_mail_cache: dict[str, Any] = {
    "version": "",
    "headers": None,
    "emails": None,
    "classifications": None,
    "metrics": None,
    "details": {},
}
_embed_model: Any | None = None
_llm: Any | None = None
_qdrant_client: Any | None = None


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() not in {"0", "false", "no", "off"}


def env_int(name: str, default: int, minimum: int = 1) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return max(minimum, int(value))
    except ValueError:
        return default


AUTO_SYNC_ENABLED = env_bool("CORAMAIL_AUTO_SYNC_ENABLED", True)
AUTO_SYNC_INTERVAL_SECONDS = env_int("CORAMAIL_AUTO_SYNC_INTERVAL_SECONDS", 60, minimum=10)
AUTO_SYNC_INITIAL_DELAY_SECONDS = env_int("CORAMAIL_AUTO_SYNC_INITIAL_DELAY_SECONDS", 5, minimum=0)
AUTO_SYNC_KEYWORD = os.getenv("CORAMAIL_AUTO_SYNC_KEYWORD", "").strip()
AUTO_SYNC_MAX_EMAILS = env_int("CORAMAIL_AUTO_SYNC_MAX_EMAILS", 100, minimum=1)
AUTO_SYNC_RETRIEVAL_LIMIT = env_int("CORAMAIL_AUTO_SYNC_RETRIEVAL_LIMIT", 5, minimum=1)
AUTO_SYNC_CLASSIFICATION_LIMIT = os.getenv("CORAMAIL_AUTO_SYNC_CLASSIFICATION_LIMIT")
LIST_AUTO_CLASSIFY_LIMIT = env_int("CORAMAIL_LIST_AUTO_CLASSIFY_LIMIT", 5, minimum=0)
_auto_sync_status: dict[str, Any] = {
    "enabled": AUTO_SYNC_ENABLED,
    "interval_seconds": AUTO_SYNC_INTERVAL_SECONDS,
    "search_keyword": AUTO_SYNC_KEYWORD,
    "max_emails": AUTO_SYNC_MAX_EMAILS,
    "running": False,
    "current_pid": None,
    "last_started_at": None,
    "last_completed_at": None,
    "last_exit_code": None,
    "last_log_path": None,
    "last_error": None,
    "last_skip_reason": None,
}

BUSINESS_LABEL_ROUTING = {
    "발주": ["sales", "order"],
    "문의": ["sales", "quote_request"],
    "서비스": ["service", "customer_support"],
    "기술": ["engineering", "technical"],
    "기타": ["general"],
}

ROUTE_NAMES = {
    "sales": "발주담당자",
    "quote_request": "견적담당자",
    "quote_sent": "영업담당자",
    "order": "발주담당자",
    "accounting": "회계담당자",
    "payment": "회계담당자",
    "logistics": "물류담당자",
    "delivery": "납기담당자",
    "service": "서비스담당자",
    "customer_support": "서비스담당자",
    "engineering": "기술담당자",
    "technical": "기술담당자",
    "general": "공통메일함",
}

CATEGORY_ORDER = ["발주", "문의", "서비스", "기술", "기타", "미분류"]
KOREAN_WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]
DISPLAY_TIMEZONE = ZoneInfo("Asia/Seoul")
EXECUTIVE_SUMMARY_FIELDS = (
    ("context", "핵심 맥락"),
    ("conclusion", "결론"),
    ("action", "필요 조치"),
    ("risk", "리스크/기한"),
)

LEGACY_CATEGORY_TO_BUSINESS_LABEL = {
    "order": "발주",
    "rfq": "문의",
    "quote": "문의",
    "delivery": "서비스",
    "payment": "기타",
    "technical": "기술",
    "general": "기타",
    "unclassified": "미분류",
}


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    limit: int = Field(5, ge=1, le=20)
    with_answer: bool = True


class ClassifyRequest(BaseModel):
    email_uid: str | None = None
    email_index: int | None = Field(None, ge=0)
    retrieval_limit: int = Field(5, ge=1, le=20)
    with_summary: bool = False


class FetchRequest(BaseModel):
    search_keyword: str = ""
    max_emails: int = Field(100, ge=1, le=2000)
    date_after: str | None = None
    date_before: str | None = None
    sender: str | None = None
    has_attachment: bool | None = None
    auto_process: bool = True
    classification_limit: int | None = Field(None, ge=1, le=2000)
    retrieval_limit: int = Field(5, ge=1, le=20)


def require_pipeline() -> None:
    if STARTUP_ERROR:
        raise HTTPException(status_code=500, detail=f"Pipeline import failed: {STARTUP_ERROR}")


def get_embed_model() -> Any:
    require_pipeline()
    global _embed_model
    with _model_lock:
        if _embed_model is None:
            _embed_model = OllamaEmbedding(
                model_name=legacy_index.EMBEDDING_MODEL,
                base_url=legacy_index.OLLAMA_BASE_URL,
            )
        return _embed_model


def get_llm() -> Any:
    require_pipeline()
    global _llm
    with _model_lock:
        if _llm is None:
            _llm = Ollama(
                model=legacy_index.LLM_MODEL,
                base_url=legacy_index.OLLAMA_BASE_URL,
                request_timeout=1000.0,
            )
        return _llm


def get_qdrant_client() -> Any:
    require_pipeline()
    global _qdrant_client
    with _qdrant_lock:
        if _qdrant_client is None:
            _qdrant_client = QdrantClient(path=str(legacy_index.QDRANT_PATH))
        return _qdrant_client


def close_qdrant_client() -> None:
    global _qdrant_client
    with _qdrant_lock:
        if _qdrant_client is not None:
            _qdrant_client.close()
            _qdrant_client = None


atexit.register(close_qdrant_client)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    del request
    return JSONResponse(status_code=500, content={"detail": str(exc)})


@app.get("/api")
def api_root() -> dict[str, str]:
    return {
        "service": "CoRA Mail AI Router API",
        "frontend": "FastAPI Jinja/HTMX UI is served from /",
    }


def mail_db() -> CoramailDB:
    require_pipeline()
    return CoramailDB(legacy_index.EMAIL_DATABASE_URL)


def current_mail_data_version() -> str:
    require_pipeline()
    return mail_db().data_version()


def reset_mail_cache(version: str = "") -> None:
    with _mail_cache_lock:
        _mail_cache.update(
            {
                "version": version,
                "headers": None,
                "emails": None,
                "classifications": None,
                "metrics": None,
                "details": {},
            }
        )


def ensure_mail_cache_version(version: str) -> None:
    with _mail_cache_lock:
        if _mail_cache.get("version") != version:
            _mail_cache.update(
                {
                    "version": version,
                    "headers": None,
                    "emails": None,
                    "classifications": None,
                    "metrics": None,
                    "details": {},
                }
            )


def load_email_headers(version: str | None = None) -> list[dict[str, Any]]:
    version = version or current_mail_data_version()
    ensure_mail_cache_version(version)
    with _mail_cache_lock:
        cached = _mail_cache.get("headers")
    if cached is not None:
        return cached

    headers = mail_db().load_email_headers()
    with _mail_cache_lock:
        if _mail_cache.get("version") == version:
            _mail_cache["headers"] = headers
    return headers


def load_emails(version: str | None = None) -> list[dict[str, Any]]:
    version = version or current_mail_data_version()
    ensure_mail_cache_version(version)
    with _mail_cache_lock:
        cached = _mail_cache.get("emails")
    if cached is not None:
        return cached

    emails = mail_db().load_emails()
    with _mail_cache_lock:
        if _mail_cache.get("version") == version:
            _mail_cache["emails"] = emails
    return emails


def load_email_detail(email_uid: str, version: str | None = None) -> dict[str, Any] | None:
    version = version or current_mail_data_version()
    ensure_mail_cache_version(version)
    target_uid = str(email_uid or "").strip()
    if not target_uid:
        return None

    with _mail_cache_lock:
        cached = (_mail_cache.get("details") or {}).get(target_uid)
    if cached is not None:
        return cached

    detail = mail_db().load_email_detail(target_uid)
    if detail is None:
        return None
    with _mail_cache_lock:
        if _mail_cache.get("version") == version:
            details = dict(_mail_cache.get("details") or {})
            details[target_uid] = detail
            _mail_cache["details"] = details
    return detail


def load_email_by_index(email_index: int, version: str | None = None, full: bool = False) -> dict[str, Any] | None:
    headers = load_email_headers(version=version)
    if email_index < 0 or email_index >= len(headers):
        return None
    header = headers[email_index]
    if not full:
        return header
    return load_email_detail(str(header.get("email_uid") or ""), version=version)


def load_classifications(
    emails: list[dict[str, Any]],
    version: str | None = None,
    prefer_cached_full: bool = True,
) -> dict[str, dict[str, Any]]:
    require_pipeline()
    version = version or current_mail_data_version()
    ensure_mail_cache_version(version)
    email_uids = [str(email.get("email_uid", "")) for email in emails if email.get("email_uid")]
    if not email_uids:
        return {}

    if prefer_cached_full:
        with _mail_cache_lock:
            cached = _mail_cache.get("classifications")
            cached_headers = _mail_cache.get("headers")
        cached_header_uids = {
            str(email.get("email_uid") or "")
            for email in (cached_headers or [])
            if str(email.get("email_uid") or "")
        }
        if cached is not None and cached_header_uids and cached_header_uids.issubset(set(email_uids)):
            return cached

    classifications = mail_db().load_classifications(email_uids)
    header_uids = {
        str(email.get("email_uid") or "")
        for email in load_email_headers(version=version)
        if str(email.get("email_uid") or "")
    }
    if header_uids and header_uids.issubset(set(email_uids)):
        with _mail_cache_lock:
            if _mail_cache.get("version") == version:
                _mail_cache["classifications"] = classifications
    return classifications


def load_mail_metrics(version: str | None = None) -> dict[str, int]:
    version = version or current_mail_data_version()
    ensure_mail_cache_version(version)
    with _mail_cache_lock:
        cached = _mail_cache.get("metrics")
    if cached is not None:
        return cached
    metrics = mail_db().email_metrics()
    with _mail_cache_lock:
        if _mail_cache.get("version") == version:
            _mail_cache["metrics"] = metrics
    return metrics


def save_classification(email: dict[str, Any], classification: dict[str, Any]) -> None:
    require_pipeline()
    mail_db().upsert_classification(email, classification)
    reset_mail_cache()


def classification_store_name() -> str:
    require_pipeline()
    return mail_db().classification_store_name()


def attachment_preview_kind(content_type: str, filename: str) -> str:
    mime_type = str(content_type or "").strip().lower()
    guessed_type = mimetypes.guess_type(filename or "")[0] or ""
    media_type = mime_type or guessed_type.lower()
    if media_type == "application/pdf":
        return "pdf"
    if media_type.startswith("image/"):
        return "image"
    if media_type.startswith("text/"):
        return "text"
    if (filename or "").lower().endswith((".txt", ".md", ".json", ".csv", ".xml", ".log")):
        return "text"
    return "binary"


def format_bytes(value: int | None) -> str:
    if not isinstance(value, int) or value < 0:
        return ""
    size = float(value)
    units = ["B", "KB", "MB", "GB"]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            precision = 0 if unit == "B" else 1
            return f"{size:.{precision}f} {unit}"
        size /= 1024
    return ""


def resolve_attachment_path(file_path: str) -> Path | None:
    relative_path = str(file_path or "").strip()
    if not relative_path:
        return None
    candidate = (PIPELINE_DIR / relative_path).resolve()
    try:
        candidate.relative_to(ATTACHMENTS_DIR)
    except ValueError:
        return None
    return candidate


def build_attachment_payload(attachment: dict[str, Any], email_index: int, attachment_index: int) -> dict[str, Any]:
    payload = dict(attachment)
    filename = str(payload.get("filename") or "attachment")
    resolved_path = resolve_attachment_path(str(payload.get("file_path") or ""))
    file_size = payload.get("file_size_bytes")
    if not isinstance(file_size, int) and resolved_path and resolved_path.exists():
        file_size = resolved_path.stat().st_size
    preview_kind = attachment_preview_kind(str(payload.get("content_type") or payload.get("mime_type") or ""), filename)
    payload.update(
        {
            "index": attachment_index,
            "exists": bool(resolved_path and resolved_path.exists()),
            "preview_kind": preview_kind,
            "size_label": format_bytes(file_size if isinstance(file_size, int) else None),
            "view_url": f"/api/emails/{email_index}/attachments/{attachment_index}",
            "download_url": f"/api/emails/{email_index}/attachments/{attachment_index}?download=true",
        }
    )
    return payload


def fallback_summary(email: dict[str, Any], classification: dict[str, Any]) -> str:
    subject = str(email.get("subject", "")).strip() or "제목 없음"
    sender = str(email.get("from", "")).strip() or "발신자 미상"
    category = business_label_of(classification)
    refs = ", ".join(str(ref) for ref in classification.get("business_refs", [])[:3]) or "없음"
    attachments = len(email.get("attachments", []) or [])
    route = route_label_of(classification.get("routing_labels") or [])
    return "\n".join(
        [
            f"핵심 맥락: {sender}가 보낸 '{subject}' 메일이며 현재 업무 분류는 {category}입니다.",
            f"결론: 확인된 업무 참조값은 {refs}이고 첨부파일은 {attachments}건입니다.",
            f"필요 조치: {route} 기준으로 원문과 첨부를 검토해 회신 또는 내부 전달 필요 여부를 판단하세요.",
            "리스크/기한: 메일 원문만으로 확정 가능한 기한 또는 리스크는 아직 명시적으로 확인되지 않았습니다.",
        ]
    )


def business_label_of(classification: dict[str, Any] | None) -> str:
    if not isinstance(classification, dict):
        return "미분류"
    explicit = classification.get("business_label") or classification.get("label")
    if explicit:
        return str(explicit)
    category = str(classification.get("mail_category") or "unclassified")
    return LEGACY_CATEGORY_TO_BUSINESS_LABEL.get(category, category if category in BUSINESS_LABEL_ROUTING else "미분류")


def route_label_of(routing_labels: list[str] | None) -> str:
    labels = [ROUTE_NAMES.get(str(label), str(label)) for label in routing_labels or [] if label]
    return labels[0] if labels else "미할당"


def normalize_summary_line(text: Any) -> str:
    cleaned = re.sub(r"^[\s\-\u2022]+", "", str(text or "").strip())
    return " ".join(cleaned.split()).strip()


def executive_summary_prompt(email: dict[str, Any], classification: dict[str, Any]) -> str:
    attachment_names = [
        str(item.get("filename") or "").strip()
        for item in (email.get("attachments") or [])
        if isinstance(item, dict) and str(item.get("filename") or "").strip()
    ]
    refs = ", ".join(str(ref) for ref in classification.get("business_refs", [])[:5]) or "없음"
    vessels = ", ".join(str(name) for name in classification.get("vessel_names", [])[:5]) or "없음"
    routes = ", ".join(str(name) for name in classification.get("routing_labels", [])[:5]) or "없음"
    attachments = ", ".join(attachment_names[:5]) or "없음"
    return textwrap.dedent(
        f"""
        다음 이메일을 바쁜 경영진이 20초 안에 판단할 수 있도록 한국어 executive summary로 작성하세요.
        목적은 전체 맥락, 결론, 필요한 조치, 리스크/기한을 빠르게 파악하게 하는 것입니다.
        메일 원문이나 첨부/분류 정보에 없는 사실은 절대 만들지 마세요.

        출력 규칙:
        - 반드시 아래 4개 라인만 출력하세요.
        - 각 라인은 지정된 라벨로 시작하세요.
        - 각 라인은 1문장으로 간결하게 작성하세요.
        - 정보가 없으면 "명시 없음" 또는 "추가 확인 필요"로 적으세요.

        형식:
        핵심 맥락: ...
        결론: ...
        필요 조치: ...
        리스크/기한: ...

        [분류]
        {business_label_of(classification)}

        [업무 참조]
        {refs}

        [선박]
        {vessels}

        [추천 라우팅]
        {routes}

        [첨부파일]
        {attachments}

        [제목]
        {email.get('subject', '')}

        [발신자]
        {email.get('from', '')}

        [수신자]
        {email.get('to', '')}

        [본문]
        {str(email.get('body', ''))[:1800]}
        """
    ).strip()


def parse_executive_summary(summary: str) -> dict[str, str]:
    text = str(summary or "").strip()
    if not text:
        return {}
    text = text.replace("\r\n", "\n")
    labels = [label for _, label in EXECUTIVE_SUMMARY_FIELDS]
    pattern = "|".join(re.escape(label) for label in labels)
    sections: dict[str, str] = {}
    for _, label in EXECUTIVE_SUMMARY_FIELDS:
        match = re.search(rf"{re.escape(label)}\s*:\s*(.+?)(?=(?:\s*)(?:{pattern})\s*:|\Z)", text, re.S)
        if match:
            sections[label] = normalize_summary_line(match.group(1))
    return sections


def executive_summary_sections(email: dict[str, Any], classification: dict[str, Any], summary: str) -> list[dict[str, str]]:
    defaults = parse_executive_summary(fallback_summary(email, classification))
    parsed = parse_executive_summary(summary)
    if summary and not parsed:
        defaults["핵심 맥락"] = normalize_summary_line(summary)
        defaults["결론"] = "추가 결론은 원문 또는 첨부 검토가 필요합니다."
        defaults["필요 조치"] = f"{route_label_of(classification.get('routing_labels') or [])} 기준으로 후속 조치를 판단하세요."
        defaults["리스크/기한"] = "명시적 기한 또는 리스크는 자동 요약에서 추가 확인이 필요합니다."
    else:
        defaults.update({key: value for key, value in parsed.items() if value})
    return [
        {"key": key, "title": label, "body": defaults.get(label, "추가 확인 필요")}
        for key, label in EXECUTIVE_SUMMARY_FIELDS
    ]


def category_class(category: str) -> str:
    if category in {"발주"}:
        return "red"
    if category in {"문의"}:
        return "gold"
    if category in {"서비스", "기술"}:
        return "blue"
    return ""


def parse_datetime_value(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None


def format_mail_datetime(value: Any) -> str:
    parsed = parse_datetime_value(value)
    if parsed is None:
        return str(value or "-")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(DISPLAY_TIMEZONE)
    weekday = KOREAN_WEEKDAYS[parsed.weekday()]
    return f"{parsed:%Y-%m-%d}({weekday}) {parsed:%H:%M:%S}"


def has_business_label(classification: dict[str, Any] | None) -> bool:
    label = business_label_of(classification)
    return label in BUSINESS_LABEL_ROUTING


def merge_qdrant_label(email: dict[str, Any], current: dict[str, Any], retrieval_limit: int = 5) -> bool:
    if has_business_label(current) and current.get("label_scores"):
        return False

    use_qdrant_evidence = False
    try:
        use_qdrant_evidence = legacy_search.ollama_has_model(legacy_index.OLLAMA_BASE_URL, legacy_index.EMBEDDING_MODEL)
    except Exception:
        use_qdrant_evidence = False

    with _qdrant_lock:
        result = legacy_search.classify_email_label(
            email,
            client=get_qdrant_client(),
            embed_model=get_embed_model() if use_qdrant_evidence else None,
            llm=None,
            retrieval_limit=retrieval_limit,
            use_qdrant_evidence=use_qdrant_evidence,
        )
    label_json = dataclass_to_json(result)
    label = str(label_json.get("label") or "기타")

    previous_category = current.get("mail_category")
    if previous_category and previous_category not in BUSINESS_LABEL_ROUTING:
        current.setdefault("legacy_mail_category", previous_category)
    elif not previous_category or previous_category in BUSINESS_LABEL_ROUTING:
        current["mail_category"] = label
    current["business_label"] = label
    current["label_confidence"] = label_json.get("confidence", 0)
    current["label_scores"] = label_json.get("scores", {})
    current["label_reasons"] = label_json.get("reasons", [])
    current["label_evidence"] = label_json.get("evidence", [])
    current["confidence"] = max(float(current.get("confidence") or 0), float(label_json.get("confidence") or 0))
    current.setdefault("routing_labels", BUSINESS_LABEL_ROUTING.get(label, BUSINESS_LABEL_ROUTING["기타"]))
    if not current.get("routing_labels"):
        current["routing_labels"] = BUSINESS_LABEL_ROUTING.get(label, BUSINESS_LABEL_ROUTING["기타"])
    current.setdefault("model_name", "coramail-qdrant-hybrid")
    current["prompt_version"] = "business-label-v1"
    current["status"] = "completed"

    reasons = list(current.get("reasons", []) or [])
    for reason in label_json.get("reasons", []) or []:
        if reason not in reasons:
            reasons.append(reason)
    current["reasons"] = reasons[:16]
    return True


def generate_summary(email: dict[str, Any], classification: dict[str, Any]) -> str:
    prompt = executive_summary_prompt(email, classification)
    try:
        response = get_llm().complete(prompt)
        summary = "\n".join(line.strip() for line in str(response).splitlines() if line.strip())
        return summary or fallback_summary(email, classification)
    except Exception:
        return fallback_summary(email, classification)


def should_run_full_classification(email: dict[str, Any], classification: dict[str, Any] | None) -> bool:
    if not isinstance(classification, dict) or not classification:
        return True
    if not classification.get("mail_category"):
        return True
    if not classification.get("routing_labels"):
        return True
    if email.get("attachments") and not isinstance(classification.get("attachments"), list):
        return True
    return False


def classify_and_enrich_email(
    email: dict[str, Any],
    classification: dict[str, Any] | None,
    retrieval_limit: int = 5,
    with_summary: bool = True,
) -> dict[str, Any]:
    current = dict(classification or {})
    changed = False

    if should_run_full_classification(email, current):
        try:
            with _qdrant_lock:
                analysis = legacy_classifier.classify_email(
                    email,
                    client=get_qdrant_client(),
                    embed_model=get_embed_model(),
                    llm=get_llm(),
                    retrieval_limit=retrieval_limit,
                    with_summary=with_summary,
                )
            current.update(dataclass_to_json(analysis))
            changed = True
        except Exception as exc:  # noqa: BLE001
            reasons = list(current.get("reasons", []) or [])
            reasons.append(f"full classification fallback: {exc}")
            current["reasons"] = reasons[:16]

    if merge_qdrant_label(email, current, retrieval_limit=retrieval_limit):
        changed = True

    summary = current.get("summary") or current.get("executive_summary")
    if with_summary and not summary:
        generated_summary = generate_summary(email, current)
        current["summary"] = generated_summary
        current["executive_summary"] = generated_summary
        changed = True
    elif not summary:
        generated_summary = fallback_summary(email, current)
        current["summary"] = generated_summary
        current["executive_summary"] = generated_summary
        changed = True
    elif current.get("summary") and not current.get("executive_summary"):
        current["executive_summary"] = current["summary"]
        changed = True
    elif current.get("executive_summary") and not current.get("summary"):
        current["summary"] = current["executive_summary"]
        changed = True

    current.setdefault("model_name", "coramail-qdrant-hybrid")
    current.setdefault("prompt_version", "business-label-v1")
    current["status"] = "completed"

    if changed:
        save_classification(email, current)
    return current


def classification_runtime_state(email_uid: str) -> dict[str, Any]:
    with _classification_queue_lock:
        return dict(_classification_runtime.get(email_uid, {}))


def classification_state(email_uid: str, classification: dict[str, Any] | None = None) -> str:
    if isinstance(classification, dict) and classification.get("status") == "completed":
        return "completed"
    runtime = classification_runtime_state(email_uid)
    state = str(runtime.get("status") or "")
    if state:
        return state
    if isinstance(classification, dict) and classification:
        return str(classification.get("status") or "completed")
    return "unclassified"


def classification_state_label(state: str) -> str:
    return {
        "queued": "대기중",
        "running": "진행중",
        "completed": "완료",
        "failed": "실패",
        "unclassified": "미분류",
    }.get(state, state or "미분류")


def queue_classification(
    email: dict[str, Any],
    retrieval_limit: int = 5,
    with_summary: bool = True,
    existing_classification: dict[str, Any] | None = None,
) -> str:
    email_uid = str(email.get("email_uid", "")).strip()
    if not email_uid:
        return "unclassified"

    existing = existing_classification if isinstance(existing_classification, dict) else load_classifications([email]).get(email_uid, {})
    if not should_run_full_classification(email, existing):
        summary = existing.get("summary") or existing.get("executive_summary")
        if summary:
            return "completed"

    with _classification_queue_lock:
        runtime = _classification_runtime.get(email_uid, {})
        state = str(runtime.get("status") or "")
        if state in {"queued", "running"}:
            return state
        _classification_runtime[email_uid] = {
            "status": "queued",
            "requested_at": datetime.now().isoformat(timespec="seconds"),
            "retrieval_limit": retrieval_limit,
            "with_summary": with_summary,
            "error": "",
        }
        if email_uid not in _classification_enqueued:
            _classification_queue.append(email_uid)
            _classification_enqueued.add(email_uid)
            _classification_queue_event.set()
        return "queued"


def queue_classifications(
    emails: list[dict[str, Any]],
    retrieval_limit: int = 5,
    with_summary: bool = True,
    classifications: dict[str, dict[str, Any]] | None = None,
) -> None:
    for email in emails:
        email_uid = str(email.get("email_uid", "")).strip()
        queue_classification(
            email,
            retrieval_limit=retrieval_limit,
            with_summary=with_summary,
            existing_classification=(classifications or {}).get(email_uid),
        )


def classification_worker() -> None:
    while not _classification_stop.is_set():
        if not _classification_queue_event.wait(1):
            continue

        while True:
            with _classification_queue_lock:
                if not _classification_queue:
                    _classification_queue_event.clear()
                    break
                email_uid = _classification_queue.popleft()
                _classification_enqueued.discard(email_uid)
                runtime = dict(_classification_runtime.get(email_uid, {}))
                _classification_runtime[email_uid] = {
                    **runtime,
                    "status": "running",
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                    "error": "",
                }

            try:
                version = current_mail_data_version()
                selected = load_email_detail(email_uid, version=version)
                if selected is None:
                    raise RuntimeError(f"Email not found for {email_uid}")
                existing = load_classifications([selected], version=version, prefer_cached_full=False).get(email_uid, {})
                retrieval_limit = int(runtime.get("retrieval_limit") or 5)
                with_summary = bool(runtime.get("with_summary", True))
                classify_and_enrich_email(
                    selected,
                    existing,
                    retrieval_limit=retrieval_limit,
                    with_summary=with_summary,
                )
                with _classification_queue_lock:
                    _classification_runtime[email_uid] = {
                        **_classification_runtime.get(email_uid, {}),
                        "status": "completed",
                        "completed_at": datetime.now().isoformat(timespec="seconds"),
                        "error": "",
                    }
            except Exception as exc:  # noqa: BLE001
                with _classification_queue_lock:
                    _classification_runtime[email_uid] = {
                        **_classification_runtime.get(email_uid, {}),
                        "status": "failed",
                        "completed_at": datetime.now().isoformat(timespec="seconds"),
                        "error": str(exc),
                    }


def enrich_classification(email: dict[str, Any], classification: dict[str, Any] | None, retrieval_limit: int = 5) -> dict[str, Any]:
    return classify_and_enrich_email(email, classification, retrieval_limit=retrieval_limit, with_summary=True)


def ensure_classifications(
    emails: list[dict[str, Any]],
    retrieval_limit: int = 5,
    enrich_missing: bool = True,
) -> dict[str, dict[str, Any]]:
    classifications = load_classifications(emails)
    if not enrich_missing:
        return classifications
    for email in emails:
        email_uid = str(email.get("email_uid", ""))
        if not email_uid:
            continue
        classifications[email_uid] = enrich_classification(email, classifications.get(email_uid), retrieval_limit=retrieval_limit)
    return classifications


def compact_email(email: dict[str, Any], index: int, classification: dict[str, Any] | None = None) -> dict[str, Any]:
    raw_attachments = email.get("attachments", []) or []
    attachments = [
        build_attachment_payload(attachment, index, attachment_index)
        for attachment_index, attachment in enumerate(raw_attachments)
        if isinstance(attachment, dict)
    ]
    body = str(email.get("body", ""))
    email_uid = str(email.get("email_uid", ""))
    state = classification_state(email_uid, classification)
    state_label = classification_state_label(state)
    summary = (classification or {}).get("summary") or (classification or {}).get("executive_summary") or ""
    return {
        "index": index,
        "email_uid": email_uid,
        "from": email.get("from", ""),
        "to": email.get("to", ""),
        "subject": email.get("subject", ""),
        "date": email.get("date", ""),
        "body": body,
        "body_preview": body[:600],
        "has_attachment": bool(email.get("has_attachment")),
        "attachment_count": len(attachments),
        "attachments": attachments,
        "summary": summary,
        "executive_summary_sections": executive_summary_sections(email, classification or {}, summary),
        "classification": classification or {},
        "business_label": business_label_of(classification) if state == "completed" else state_label,
        "classification_state": state,
        "classification_state_label": state_label,
    }


def stable_digest(payload: Any) -> str:
    data = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha1(data.encode("utf-8")).hexdigest()[:16]


def normalized_business_refs(values: Any) -> list[str]:
    refs: list[str] = []
    for value in values or []:
        ref = str(value or "").strip()
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def find_email_index_by_uid(emails: list[dict[str, Any]], email_uid: str | None) -> int | None:
    target = str(email_uid or "").strip()
    if not target:
        return None
    for index, email in enumerate(emails):
        if str(email.get("email_uid", "")).strip() == target:
            return index
    return None


def resolve_selected_email(
    emails: list[dict[str, Any]],
    email_index: int | None = None,
    email_uid: str | None = None,
) -> tuple[int | None, dict[str, Any] | None]:
    resolved_index = find_email_index_by_uid(emails, email_uid)
    if resolved_index is None and email_index is not None and 0 <= email_index < len(emails):
        resolved_index = email_index
    if resolved_index is None:
        return None, None
    return resolved_index, emails[resolved_index]


def related_emails_by_refs(
    source_email: dict[str, Any],
    source_classification: dict[str, Any],
    *,
    version: str | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    source_uid = str(source_email.get("email_uid") or "").strip()
    target_refs = set(normalized_business_refs(source_classification.get("business_refs", [])))
    if not target_refs:
        return []

    version = version or current_mail_data_version()
    all_emails = load_email_headers(version=version)
    classifications = load_classifications(all_emails, version=version)
    related: list[dict[str, Any]] = []

    for index, candidate in enumerate(all_emails):
        candidate_uid = str(candidate.get("email_uid") or "").strip()
        if not candidate_uid or candidate_uid == source_uid:
            continue
        candidate_classification = classifications.get(candidate_uid, {})
        candidate_refs = normalized_business_refs(candidate_classification.get("business_refs", []))
        matched_refs = [ref for ref in candidate_refs if ref in target_refs]
        if not matched_refs:
            continue
        item = compact_email(candidate, index, candidate_classification)
        item["matched_refs"] = matched_refs
        related.append(item)
        if len(related) >= limit:
            break
    return related


def qdrant_count(collection: str) -> int:
    client = get_qdrant_client()
    if not client.collection_exists(collection):
        return 0
    return int(client.count(collection_name=collection, exact=True).count)


def cached_qdrant_points(max_age_seconds: float = 30.0) -> dict[str, Any]:
    now = time.monotonic()
    with _summary_cache_lock:
        cached = dict(_summary_cache.get("qdrant_points", {}))
        updated_at = float(_summary_cache.get("qdrant_updated_at", 0.0) or 0.0)
    if cached and now - updated_at <= max_age_seconds:
        return cached

    if not _qdrant_lock.acquire(blocking=False):
        return cached or {"emails": None, "attachments": None, "status": "busy"}
    try:
        points = {
            "emails": qdrant_count(legacy_index.EMAIL_COLLECTION),
            "attachments": qdrant_count(legacy_index.ATTACHMENT_COLLECTION),
        }
    except Exception as exc:  # noqa: BLE001
        points = cached or {"emails": None, "attachments": None, "status": str(exc)}
    finally:
        _qdrant_lock.release()

    with _summary_cache_lock:
        _summary_cache["qdrant_points"] = points
        _summary_cache["qdrant_updated_at"] = now
    return points


def dataclass_to_json(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    return value


def result_payload(result: Any, final_score: float) -> dict[str, Any]:
    payload = getattr(result, "payload", None) or {}
    raw_text = str(payload.get("raw_text") or payload.get("caption") or payload.get("embedding_text") or "")
    source_kind = "첨부 문서" if payload.get("filename") or payload.get("stored_name") else "이메일 본문"
    business_refs = payload.get("business_refs", [])
    vessels = payload.get("vessel_names", [])
    explanation_bits = [source_kind]
    if payload.get("document_category_label") or payload.get("mail_category"):
        explanation_bits.append(f"분류 {payload.get('document_category_label') or payload.get('mail_category')}")
    if business_refs:
        explanation_bits.append(f"참조 {', '.join(str(ref) for ref in business_refs[:2])}")
    if vessels:
        explanation_bits.append(f"선박 {', '.join(str(name) for name in vessels[:2])}")
    if payload.get("parent_email_from") or payload.get("from"):
        explanation_bits.append(f"발신 {payload.get('parent_email_from') or payload.get('from')}")
    return {
        "final_score": round(float(final_score), 4),
        "vector_score": round(legacy_search.vector_score(result), 4),
        "source": payload.get("filename") or payload.get("stored_name") or payload.get("subject") or "unknown",
        "title": payload.get("parent_email_subject") or payload.get("subject") or "",
        "category": payload.get("document_category_label") or payload.get("mail_category") or "",
        "document_category": payload.get("document_category", ""),
        "file_group": payload.get("file_group", ""),
        "business_refs": payload.get("business_refs", []),
        "vessel_names": payload.get("vessel_names", []),
        "parent_email_from": payload.get("parent_email_from") or payload.get("from") or "",
        "preview": " ".join(raw_text.split())[:900],
        "match_explanation": " · ".join(explanation_bits),
        "payload": payload,
    }


def start_legacy_script(script_name: str, args: list[str]) -> tuple[subprocess.Popen[str], Path, list[str]]:
    require_pipeline()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = RUN_DIR / f"{Path(script_name).stem}_{stamp}.log"
    command = [sys.executable, str(PIPELINE_DIR / script_name), *args]
    with log_path.open("w", encoding="utf-8") as log_file:
        process = subprocess.Popen(  # noqa: S603
            command,
            cwd=PIPELINE_DIR,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
    return process, log_path, command


def launch_legacy_script(script_name: str, args: list[str]) -> dict[str, Any]:
    process, log_path, command = start_legacy_script(script_name, args)
    return {"pid": process.pid, "log_path": str(log_path), "command": command}


def auto_sync_args() -> list[str]:
    args = ["--max", str(AUTO_SYNC_MAX_EMAILS), "--retrieval-limit", str(AUTO_SYNC_RETRIEVAL_LIMIT)]
    if AUTO_SYNC_KEYWORD:
        args.extend(["--keyword", AUTO_SYNC_KEYWORD])
    if AUTO_SYNC_CLASSIFICATION_LIMIT:
        args.extend(["--classification-limit", AUTO_SYNC_CLASSIFICATION_LIMIT])
    return args


def launch_auto_ingest_job(args: list[str], reason: str) -> dict[str, Any]:
    global _auto_sync_process, _auto_sync_launching
    started_at = datetime.now().isoformat(timespec="seconds")
    with _auto_sync_lock:
        refresh_auto_sync_status_locked()
        if _auto_sync_process is not None or _auto_sync_launching:
            _auto_sync_status["last_skip_reason"] = "previous auto sync is still running"
            return {
                "started": False,
                "running": True,
                "pid": _auto_sync_process.pid if _auto_sync_process is not None else None,
                "log_path": _auto_sync_status.get("last_log_path"),
                "command": [],
                "message": _auto_sync_status["last_skip_reason"],
            }
        _auto_sync_launching = True
        _auto_sync_status.update(
            {
                "enabled": AUTO_SYNC_ENABLED,
                "running": True,
                "current_pid": None,
                "last_started_at": started_at,
                "last_completed_at": None,
                "last_exit_code": None,
                "last_error": None,
                "last_skip_reason": None,
                "last_reason": reason,
            }
        )
    try:
        close_qdrant_client()
        process, log_path, command = start_legacy_script("auto_ingest.py", args)
    except Exception as exc:  # noqa: BLE001
        with _auto_sync_lock:
            _auto_sync_launching = False
            _auto_sync_status["last_error"] = str(exc)
            _auto_sync_status["running"] = False
            _auto_sync_status["current_pid"] = None
            _auto_sync_status["last_exit_code"] = None
        return {
            "started": False,
            "running": False,
            "pid": None,
            "log_path": None,
            "command": [],
            "message": str(exc),
        }
    with _auto_sync_lock:
        _auto_sync_process = process
        _auto_sync_launching = False
        _auto_sync_status.update(
            {
                "enabled": AUTO_SYNC_ENABLED,
                "running": True,
                "current_pid": process.pid,
                "last_started_at": started_at,
                "last_completed_at": None,
                "last_exit_code": None,
                "last_log_path": str(log_path),
                "last_error": None,
                "last_skip_reason": None,
                "last_reason": reason,
            }
        )
        return {
            "started": True,
            "running": True,
            "pid": process.pid,
            "log_path": str(log_path),
            "command": command,
            "message": f"auto_ingest started: {reason}",
        }


def refresh_auto_sync_status_locked() -> None:
    global _auto_sync_process
    if _auto_sync_launching:
        _auto_sync_status["running"] = True
        _auto_sync_status["current_pid"] = None
        return
    if _auto_sync_process is None:
        _auto_sync_status["running"] = False
        _auto_sync_status["current_pid"] = None
        return
    exit_code = _auto_sync_process.poll()
    if exit_code is None:
        _auto_sync_status["running"] = True
        _auto_sync_status["current_pid"] = _auto_sync_process.pid
        return
    _auto_sync_status["running"] = False
    _auto_sync_status["current_pid"] = None
    _auto_sync_status["last_exit_code"] = exit_code
    _auto_sync_status["last_completed_at"] = datetime.now().isoformat(timespec="seconds")
    _auto_sync_process = None


def start_auto_sync_once(reason: str) -> bool:
    return bool(launch_auto_ingest_job(auto_sync_args(), reason).get("started"))


def auto_sync_worker() -> None:
    if _auto_sync_stop.wait(AUTO_SYNC_INITIAL_DELAY_SECONDS):
        return
    start_auto_sync_once("startup")
    while not _auto_sync_stop.wait(AUTO_SYNC_INTERVAL_SECONDS):
        start_auto_sync_once("interval")


@app.on_event("startup")
def start_auto_sync_scheduler() -> None:
    global _auto_sync_thread, _classification_thread
    if not AUTO_SYNC_ENABLED:
        pass
    if STARTUP_ERROR:
        _auto_sync_status["last_error"] = f"Pipeline import failed: {STARTUP_ERROR}"
    else:
        if AUTO_SYNC_ENABLED and not (_auto_sync_thread and _auto_sync_thread.is_alive()):
            _auto_sync_stop.clear()
            _auto_sync_thread = Thread(target=auto_sync_worker, name="coramail-auto-sync", daemon=True)
            _auto_sync_thread.start()
        if not (_classification_thread and _classification_thread.is_alive()):
            _classification_stop.clear()
            _classification_thread = Thread(target=classification_worker, name="coramail-classification", daemon=True)
            _classification_thread.start()


@app.on_event("shutdown")
def stop_auto_sync_scheduler() -> None:
    _auto_sync_stop.set()
    _classification_stop.set()
    _classification_queue_event.set()
    if _auto_sync_thread and _auto_sync_thread.is_alive():
        _auto_sync_thread.join(timeout=2)
    if _classification_thread and _classification_thread.is_alive():
        _classification_thread.join(timeout=2)


@app.get("/api/health")
def health() -> dict[str, Any]:
    email_database_url = (
        legacy_fetcher.redact_database_url(getattr(legacy_index, "EMAIL_DATABASE_URL", "")) if not STARTUP_ERROR else ""
    )
    return {
        "ok": STARTUP_ERROR is None,
        "startup_error": STARTUP_ERROR,
        "app_dir": str(APP_DIR),
        "pipeline_dir": str(PIPELINE_DIR),
        "email_db": email_database_url,
        "email_database_url": email_database_url,
        "qdrant_path": str(getattr(legacy_index, "QDRANT_PATH", "")) if not STARTUP_ERROR else "",
        "embedding_model": getattr(legacy_index, "EMBEDDING_MODEL", "") if not STARTUP_ERROR else "",
        "llm_model": getattr(legacy_index, "LLM_MODEL", "") if not STARTUP_ERROR else "",
        "ollama_base_url": getattr(legacy_index, "OLLAMA_BASE_URL", "") if not STARTUP_ERROR else "",
    }


@app.get("/api/auto-sync")
def auto_sync_status() -> dict[str, Any]:
    with _auto_sync_lock:
        refresh_auto_sync_status_locked()
        return dict(_auto_sync_status)


@app.get("/api/client-version")
def client_version() -> dict[str, str]:
    return {"version": current_asset_version()}


@app.get("/api/ui-state")
def ui_state(
    view: str,
    q: str = "",
    category: str = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 40,
    selected_email_index: int | None = None,
    selected_email_uid: str = "",
) -> dict[str, Any]:
    version = current_mail_data_version()
    if view == "dashboard":
        all_emails = load_email_headers(version=version)
        classifications = load_classifications(all_emails, version=version)
        rows = filtered_email_rows(all_emails, classifications)
        business_labels: dict[str, int] = {}
        for email in all_emails:
            classification = classifications.get(str(email.get("email_uid", "")), {})
            label = business_label_of(classification)
            business_labels[label] = business_labels.get(label, 0) + 1
        metrics = load_mail_metrics(version=version)
        with _auto_sync_lock:
            refresh_auto_sync_status_locked()
            sync_status = dict(_auto_sync_status)
        return {
            "view": "dashboard",
            "versions": {
                "auto_sync": stable_digest(auto_sync_signature(sync_status)),
                "stats": stable_digest(
                    {
                        "email_count": metrics["email_count"],
                        "classified_count": metrics["classified_count"],
                        "duplicate_risk_count": metrics["duplicate_risk_count"],
                        "attachment_count": metrics["attachment_count"],
                        "routed_count": metrics["routed_count"],
                    }
                ),
                "distribution": stable_digest(business_labels),
                "mail_rows": stable_digest(dashboard_row_signature(rows)),
            },
        }

    if view == "inbox":
        all_emails = load_emails(version=version) if q.strip() else load_email_headers(version=version)
        classifications = load_classifications(all_emails, version=version)
        rows = filtered_email_rows(all_emails, classifications, q=q, category=category)
        page_items = rows[:limit]
        resolved_index, resolved_email = resolve_selected_email(
            all_emails,
            email_index=selected_email_index,
            email_uid=selected_email_uid,
        )
        if resolved_email is None and page_items:
            fallback_uid = str(page_items[0].get("email_uid") or "").strip()
            resolved_index, resolved_email = resolve_selected_email(all_emails, email_uid=fallback_uid)

        detail_item = None
        detail_related: list[dict[str, Any]] = []
        if resolved_email is not None and resolved_index is not None:
            detail_email = load_email_detail(str(resolved_email.get("email_uid") or ""), version=version)
            if detail_email is not None:
                classification = classifications.get(str(detail_email.get("email_uid", "")), {})
                detail_item = compact_email(detail_email, resolved_index, classification)
                detail_related = related_emails_by_refs(detail_email, classification, version=version)

        return {
            "view": "inbox",
            "selected_email_index": resolved_index,
            "selected_email_uid": detail_item.get("email_uid", "") if detail_item else "",
            "versions": {
                "mail_rows": stable_digest(inbox_row_signature(page_items)),
                "detail": stable_digest({"email": detail_item or {}, "related": detail_related}),
            },
        }

    raise HTTPException(status_code=400, detail="Unsupported view")


def summary_payload(
    emails: list[dict[str, Any]] | None = None,
    classifications: dict[str, dict[str, Any]] | None = None,
    metrics: dict[str, int] | None = None,
    version: str | None = None,
) -> dict[str, Any]:
    version = version or current_mail_data_version()
    emails = emails if emails is not None else load_email_headers(version=version)
    classifications = classifications if classifications is not None else load_classifications(emails, version=version)
    metrics = metrics if metrics is not None else load_mail_metrics(version=version)
    mail_categories: dict[str, int] = {}
    business_labels: dict[str, int] = {}
    attachment_categories: dict[str, int] = {}
    for email in emails:
        classification = classifications.get(str(email.get("email_uid", "")), {})
        category = business_label_of(classification)
        mail_categories[category] = mail_categories.get(category, 0) + 1
        business_labels[category] = business_labels.get(category, 0) + 1
        for attachment in classification.get("attachments", []) if isinstance(classification, dict) else []:
            label = attachment.get("document_category_label") or attachment.get("document_category") or "미분류"
            attachment_categories[label] = attachment_categories.get(label, 0) + 1

    qdrant = cached_qdrant_points()

    return {
        "email_count": metrics["email_count"],
        "attachment_count": metrics["attachment_count"],
        "classified_count": metrics["classified_count"],
        "routed_count": metrics["routed_count"],
        "duplicate_risk_count": metrics["duplicate_risk_count"],
        "mail_categories": mail_categories,
        "business_labels": business_labels,
        "attachment_categories": attachment_categories,
        "qdrant_points": qdrant,
        "classification_store": classification_store_name(),
    }


def filtered_email_rows(
    all_emails: list[dict[str, Any]],
    classifications: dict[str, dict[str, Any]],
    q: str = "",
    category: str = "",
) -> list[dict[str, Any]]:
    rows = []
    needle = q.casefold().strip()
    for index, email in enumerate(all_emails):
        classification = classifications.get(str(email.get("email_uid", "")), {})
        haystack = "\n".join(
            [
                str(email.get("subject", "")),
                str(email.get("from", "")),
                str(email.get("to", "")),
                str(email.get("body", "")),
                " ".join(str(ref) for ref in classification.get("business_refs", [])),
            ]
        ).casefold()
        if needle and needle not in haystack:
            continue
        if category and business_label_of(classification) != category:
            continue
        rows.append(compact_email(email, index, classification))
    return rows


def merge_row_classification(row: dict[str, Any], classification: dict[str, Any]) -> dict[str, Any]:
    merged = dict(row)
    email_uid = str(merged.get("email_uid", ""))
    state = classification_state(email_uid, classification)
    state_label = classification_state_label(state)
    merged["classification"] = classification
    merged["summary"] = classification.get("summary") or classification.get("executive_summary") or merged.get("summary", "")
    merged["business_label"] = business_label_of(classification) if state == "completed" else state_label
    merged["classification_state"] = state
    merged["classification_state_label"] = state_label
    return merged


def dashboard_row_signature(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "email_uid": item.get("email_uid"),
            "state": item.get("classification_state"),
            "label": item.get("business_label"),
            "route": route_label_of((item.get("classification") or {}).get("routing_labels") or []),
            "time": item.get("date"),
        }
        for item in items
    ]


def inbox_row_signature(items: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("email_uid") or "") for item in items]


def auto_sync_signature(sync_status: dict[str, Any]) -> dict[str, Any]:
    return {
        "running": sync_status.get("running"),
        "current_pid": sync_status.get("current_pid"),
        "last_started_at": sync_status.get("last_started_at"),
        "last_completed_at": sync_status.get("last_completed_at"),
        "last_exit_code": sync_status.get("last_exit_code"),
        "last_error": sync_status.get("last_error"),
        "last_skip_reason": sync_status.get("last_skip_reason"),
        "last_log_path": sync_status.get("last_log_path"),
    }


@app.get("/api/summary")
def summary() -> dict[str, Any]:
    return summary_payload()


def email_page_payload(
    q: str = "",
    category: str = "",
    limit: int | None = 50,
    offset: int = 0,
    auto_classify: bool = False,
) -> dict[str, Any]:
    version = current_mail_data_version()
    all_emails = load_emails(version=version) if q.strip() else load_email_headers(version=version)
    classifications = load_classifications(all_emails, version=version)
    rows = filtered_email_rows(all_emails, classifications, q=q, category=category)

    page = rows[offset:] if limit is None else rows[offset : offset + limit]
    if auto_classify:
        queue_candidates: list[dict[str, Any]] = []
        for item in page[:LIST_AUTO_CLASSIFY_LIMIT]:
            detail = load_email_detail(str(item.get("email_uid") or ""), version=version)
            if detail is not None:
                queue_candidates.append(detail)
        enriched = load_classifications(queue_candidates, version=version, prefer_cached_full=False) if queue_candidates else {}
        queue_classifications(queue_candidates, retrieval_limit=5, with_summary=True, classifications=enriched)
    else:
        enriched = {str(item.get("email_uid", "")): item.get("classification", {}) for item in page}
    return {
        "total": len(rows),
        "items": [merge_row_classification(item, enriched.get(str(item.get("email_uid", "")), item.get("classification", {}))) for item in page],
    }


def ui_globals() -> dict[str, Any]:
    return {
        "asset_version": current_asset_version(),
        "category_order": CATEGORY_ORDER,
        "route_label": route_label_of,
        "business_label": business_label_of,
        "category_class": category_class,
        "format_mail_datetime": format_mail_datetime,
        "route_by_category": {
            "발주": "sales",
            "문의": "quote_request",
            "서비스": "service",
            "기술": "technical",
            "기타": "general",
        },
    }


def ui_dashboard_context() -> dict[str, Any]:
    version = current_mail_data_version()
    all_emails = load_email_headers(version=version)
    classifications = load_classifications(all_emails, version=version)
    rows = filtered_email_rows(all_emails, classifications)
    email_page = {
        "total": len(rows),
        "items": rows,
    }
    metrics = load_mail_metrics(version=version)
    with _auto_sync_lock:
        refresh_auto_sync_status_locked()
        sync_status = dict(_auto_sync_status)
    return {
        **ui_globals(),
        "summary": summary_payload(all_emails, classifications, metrics=metrics, version=version),
        "emails": email_page["items"],
        "total_emails": email_page["total"],
        "sync": sync_status,
    }


def ui_email_detail_context(email_index: int) -> dict[str, Any]:
    version = current_mail_data_version()
    email = load_email_by_index(email_index, version=version, full=True)
    if email is None:
        raise HTTPException(status_code=404, detail="이메일을 찾을 수 없습니다.")
    classification = load_classifications([email], version=version, prefer_cached_full=False).get(str(email.get("email_uid", "")), {})
    queue_classification(email, retrieval_limit=5, with_summary=True, existing_classification=classification)
    related_emails = related_emails_by_refs(email, classification, version=version)
    return {
        **ui_globals(),
        "email": compact_email(email, email_index, classification),
        "related_emails": related_emails,
    }


@app.get("/", response_class=HTMLResponse)
def ui_root(request: Request) -> HTMLResponse:
    context = {
        **ui_dashboard_context(),
        "active_view": "dashboard",
        "request": request,
    }
    html = templates.get_template("shell.html").render(context)
    return HTMLResponse(content=html)


@app.get("/ui/dashboard", response_class=HTMLResponse)
def ui_dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "views/dashboard.html", ui_dashboard_context())


@app.get("/ui/stats", response_class=HTMLResponse)
def ui_stats(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "partials/stats.html",
        {
            **ui_globals(),
            "summary": summary_payload(),
        },
    )


@app.get("/ui/dashboard-distribution", response_class=HTMLResponse)
def ui_dashboard_distribution(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "partials/dashboard_distribution.html",
        {
            **ui_globals(),
            "summary": summary_payload(),
        },
    )


@app.get("/ui/auto-sync", response_class=HTMLResponse)
def ui_auto_sync(request: Request) -> HTMLResponse:
    with _auto_sync_lock:
        refresh_auto_sync_status_locked()
        sync_status = dict(_auto_sync_status)
    return templates.TemplateResponse(request, "partials/auto_sync.html", {"sync": sync_status})


@app.get("/ui/mail-rows", response_class=HTMLResponse)
def ui_mail_rows(
    request: Request,
    q: str = "",
    category: str = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 40,
    view: str = "inbox",
    selected_email_index: int | None = None,
    selected_email_uid: str = "",
) -> HTMLResponse:
    email_page = email_page_payload(
        q=q,
        category=category,
        limit=None if view == "dashboard" else limit,
        auto_classify=True,
    )
    return templates.TemplateResponse(
        request,
        "partials/mail_rows.html",
        {
            **ui_globals(),
            "emails": email_page["items"],
            "mail_rows_mode": "dashboard" if view == "dashboard" else "inbox",
            "selected_email_index": selected_email_index,
            "selected_email_uid": selected_email_uid,
        },
    )


@app.get("/ui/inbox", response_class=HTMLResponse)
def ui_inbox(request: Request, email_index: int = 0, email_uid: str = "") -> HTMLResponse:
    version = current_mail_data_version()
    email_page = email_page_payload(limit=80, auto_classify=True)
    detail_context: dict[str, Any] = {"email": None}
    if email_page["items"]:
        all_emails = load_email_headers(version=version)
        resolved_index, _ = resolve_selected_email(all_emails, email_index=email_index, email_uid=email_uid)
        available_indices = {int(email["index"]) for email in email_page["items"]}
        if resolved_index is None or resolved_index not in available_indices:
            first_uid = str(email_page["items"][0].get("email_uid") or "").strip()
            resolved_index, _ = resolve_selected_email(all_emails, email_uid=first_uid)
        if resolved_index is not None:
            detail_context = ui_email_detail_context(resolved_index)
    return templates.TemplateResponse(
        request,
        "views/inbox.html",
        {
            **ui_globals(),
            "emails": email_page["items"],
            "selected_email_index": detail_context["email"]["index"] if detail_context.get("email") else None,
            "selected_email_uid": detail_context["email"]["email_uid"] if detail_context.get("email") else "",
            **detail_context,
        },
    )


@app.get("/ui/emails/{email_index}", response_class=HTMLResponse)
def ui_email_detail(request: Request, email_index: int) -> HTMLResponse:
    return templates.TemplateResponse(request, "partials/email_detail.html", ui_email_detail_context(email_index))


@app.post("/ui/emails/{email_index}/classify", response_class=HTMLResponse)
def ui_classify_email(request: Request, email_index: int) -> HTMLResponse:
    classify(ClassifyRequest(email_index=email_index, retrieval_limit=5, with_summary=False))
    return templates.TemplateResponse(request, "partials/email_detail.html", ui_email_detail_context(email_index))


@app.get("/ui/search", response_class=HTMLResponse)
def ui_search(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "views/search.html", ui_globals())


@app.get("/ui/search-results", response_class=HTMLResponse)
def ui_search_results(
    request: Request,
    q: str = "",
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
) -> HTMLResponse:
    result: dict[str, Any] | None = None
    error = ""
    if q.strip():
        try:
            result = search(SearchRequest(query=q, limit=limit, with_answer=True))
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
    return templates.TemplateResponse(
        request,
        "partials/search_results.html",
        {
            **ui_globals(),
            "query": q,
            "result": result,
            "error": error,
        },
    )


@app.get("/ui/settings", response_class=HTMLResponse)
def ui_settings(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "views/settings.html",
        {
            **ui_globals(),
            "health": health(),
            "summary": summary_payload(),
        },
    )


@app.post("/ui/auto-sync/run", response_class=HTMLResponse)
def ui_auto_sync_run(request: Request) -> HTMLResponse:
    launch_auto_ingest_job(auto_sync_args(), "manual-button")
    with _auto_sync_lock:
        refresh_auto_sync_status_locked()
        sync_status = dict(_auto_sync_status)
    return templates.TemplateResponse(request, "partials/auto_sync.html", {"sync": sync_status})


@app.get("/api/emails")
def emails(
    q: str = "",
    category: str = "",
    limit: Annotated[int, Query(ge=1, le=300)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    auto_classify: bool = False,
) -> dict[str, Any]:
    return email_page_payload(q=q, category=category, limit=limit, offset=offset, auto_classify=auto_classify)


@app.get("/api/emails/{email_index}")
def email_detail(email_index: int) -> dict[str, Any]:
    version = current_mail_data_version()
    email = load_email_by_index(email_index, version=version, full=True)
    if email is None:
        raise HTTPException(status_code=404, detail="이메일을 찾을 수 없습니다.")
    classification = load_classifications([email], version=version, prefer_cached_full=False).get(str(email.get("email_uid", "")), {})
    queue_classification(email, retrieval_limit=5, with_summary=True, existing_classification=classification)
    return {"item": compact_email(email, email_index, classification)}


@app.get("/api/emails/{email_index}/attachments/{attachment_index}")
def email_attachment(email_index: int, attachment_index: int, download: bool = False) -> FileResponse:
    version = current_mail_data_version()
    email = load_email_by_index(email_index, version=version, full=True)
    if email is None:
        raise HTTPException(status_code=404, detail="이메일을 찾을 수 없습니다.")

    attachments = email.get("attachments", []) or []
    if attachment_index < 0 or attachment_index >= len(attachments):
        raise HTTPException(status_code=404, detail="첨부파일을 찾을 수 없습니다.")

    attachment = attachments[attachment_index]
    if not isinstance(attachment, dict):
        raise HTTPException(status_code=404, detail="첨부파일을 찾을 수 없습니다.")

    file_path = resolve_attachment_path(str(attachment.get("file_path") or ""))
    if file_path is None or not file_path.exists():
        raise HTTPException(status_code=404, detail="첨부파일 원본이 존재하지 않습니다.")

    filename = str(attachment.get("filename") or file_path.name)
    media_type = (
        str(attachment.get("content_type") or "").strip()
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )
    disposition = "attachment" if download else "inline"
    return FileResponse(
        path=file_path,
        media_type=media_type,
        filename=filename,
        content_disposition_type=disposition,
    )


@app.post("/api/search")
def search(request: SearchRequest) -> dict[str, Any]:
    require_pipeline()
    try:
        with _qdrant_lock:
            intent, results = legacy_search.hybrid_search(
                client=get_qdrant_client(),
                embed_model=get_embed_model(),
                query=request.query,
                limit=request.limit,
            )
        answer = legacy_search.generate_answer(get_llm(), request.query, results) if request.with_answer else ""
        return {
            "intent": asdict(intent),
            "answer": answer,
            "context": legacy_search.build_answer_context(results, max_chars=5000),
            "results": [result_payload(result, score) for result, score in results],
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/classify")
def classify(request: ClassifyRequest) -> dict[str, Any]:
    version = current_mail_data_version()
    all_headers = load_email_headers(version=version)
    selected: dict[str, Any] | None = None
    if request.email_uid:
        selected = load_email_detail(request.email_uid, version=version)
    elif request.email_index is not None and request.email_index < len(all_headers):
        selected = load_email_by_index(request.email_index, version=version, full=True)
    if selected is None:
        raise HTTPException(status_code=404, detail="대상 이메일을 찾을 수 없습니다.")

    try:
        existing = load_classifications([selected], version=version, prefer_cached_full=False).get(str(selected.get("email_uid", "")), {})
        classification_json = classify_and_enrich_email(
            selected,
            existing,
            retrieval_limit=request.retrieval_limit,
            with_summary=True,
        )
        selected_index = find_email_index_by_uid(all_headers, str(selected.get("email_uid", "")))
        return {
            "email": compact_email(selected, selected_index or 0, classification_json),
            "classification": classification_json,
            "classification_store": classification_store_name(),
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/reindex")
def reindex() -> dict[str, Any]:
    close_qdrant_client()
    return launch_legacy_script("index.py", [])


@app.post("/api/fetch")
def fetch(request: FetchRequest) -> dict[str, Any]:
    require_pipeline()
    legacy_fetcher.GmailFetchConfig.from_base_dir(PIPELINE_DIR)
    args = ["--max", str(request.max_emails)]
    search_keyword = request.search_keyword.strip()
    if search_keyword:
        args.extend(["--keyword", search_keyword])
    if request.date_after:
        args.extend(["--date-after", request.date_after])
    if request.date_before:
        args.extend(["--date-before", request.date_before])
    if request.sender:
        args.extend(["--sender", request.sender])
    if request.has_attachment is True:
        args.append("--has-attachment")
    if request.auto_process:
        if request.classification_limit:
            args.extend(["--classification-limit", str(request.classification_limit)])
        args.extend(["--retrieval-limit", str(request.retrieval_limit)])
        return launch_auto_ingest_job(args, "manual-api")
    return launch_legacy_script("gmail_postgres_fetcher.py", args)
