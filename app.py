from __future__ import annotations

import atexit
import importlib
import json
import os
import subprocess
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock, RLock
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
RUN_DIR = APP_DIR / "runs"
PIPELINE_DIR = Path(os.getenv("CORAMAIL_PIPELINE_DIR", str(APP_DIR / "pipeline"))).resolve()

if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

try:
    from llama_index.embeddings.ollama import OllamaEmbedding
    from llama_index.llms.ollama import Ollama
    from qdrant_client import QdrantClient

    legacy_index = importlib.import_module("index")
    legacy_fetcher = importlib.import_module("gmail_sqlite_fetcher")
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

_model_lock = Lock()
_qdrant_lock = RLock()
_embed_model: Any | None = None
_llm: Any | None = None
_qdrant_client: Any | None = None


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    limit: int = Field(5, ge=1, le=20)
    with_answer: bool = True


class ClassifyRequest(BaseModel):
    email_uid: str | None = None
    email_index: int | None = Field(None, ge=0)
    retrieval_limit: int = Field(5, ge=1, le=20)


class FetchRequest(BaseModel):
    search_keyword: str = "quote order purchase"
    max_emails: int = Field(100, ge=1, le=2000)
    date_after: str | None = None
    date_before: str | None = None
    sender: str | None = None
    has_attachment: bool | None = None


def require_pipeline() -> None:
    if STARTUP_ERROR:
        raise HTTPException(status_code=500, detail=f"Pipeline import failed: {STARTUP_ERROR}")


def get_embed_model() -> Any:
    require_pipeline()
    global _embed_model
    with _model_lock:
        if _embed_model is None:
            _embed_model = OllamaEmbedding(model_name=legacy_index.EMBEDDING_MODEL)
        return _embed_model


def get_llm() -> Any:
    require_pipeline()
    global _llm
    with _model_lock:
        if _llm is None:
            _llm = Ollama(model=legacy_index.LLM_MODEL, request_timeout=1000.0)
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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


def load_emails() -> list[dict[str, Any]]:
    require_pipeline()
    return legacy_index.load_emails_from_sqlite(legacy_index.EMAIL_DB_PATH)


def report_path() -> Path:
    return PIPELINE_DIR / "classification_report.json"


def load_report_rows() -> list[dict[str, Any]]:
    path = report_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def email_key(email: dict[str, Any]) -> tuple[str, str, str]:
    return (str(email.get("subject", "")), str(email.get("from", "")), str(email.get("date", "")))


def classification_map() -> dict[tuple[str, str, str], dict[str, Any]]:
    rows = load_report_rows()
    return {
        (str(row.get("subject", "")), str(row.get("from", "")), str(row.get("date", ""))): row.get("classification", {})
        for row in rows
        if isinstance(row, dict)
    }


def compact_email(email: dict[str, Any], index: int, classification: dict[str, Any] | None = None) -> dict[str, Any]:
    attachments = email.get("attachments", []) or []
    body = str(email.get("body", ""))
    return {
        "index": index,
        "email_uid": email.get("email_uid", ""),
        "from": email.get("from", ""),
        "to": email.get("to", ""),
        "subject": email.get("subject", ""),
        "date": email.get("date", ""),
        "body": body,
        "body_preview": body[:600],
        "has_attachment": bool(email.get("has_attachment")),
        "attachment_count": len(attachments),
        "attachments": attachments,
        "classification": classification or {},
    }


def qdrant_count(collection: str) -> int:
    client = get_qdrant_client()
    if not client.collection_exists(collection):
        return 0
    return int(client.count(collection_name=collection, exact=True).count)


def dataclass_to_json(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    return value


def result_payload(result: Any, final_score: float) -> dict[str, Any]:
    payload = getattr(result, "payload", None) or {}
    raw_text = str(payload.get("raw_text") or payload.get("caption") or payload.get("embedding_text") or "")
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
        "payload": payload,
    }


