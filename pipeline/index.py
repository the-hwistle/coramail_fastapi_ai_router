import hashlib
import mimetypes
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from llama_index.core import Settings
from llama_index.core.base.llms.types import ChatMessage, ImageBlock, TextBlock
from llama_index.embeddings.ollama import OllamaEmbedding

from llama_index.multi_modal_llms.ollama import OllamaMultiModal
from gmail_sqlite_fetcher import SQLiteEmailStore

try:
    import fitz
except ImportError:
    fitz = None

try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, FieldCondition, Filter, MatchValue, PointStruct, VectorParams
except ImportError as exc:
    raise SystemExit(
        "qdrant-client 패키지가 필요합니다.\n"
        "설치 예: pip install qdrant-client"
    ) from exc

# ==========================================
# 0. 저장소 및 컬렉션 설정
# ==========================================
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
EMAIL_DB_PATH = BASE_DIR / "emails.db"
QDRANT_PATH = BASE_DIR / "qdrant_storage"


EMAIL_COLLECTION = "coramail_emails"
ATTACHMENT_COLLECTION = "coramail_attachments"

LLM_MODEL = "qwen3.5:2b"
EMBEDDING_MODEL = "nomic-embed-text"
VISION_MODEL = "moondream"  # 모델 사이즈 키울 시, RAM 부족 문제 발생
VECTOR_SIZE = 768
MAX_EMBEDDING_CHARS = 6000

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}


# ==========================================
# 1. 첨부파일 유형 매핑
# ==========================================