def launch_legacy_script(script_name: str, args: list[str]) -> dict[str, Any]:
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
    return {"pid": process.pid, "log_path": str(log_path), "command": command}


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": STARTUP_ERROR is None,
        "startup_error": STARTUP_ERROR,
        "app_dir": str(APP_DIR),
        "pipeline_dir": str(PIPELINE_DIR),
        "email_db": str(getattr(legacy_index, "EMAIL_DB_PATH", "")) if not STARTUP_ERROR else "",
        "qdrant_path": str(getattr(legacy_index, "QDRANT_PATH", "")) if not STARTUP_ERROR else "",
        "embedding_model": getattr(legacy_index, "EMBEDDING_MODEL", "") if not STARTUP_ERROR else "",
        "llm_model": getattr(legacy_index, "LLM_MODEL", "") if not STARTUP_ERROR else "",
    }


@app.get("/api/summary")
def summary() -> dict[str, Any]:
    emails = load_emails()
    report = classification_map()
    mail_categories: dict[str, int] = {}
    attachment_categories: dict[str, int] = {}
    routed = 0
    for email in emails:
        classification = report.get(email_key(email), {})
        category = classification.get("mail_category") or "unclassified"
        mail_categories[category] = mail_categories.get(category, 0) + 1
        if classification.get("routing_labels"):
            routed += 1
        for attachment in classification.get("attachments", []) if isinstance(classification, dict) else []:
            label = attachment.get("document_category_label") or attachment.get("document_category") or "미분류"
            attachment_categories[label] = attachment_categories.get(label, 0) + 1

    try:
        with _qdrant_lock:
            qdrant = {
                "emails": qdrant_count(legacy_index.EMAIL_COLLECTION),
                "attachments": qdrant_count(legacy_index.ATTACHMENT_COLLECTION),
            }
    except Exception as exc:  # noqa: BLE001
        qdrant = {"emails": None, "attachments": None, "status": str(exc)}

    return {
        "email_count": len(emails),
        "attachment_count": sum(len(email.get("attachments", []) or []) for email in emails),
        "classified_count": len(report),
        "routed_count": routed,
        "duplicate_risk_count": max(0, len(emails) - len({email.get("gmail_thread_id") or email.get("subject") for email in emails})),
        "mail_categories": mail_categories,
        "attachment_categories": attachment_categories,
        "qdrant_points": qdrant,
        "report_path": str(report_path()) if report_path().exists() else None,
    }


@app.get("/api/emails")
def emails(
    q: str = "",
    category: str = "",
    limit: Annotated[int, Query(ge=1, le=300)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    all_emails = load_emails()
    report = classification_map()
    rows = []
    needle = q.casefold().strip()
    for index, email in enumerate(all_emails):
        classification = report.get(email_key(email), {})
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
        if category and classification.get("mail_category") != category:
            continue
        rows.append(compact_email(email, index, classification))
    return {"total": len(rows), "items": rows[offset : offset + limit]}


@app.get("/api/emails/{email_index}")
def email_detail(email_index: int) -> dict[str, Any]:
    all_emails = load_emails()
    if email_index < 0 or email_index >= len(all_emails):
        raise HTTPException(status_code=404, detail="이메일을 찾을 수 없습니다.")
    email = all_emails[email_index]
    return {"item": compact_email(email, email_index, classification_map().get(email_key(email), {}))}


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
    all_emails = load_emails()
    selected: dict[str, Any] | None = None
    if request.email_uid:
        selected = next((email for email in all_emails if email.get("email_uid") == request.email_uid), None)
    elif request.email_index is not None and request.email_index < len(all_emails):
        selected = all_emails[request.email_index]
    if selected is None:
        raise HTTPException(status_code=404, detail="대상 이메일을 찾을 수 없습니다.")

    try:
        with _qdrant_lock:
            classification = legacy_classifier.classify_email(
                selected,
                client=get_qdrant_client(),
                embed_model=get_embed_model(),
                llm=get_llm(),
                retrieval_limit=request.retrieval_limit,
            )
        return {
            "email": compact_email(selected, all_emails.index(selected)),
            "classification": dataclass_to_json(classification),
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
    args = ["--keyword", request.search_keyword, "--max", str(request.max_emails)]
    if request.date_after:
        args.extend(["--date-after", request.date_after])
    if request.date_before:
        args.extend(["--date-before", request.date_before])
    if request.sender:
        args.extend(["--sender", request.sender])
    if request.has_attachment is True:
        args.append("--has-attachment")
    return launch_legacy_script("gmail_sqlite_fetcher.py", args)