ATTACHMENT_CATEGORY_BY_STORED_NAME = {
    # 견적서
    "0ad8b758a6_FM250015723_C0000111.pdf": ("quote", "견적서", "structured"),
    "0e08d9db22_FM250015996_C0000010.pdf": ("quote", "견적서", "structured"),
    "2fc2e9d132_FM250016341_C0000081.pdf": ("quote", "견적서", "structured"),
    "3cf78e9b6d_FM250015978_C0000018.pdf": ("quote", "견적서", "structured"),
    "4dc01ba86f_FM250016001_C0000105.pdf": ("quote", "견적서", "structured"),
    "6a5e061e34_FM250015985_C0000034.pdf": ("quote", "견적서", "structured"),
    "9e124c6b12_FM250016018_C0000036.pdf": ("quote", "견적서", "structured"),
    "019a8254bf_FM250016329_C0000137.pdf": ("quote", "견적서", "structured"),
    "24d60af01e_FM250016378_C0000137.pdf": ("quote", "견적서", "structured"),
    "26ce03283a_FM250015892_C0000018.pdf": ("quote", "견적서", "structured"),
    "33c85b65de_FM250016353_C0000552.pdf": ("quote", "견적서", "structured"),
    "77ad3d80db_FM250016038_C0000010.pdf": ("quote", "견적서", "structured"),
    "088e4484ae_FM250016318_C0000071.pdf": ("quote", "견적서", "structured"),
    "452ed4cd23_FM250016012_C0000105.pdf": ("quote", "견적서", "structured"),
    "720dd6b7a5_FM250016021_C0000105.pdf": ("quote", "견적서", "structured"),
    "876f1b7ab3_FM250016048_C0000010.pdf": ("quote", "견적서", "structured"),
    "948cf5b0f9_FM250015713_C0000071.pdf": ("quote", "견적서", "structured"),
    "974b033baf_FM250016371_C0000144.pdf": ("quote", "견적서", "structured"),
    "2725d836e1_FM250016362_C0000105.pdf": ("quote", "견적서", "structured"),
    "266885de97_FM250015719_C0000451.pdf": ("quote", "견적서", "structured"),
    "ba5459cc59_FM250016008_C0000114.pdf": ("quote", "견적서", "structured"),
    "c31d722af6_FM250016039_C0000162.pdf": ("quote", "견적서", "structured"),
    "c7600f2af6_FM250015885_C0000096.pdf": ("quote", "견적서", "structured"),
    "d25be191a3_FM250016023_C0000032.pdf": ("quote", "견적서", "structured"),
    "dbe49481f2_FM250016062_C0000105.pdf": ("quote", "견적서", "structured"),
    "e294474a4b_FM250015733_C0000371.pdf": ("quote", "견적서", "structured"),
    "eaa0bcb337_FM250016030_C0000010.pdf": ("quote", "견적서", "structured"),
    "ecb31f5ccf_FM250016134_C0000455.pdf": ("quote", "견적서", "structured"),
    "f46961486b_FM250015990_C0000065.pdf": ("quote", "견적서", "structured"),
    # 견적의뢰서
    "2bacb9d78e_FM250016126_C0000242.pdf": ("rfq", "견적의뢰서", "structured"),
    "9fe734caf4_FM250016252_C0000263.pdf": ("rfq", "견적의뢰서", "structured"),
    "23b67078e5_FM250016079_C0000197.pdf": ("rfq", "견적의뢰서", "structured"),
    "922b83eac2_FM250016046_C0000113.pdf": ("rfq", "견적의뢰서", "structured"),
    "6262b12efd_FM250016135_C0000614.pdf": ("rfq", "견적의뢰서", "structured"),
    "72641a35f6_FM250015974_C0000741.pdf": ("rfq", "견적의뢰서", "structured"),
    "731550c780_FM250015721_C0000171.pdf": ("rfq", "견적의뢰서", "structured"),
    "c09abf10fd_FM250016124_C0000369.pdf": ("rfq", "견적의뢰서", "structured"),
    "cdc0cab13c_FM250015728_C0000369.pdf": ("rfq", "견적의뢰서", "structured"),
    "e78b608e30_FM250016389_C0000105.pdf": ("rfq", "견적의뢰서", "structured"),
    "f3563e78db_FM250011627_C0000263.pdf": ("rfq", "견적의뢰서", "structured"),
    # 입금요청서 / 거래명세서
    "10bc73f238_KOPA25010102.pdf": ("payment_request", "입금요청서", "structured"),
    "17ec305b24_HC-0502-320.pdf": ("payment_request", "입금요청서", "structured"),
    "8522b77369_LS25-0131P.pdf": ("transaction_statement", "거래명세서", "structured"),
    # 스캔 PDF
    "1a8b416936_IWI182502100131_1_182502102835.pdf": ("drawing_scan", "도면 스캔본", "unstructured"),
    "7e4e91ee7c_PRIMARY_PR._REG._VALVE.pdf": ("drawing_scan", "도면 스캔본", "unstructured"),
    "bd86a8ea61_Pressure_Regulating_Valve.pdf": ("drawing_scan", "도면 스캔본", "unstructured"),
    "dc02309ce5_100135686_13800017844_11.pdf": ("drawing_scan", "도면 스캔본", "unstructured"),
    "7aba29abd0_CONTROL_DRYER_MANUAL_PAGES.pdf": ("manual_scan", "매뉴얼 스캔본", "unstructured"),
    # 이미지
    "3d48aef884_81800106800_81800106800_L_T__F_WCONTROL_V_V.JPG": ("field_photo", "현장 사진", "unstructured"),
    "8abf3232e0_81800106805_81800106805_PCB_INSTALLED_INSIDE_CONTROLLER.JPG": ("field_photo", "현장 사진", "unstructured"),
    "19dfa7fd53_Rudder_Indicator.jpg": ("field_photo", "현장 사진", "unstructured"),
    "a0b3b39f9f_81800106803_81800106803_20250211_141218.JPG": ("field_photo", "현장 사진", "unstructured"),
    "f8868def7b_81800106804_81800106804_20250211_141145.JPG": ("field_photo", "현장 사진", "unstructured"),
    "53065c7258_silver_ion_and_filter.jpg": ("document_scan", "JPG 스캔본", "unstructured"),
    "c90013c252_rehardening_filter.jpg": ("document_scan", "JPG 스캔본", "unstructured"),
    "74781109f5_81800106801_81800106801_FRONT_PCB.JPG": ("part_photo", "부품 사진", "unstructured"),
    "a8810668ff_81800106802_81800106802_BACK_PCB.JPG": ("part_photo", "부품 사진", "unstructured"),
    "b29f05675b_81800106806_81800106806_Photo.JPG": ("part_photo", "부품 사진", "unstructured"),
}





def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_subject(subject: str) -> str:
    normalized = subject.strip()
    normalized = re.sub(r"^(\s*(fw|fwd|re)\s*:\s*)+", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def parse_date_iso(date_raw: str) -> str | None:
    if not date_raw:
        return None
    try:
        return parsedate_to_datetime(date_raw).isoformat()
    except (TypeError, ValueError):
        return None


def extract_business_refs(text: str) -> list[str]:
    patterns = [
        r"\bFM\d{6,}(?:-\d+)?\b",
        r"\bC\d{6,}\b",
        r"\b[A-Z]{1,4}-?\d{6,}[A-Z]?\b",
        r"\b[A-Z]{1,4}\d{6,}[A-Z]?\b",
    ]
    refs: set[str] = set()
    for pattern in patterns:
        refs.update(re.findall(pattern, text or "", flags=re.IGNORECASE))
    return sorted(ref.upper() for ref in refs)


def extract_vessel_names(subject: str, body: str) -> list[str]:
    text = f"{subject}\n{body}"
    candidates: set[str] = set()
    for match in re.findall(r"/\s*([A-Z][A-Z0-9 ._-]{2,40})\s*/", text):
        cleaned = re.sub(r"\s+", " ", match).strip(" /")
        if cleaned and not re.match(r"^(HYUNDAI|SAMSUNG|DAEWOO|SHANGHAI|SAMHO)\b", cleaned):
            candidates.add(cleaned)
    for match in re.findall(r"Vessel\s*[:：]\s*([^\n\r/]+)", text, flags=re.IGNORECASE):
        cleaned = re.sub(r"\s+", " ", match).strip()
        if cleaned:
            candidates.add(cleaned)
    return sorted(candidates)


def infer_mail_category(subject: str, body: str) -> str:
    text = f"{subject}\n{body}"
    if "견적의뢰서" in text or "견적 요청" in text or "견적요청" in text:
        return "rfq"
    if "견적서" in text:
        return "quote"
    if "발주" in text:
        return "order"
    if "입금" in text or "송금" in text:
        return "payment"
    if "납기" in text or "입고" in text or "배송" in text:
        return "delivery"
    return "general"


def stored_name_from_attachment_path(file_path: str, filename: str) -> str:
    if file_path:
        return Path(file_path).name
    return filename


def display_filename_from_stored_name(stored_name: str) -> str:
    match = re.match(r"^[0-9a-f]{10}_(.+)$", stored_name, flags=re.IGNORECASE)
    return match.group(1) if match else stored_name


def classify_attachment(stored_name: str, filename: str, content_type: str) -> tuple[str, str, str, str]:
    category, label, form_type = ATTACHMENT_CATEGORY_BY_STORED_NAME.get(
        stored_name,
        ("unknown", "미분류", "unstructured"),
    )
    ext = Path(stored_name or filename).suffix.lower()
    if content_type.startswith("image/") or ext in IMAGE_EXTENSIONS:
        file_group = "image"
    elif content_type == "application/pdf" or ext == ".pdf":
        file_group = "pdf"
    else:
        file_group = ext.lstrip(".") or "unknown"
    return file_group, category, label, form_type


def local_data_path(stored_name: str) -> Path | None:
    candidate = DATA_DIR / stored_name
    return candidate if candidate.exists() else None


def extract_pdf_text_from_data(file_path: Path) -> str:
    if fitz is None:
        raise RuntimeError("PDF 직접 파싱에는 PyMuPDF가 필요합니다. 설치 예: pip install pymupdf")

    page_texts = []
    with fitz.open(file_path) as doc:
        for page_index, page in enumerate(doc, start=1):
            text = normalize_extracted_text(page.get_text("text"))
            if text:
                page_texts.append(f"[페이지 {page_index}]\n{text}")
    return "\n\n".join(page_texts)


def normalize_extracted_text(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_attachment_text_from_data(local_path: Path | None, file_group: str) -> tuple[str, str, str | None]:
    if local_path is None:
        return "", "missing", "data 디렉토리에서 원본 첨부파일을 찾을 수 없습니다."

    try:
        if file_group == "pdf":
            text = extract_pdf_text_from_data(local_path)
            return text, "success" if text else "empty", None if text else "PDF에서 텍스트를 찾을 수 없습니다."

        if file_group in {"txt", "md", "csv"}:
            text = normalize_extracted_text(local_path.read_text(encoding="utf-8", errors="ignore"))
            return text, "success" if text else "empty", None if text else "텍스트 파일이 비어 있습니다."

        if file_group == "image":
            return "", "skipped", None

        return "", "unsupported", f"지원하지 않는 첨부파일 유형입니다: {file_group}"
    except Exception as exc:  # noqa: BLE001
        return "", "failed", str(exc)


def split_text(text: str, chunk_size: int = 1800, overlap: int = 180) -> list[str]:
    normalized = re.sub(r"\n{3,}", "\n\n", text or "").strip()
    if not normalized:
        return []
    chunks = []
    start = 0
    while start < len(normalized):
        end = min(start + chunk_size, len(normalized))
        chunks.append(normalized[start:end])
        if end == len(normalized):
            break
        start = max(0, end - overlap)
    return chunks


def point_id(seed: str) -> str:
    # Qdrant point id는 UUID 또는 정수여야 하므로 hash를 UUID 모양으로 변환합니다.
    digest = sha256_text(seed)[:32]
    return f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"




def get_vector(embed_model: OllamaEmbedding, text: str) -> list[float]:
    last_error = None
    for attempt in range(3):
        try:
            return embed_model.get_text_embedding(text[:MAX_EMBEDDING_CHARS])
        except ConnectionError as exc:
            last_error = exc
            time.sleep(2 * (attempt + 1))
    raise last_error


def stable_email_id(email: dict[str, Any]) -> str:
    if email.get("email_uid"):
        return str(email["email_uid"])
    if email.get("gmail_message_id"):
        return f"gmail:{email['gmail_message_id']}"
    if email.get("rfc_message_id"):
        return f"rfc822:{str(email['rfc_message_id']).strip('<>')}"

    return sha256_text(
        "\n".join(
            [
                str(email.get("from", "")),
                str(email.get("to", "")),
                str(email.get("subject", "")),
                str(email.get("date", "")),
                str(email.get("body", "")),
            ]
        )
    )


def load_emails_from_sqlite(db_path: Path = EMAIL_DB_PATH) -> list[dict[str, Any]]:
    if not db_path.exists():
        raise FileNotFoundError(f"메일 SQLite DB를 찾을 수 없습니다: {db_path}")

    store = SQLiteEmailStore(db_path)
    try:
        emails = store.load_emails()
    finally:
        store.close()

    if not emails:
        raise RuntimeError(f"SQLite DB에 저장된 이메일이 없습니다: {db_path}")
    return emails


def ensure_collection(client: QdrantClient, collection_name: str) -> None:
    if client.collection_exists(collection_name):
        return
    client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
    )


def recreate_collection(client: QdrantClient, collection_name: str) -> None:
    if client.collection_exists(collection_name):
        client.delete_collection(collection_name=collection_name)
    client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
    )


def image_caption_exists(client: QdrantClient, stored_name: str) -> str | None:
    if not client.collection_exists(ATTACHMENT_COLLECTION):
        return None
    records, _ = client.scroll(
        collection_name=ATTACHMENT_COLLECTION,
        scroll_filter=Filter(
            must=[
                FieldCondition(key="stored_name", match=MatchValue(value=stored_name)),
                FieldCondition(key="chunk_type", match=MatchValue(value="image_caption")),
            ]
        ),
        limit=1,
        with_payload=True,
        with_vectors=False,
    )
    if records:
        return records[0].payload.get("caption")
    return None


def build_image_caption(vision_llm: OllamaMultiModal, img_path: Path) -> str:
    with img_path.open("rb") as f:
        image_bytes = f.read()

    mime_type = mimetypes.guess_type(img_path.name)[0] or "image/jpeg"
    prompt_text = (
        "this is a scene photo. please describe what objects, status, "
        "and features are in this picture in detailed Korean in one sentence."
    )
    message = ChatMessage(
        role="user",
        blocks=[
            TextBlock(text=prompt_text),
            ImageBlock(image=image_bytes, image_mimetype=mime_type),
        ],
    )
    chat_response = vision_llm.chat(messages=[message])
    return str(chat_response.message.content).strip()


def build_email_point(email: dict[str, Any], email_index: int, embed_model: OllamaEmbedding) -> PointStruct:
    subject = email.get("subject", "")
    body = email.get("body", "")
    from_addr = email.get("from", "")
    to_addr = email.get("to", "")
    date_raw = email.get("date", "")
    email_id = stable_email_id(email)

    attachment_ids = [
        sha256_text(f"{email_id}\n{a.get('filename', '')}\n{a.get('file_path', '')}")
        for a in email.get("attachments", [])
    ]
    subject_normalized = normalize_subject(subject)
    embedding_text = (
        f"메일 제목: {subject}\n"
        f"정규화 제목: {subject_normalized}\n"
        f"보낸 사람: {from_addr}\n"
        f"받는 사람: {to_addr}\n"
        f"본문:\n{body}\n"
        f"첨부파일: {', '.join(a.get('filename', '') for a in email.get('attachments', []))}"
    )
    payload = {
        "point_type": "email",
        "email_id": email_id,
        "email_uid": email_id,
        "gmail_message_id": email.get("gmail_message_id", ""),
        "gmail_thread_id": email.get("gmail_thread_id", ""),
        "rfc_message_id": email.get("rfc_message_id", ""),
        "email_index": email_index,
        "message_id": email.get("rfc_message_id") or email.get("gmail_message_id"),
        "thread_id": email.get("gmail_thread_id") or subject_normalized or email_id,
        "in_reply_to": None,
        "references": [],
        "from": from_addr,
        "to": to_addr,
        "subject": subject,
        "subject_normalized": subject_normalized,
        "date_raw": date_raw,
        "date_iso": parse_date_iso(date_raw),
        "raw_text": body,
        "embedding_text": embedding_text,
        "signature": email.get("signature", ""),
        "has_attachment": bool(email.get("has_attachment")),
        "attachment_count": len(email.get("attachments", [])),
        "attachment_ids": attachment_ids,
        "mail_category": infer_mail_category(subject, body),
        "business_refs": extract_business_refs(f"{subject}\n{body}"),
        "vessel_names": extract_vessel_names(subject, body),
        "content_hash": sha256_text(body),
        "embedding_model": EMBEDDING_MODEL,
        "created_at": now_iso(),
    }
    return PointStruct(
        id=point_id(f"email:{email_id}"),
        vector=get_vector(embed_model, embedding_text),
        payload=payload,
    )


def build_attachment_contexts(emails: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    contexts: dict[str, dict[str, Any]] = {}
    for email in emails:
        subject = email.get("subject", "")
        body = email.get("body", "")
        from_addr = email.get("from", "")
        to_addr = email.get("to", "")
        date_raw = email.get("date", "")
        email_id = stable_email_id(email)

        for attachment in email.get("attachments", []):
            stored_name = stored_name_from_attachment_path(
                attachment.get("file_path", ""),
                attachment.get("filename", ""),
            )
            contexts.setdefault(
                stored_name,
                {
                    "email_id": email_id,
                    "parent_email_id": email_id,
                    "parent_email_uid": email_id,
                    "parent_gmail_message_id": email.get("gmail_message_id", ""),
                    "parent_gmail_thread_id": email.get("gmail_thread_id", ""),
                    "parent_rfc_message_id": email.get("rfc_message_id", ""),
                    "filename": attachment.get("filename", ""),
                    "file_path": attachment.get("file_path", ""),
                    "content_type": attachment.get("content_type", ""),
                    "parent_subject": subject,
                    "parent_body": body,
                    "parent_email_date": parse_date_iso(date_raw),
                    "parent_email_from": from_addr,
                },
            )
    return contexts


def build_data_attachment_points(
    local_path: Path,
    context: dict[str, Any],
    embed_model: OllamaEmbedding,
    vision_llm: OllamaMultiModal,
    client: QdrantClient,
) -> list[PointStruct]:
    stored_name = local_path.name
    filename = context.get("filename") or display_filename_from_stored_name(stored_name)
    file_path = context.get("file_path") or str(local_path.relative_to(BASE_DIR))
    content_type = context.get("content_type") or mimetypes.guess_type(stored_name)[0] or ""
    attachment_id = sha256_text(f"data:{stored_name}")
    file_group, category, category_label, form_type = classify_attachment(stored_name, filename, content_type)

    caption = None
    attachment_text, parse_status, parse_error = parse_attachment_text_from_data(local_path, file_group)
    chunk_type = "ocr_text"

    if file_group == "image":
        caption = image_caption_exists(client, stored_name)
        if caption:
            print(f"  - 기존 이미지 캡션 재사용: {stored_name}")
        elif local_path:
            print(f"  - 이미지 VLM 분석 중: {stored_name}")
            try:
                caption = build_image_caption(vision_llm, local_path)
            except Exception as exc:  # noqa: BLE001
                print(f"    이미지 분석 실패: {stored_name} / {exc}")
        if caption:
            attachment_text = f"파일명: {filename} / 현장 사진 설명: {caption}"
            chunk_type = "image_caption"

    chunks = split_text(attachment_text)
    if not chunks:
        chunks = [f"파일명: {filename} / 텍스트 추출 결과 없음"]

    points: list[PointStruct] = []
    email_id = context.get("email_id")
    parent_email_id = context.get("parent_email_id") or email_id
    parent_subject = context.get("parent_subject", "")
    parent_body = context.get("parent_body", "")
    refs = sorted(set(extract_business_refs(f"{parent_subject}\n{parent_body}\n{filename}\n{attachment_text}")))
    vessels = sorted(set(extract_vessel_names(parent_subject, parent_body)))

    for chunk_index, chunk in enumerate(chunks):
        chunk_id = f"{attachment_id}:c{chunk_index:04d}"
        embedding_text = (
            f"첨부파일명: {filename}\n"
            f"문서유형: {category_label}\n"
            f"부모 메일 제목: {parent_subject}\n"
            f"본문:\n{chunk}"
        )
        payload = {
            "point_type": "attachment",
            "email_id": email_id,
            "parent_email_id": parent_email_id,
            "parent_email_uid": context.get("parent_email_uid") or parent_email_id,
            "parent_gmail_message_id": context.get("parent_gmail_message_id", ""),
            "parent_gmail_thread_id": context.get("parent_gmail_thread_id", ""),
            "parent_rfc_message_id": context.get("parent_rfc_message_id", ""),
            "attachment_id": attachment_id,
            "chunk_id": chunk_id,
            "parent_id": attachment_id,
            "filename": filename,
            "stored_name": stored_name,
            "file_path": file_path,
            "local_data_path": str(local_path) if local_path else None,
            "content_type": content_type,
            "file_group": file_group,
            "document_category": category,
            "document_category_label": category_label,
            "form_type": form_type,
            "page": None,
            "chunk_index": chunk_index,
            "chunk_type": chunk_type,
            "raw_text": chunk,
            "embedding_text": embedding_text,
            "ocr_text": attachment_text if chunk_type != "image_caption" else None,
            "caption": caption,
            "content_source": "data_dir",
            "data_parse_status": parse_status,
            "data_parse_error": parse_error,
            "extracted_fields": {},
            "line_items": [],
            "parent_email_subject": parent_subject,
            "parent_email_date": context.get("parent_email_date"),
            "parent_email_from": context.get("parent_email_from", ""),
            "business_refs": refs,
            "vessel_names": vessels,
            "processing_status": {
                "ocr": parse_status,
                "vlm_caption": "success" if caption else ("skipped" if file_group != "image" else "failed"),
                "field_extraction": "skipped",
                "error": parse_error,
            },
            "content_hash": sha256_text(chunk),
            "embedding_model": EMBEDDING_MODEL,
            "vision_model": VISION_MODEL if file_group == "image" else None,
            "created_at": now_iso(),
        }
        points.append(
            PointStruct(
                id=point_id(f"attachment:{chunk_id}"),
                vector=get_vector(embed_model, embedding_text),
                payload=payload,
            )
        )
    return points


def upsert_in_batches(client: QdrantClient, collection_name: str, points: list[PointStruct], batch_size: int = 32) -> None:
    for start in range(0, len(points), batch_size):
        batch = points[start : start + batch_size]
        client.upsert(collection_name=collection_name, points=batch)


def main() -> None:
    print("[1/4] 임베딩, 비전 모델, Qdrant 초기화 중...")
    embed_model = OllamaEmbedding(model_name=EMBEDDING_MODEL)
    Settings.embed_model = embed_model
    vision_llm = OllamaMultiModal(
        model=VISION_MODEL,
        request_timeout=1000.0,
        additional_kwargs={"keep_alive": 0},
    )

    client = QdrantClient(path=str(QDRANT_PATH))
    recreate_collection(client, EMAIL_COLLECTION)
    recreate_collection(client, ATTACHMENT_COLLECTION)

    print("[2/4] SQLite DB 로드 및 스키마 변환 중...")
    emails = load_emails_from_sqlite()
    email_points: list[PointStruct] = []
    attachment_points: list[PointStruct] = []

    print("[3/4] 메일/첨부파일 임베딩 및 Qdrant 포인트 생성 중...")
    for email_index, email in enumerate(emails):
        email_point = build_email_point(email, email_index, embed_model)
        email_points.append(email_point)

    attachment_contexts = build_attachment_contexts(emails)
    for local_path in sorted(path for path in DATA_DIR.iterdir() if path.is_file()):
        attachment_points.extend(
            build_data_attachment_points(
                local_path=local_path,
                context=attachment_contexts.get(local_path.name, {}),
                embed_model=embed_model,
                vision_llm=vision_llm,
                client=client,
            )
        )

    print("[4/4] Qdrant 로컬 DB에 영구 저장 중...")
    upsert_in_batches(client, EMAIL_COLLECTION, email_points)
    upsert_in_batches(client, ATTACHMENT_COLLECTION, attachment_points)

    print("=============================================")
    print(f"Qdrant 저장 위치: {QDRANT_PATH}")
    print(f"메일 컬렉션: {EMAIL_COLLECTION} / {len(email_points)} points")
    print(f"첨부 컬렉션: {ATTACHMENT_COLLECTION} / {len(attachment_points)} points")
    print("=============================================")


if __name__ == "__main__":
    main()
