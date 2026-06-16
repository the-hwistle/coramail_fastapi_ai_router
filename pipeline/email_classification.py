import argparse
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.llms.ollama import Ollama
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from gmail_postgres_fetcher import PostgresEmailStore
from index import (
    ATTACHMENT_COLLECTION,
    EMAIL_COLLECTION,
    EMAIL_DATABASE_URL,
    EMBEDDING_MODEL,
    LLM_MODEL,
    OLLAMA_BASE_URL,
    QDRANT_PATH,
    classify_attachment,
    extract_business_refs,
    extract_vessel_names,
    get_vector,
    infer_mail_category,
    load_emails_from_postgres,
    stable_email_id,
    stored_name_from_attachment_path,
)


MAIL_CATEGORIES = ["rfq", "quote", "order", "payment", "delivery", "technical", "general"]
DOCUMENT_CATEGORIES = [
    "quote",
    "rfq",
    "payment_request",
    "transaction_statement",
    "drawing_scan",
    "manual_scan",
    "field_photo",
    "part_photo",
    "document_scan",
    "unknown",
]

ROUTING_LABELS_BY_CATEGORY = {
    "rfq": ["sales", "quote_request"],
    "quote": ["sales", "quote_sent"],
    "order": ["sales", "order"],
    "payment": ["accounting", "payment"],
    "delivery": ["logistics", "delivery"],
    "technical": ["engineering", "technical"],
    "general": ["general"],
}

MAIL_CATEGORY_LABELS = {
    "rfq": "문의",
    "quote": "문의",
    "order": "발주",
    "payment": "기타",
    "delivery": "서비스",
    "technical": "기술",
    "general": "기타",
}

DEFAULT_DOCUMENT_LABELS = {
    "quote": "견적서",
    "rfq": "견적의뢰서",
    "payment_request": "입금요청서",
    "transaction_statement": "거래명세서",
    "drawing_scan": "도면 스캔본",
    "manual_scan": "매뉴얼 스캔본",
    "field_photo": "현장 사진",
    "part_photo": "부품 사진",
    "document_scan": "JPG 스캔본",
    "unknown": "미분류",
}

DEFAULT_FORM_TYPES = {
    "quote": "structured",
    "rfq": "structured",
    "payment_request": "structured",
    "transaction_statement": "structured",
    "drawing_scan": "unstructured",
    "manual_scan": "unstructured",
    "field_photo": "unstructured",
    "part_photo": "unstructured",
    "document_scan": "unstructured",
    "unknown": "unstructured",
}


@dataclass
class AttachmentClassification:
    filename: str
    stored_name: str
    file_group: str
    document_category: str
    document_category_label: str
    form_type: str
    business_refs: list[str] = field(default_factory=list)
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)


@dataclass
class EmailClassification:
    mail_category: str
    business_refs: list[str] = field(default_factory=list)
    vessel_names: list[str] = field(default_factory=list)
    has_structured_document: bool = False
    has_unstructured_document: bool = False
    attachment_categories: list[str] = field(default_factory=list)
    routing_labels: list[str] = field(default_factory=list)
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)
    attachments: list[AttachmentClassification] = field(default_factory=list)
    summary: str = ""


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def clamp_confidence(value: Any, default: float = 0.5) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return round(max(0.0, min(number, 1.0)), 2)


def clean_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned = []
    for item in value:
        text = normalize_text(str(item))
        if text:
            cleaned.append(text)
    return sorted(set(cleaned))


def email_uid_of(email: dict[str, Any]) -> str:
    return str(
        email.get("email_uid")
        or stable_email_id(email)
    )


def rule_based_attachment_classification(attachment: dict[str, Any]) -> AttachmentClassification:
    filename = attachment.get("filename", "")
    stored_name = stored_name_from_attachment_path(attachment.get("file_path", ""), filename)
    file_group, category, label, form_type = classify_attachment(
        stored_name,
        filename,
        attachment.get("content_type", ""),
    )
    refs = sorted(
        set(
            extract_business_refs(
                "\n".join(
                    [
                        filename,
                        stored_name,
                        str(attachment.get("content", "")),
                    ]
                )
            )
        )
    )
    confidence = 0.94 if category != "unknown" else 0.35
    reasons = (
        ["첨부파일명/저장명 규칙 매핑으로 문서 유형을 확정"]
        if category != "unknown"
        else ["첨부파일 규칙 매핑만으로는 문서 유형을 확정하지 못함"]
    )
    return AttachmentClassification(
        filename=filename,
        stored_name=stored_name,
        file_group=file_group,
        document_category=category if category in DOCUMENT_CATEGORIES else "unknown",
        document_category_label=label or DEFAULT_DOCUMENT_LABELS.get(category, "미분류"),
        form_type=form_type if form_type in {"structured", "unstructured"} else DEFAULT_FORM_TYPES.get(category, "unstructured"),
        business_refs=refs,
        confidence=confidence,
        reasons=reasons,
    )


def infer_mail_category_from_attachments(attachments: list[AttachmentClassification]) -> str:
    categories = {attachment.document_category for attachment in attachments}
    if "rfq" in categories:
        return "rfq"
    if "quote" in categories:
        return "quote"
    if "payment_request" in categories or "transaction_statement" in categories:
        return "payment"
    if categories & {"drawing_scan", "manual_scan", "field_photo", "part_photo", "document_scan"}:
        return "technical"
    return "general"


def build_rule_based_classification(email: dict[str, Any]) -> EmailClassification | None:
    attachments = [rule_based_attachment_classification(item) for item in email.get("attachments", [])]
    subject = str(email.get("subject", ""))
    body = str(email.get("body", ""))
    body_category = infer_mail_category(subject, body)
    attachment_category = infer_mail_category_from_attachments(attachments)
    mail_category = body_category if body_category != "general" else attachment_category
    known_attachment_count = sum(1 for attachment in attachments if attachment.document_category != "unknown")
    strong_attachment_signal = bool(attachments) and known_attachment_count == len(attachments)
    strong_text_signal = body_category != "general"
    if not strong_text_signal and not strong_attachment_signal:
        return None

    refs = set(extract_business_refs(f"{subject}\n{body}"))
    vessels = set(extract_vessel_names(subject, body))
    for attachment in attachments:
        refs.update(attachment.business_refs)

    attachment_categories = sorted(
        {
            attachment.document_category
            for attachment in attachments
            if attachment.document_category != "unknown"
        }
    )
    reasons = []
    if strong_text_signal:
        reasons.append(f"제목/본문 규칙으로 메일 카테고리 추정: {mail_category}")
    if strong_attachment_signal:
        reasons.append(f"첨부파일 {known_attachment_count}건이 규칙 매핑으로 확정되어 LLM 분류 생략")

    confidence = 0.91 if strong_text_signal and strong_attachment_signal else 0.84
    return EmailClassification(
        mail_category=mail_category,
        business_refs=sorted(refs),
        vessel_names=sorted(vessels),
        has_structured_document=any(attachment.form_type == "structured" for attachment in attachments),
        has_unstructured_document=any(attachment.form_type == "unstructured" for attachment in attachments),
        attachment_categories=attachment_categories,
        routing_labels=ROUTING_LABELS_BY_CATEGORY.get(mail_category, ROUTING_LABELS_BY_CATEGORY["general"]),
        confidence=confidence,
        reasons=reasons or ["규칙 기반 자동 분류 적용"],
        attachments=attachments,
    )


def query_points(
    client: QdrantClient,
    collection_name: str,
    vector: list[float],
    limit: int,
    query_filter: Filter | None = None,
) -> list[Any]:
    if not client.collection_exists(collection_name):
        return []
    if hasattr(client, "query_points"):
        result = client.query_points(
            collection_name=collection_name,
            query=vector,
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )
        return list(result.points)
    return client.search(
        collection_name=collection_name,
        query_vector=vector,
        query_filter=query_filter,
        limit=limit,
        with_payload=True,
    )


def generate_executive_summary(
    llm: Ollama,
    email: dict[str, Any],
    classification: EmailClassification,
) -> str:
    label = MAIL_CATEGORY_LABELS.get(classification.mail_category, classification.mail_category or "기타")
    prompt = f"""
다음 이메일을 바쁜 경영진이 20초 안에 판단할 수 있도록 한국어 executive summary로 작성하세요.
목적은 전체 맥락, 결론, 필요한 조치, 리스크/기한을 빠르게 파악하게 하는 것입니다.
메일 원문이나 분류 정보에 없는 사실은 절대 만들지 마세요.

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
{label}

[제목]
{email.get('subject', '')}

[발신자]
{email.get('from', '')}

[본문]
{str(email.get('body', ''))[:1800]}
""".strip()
    return "\n".join(line.strip() for line in str(llm.complete(prompt)).splitlines() if line.strip())


def scroll_payloads(
    client: QdrantClient,
    collection_name: str,
    query_filter: Filter,
    limit: int,
) -> list[dict[str, Any]]:
    if not client.collection_exists(collection_name):
        return []
    records, _ = client.scroll(
        collection_name=collection_name,
        scroll_filter=query_filter,
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )
    return [record.payload or {} for record in records]


def payload_preview(payload: dict[str, Any], max_chars: int = 600) -> str:
    text = str(
        payload.get("raw_text")
        or payload.get("caption")
        or payload.get("embedding_text")
        or ""
    )
    return normalize_text(text)[:max_chars]


def result_score(result: Any) -> float:
    score = getattr(result, "score", None)
    return float(score) if isinstance(score, (int, float)) else 0.0


def collect_qdrant_evidence(
    client: QdrantClient,
    embed_model: OllamaEmbedding,
    email: dict[str, Any],
    retrieval_limit: int,
) -> dict[str, Any]:
    email_id = stable_email_id(email)
    email_text = normalize_text(
        f"{email.get('subject', '')}\n{email.get('from', '')}\n{email.get('to', '')}\n{email.get('body', '')}"
    )
    vector = get_vector(embed_model, email_text)

    email_results = query_points(
        client=client,
        collection_name=EMAIL_COLLECTION,
        vector=vector,
        limit=retrieval_limit,
    )

    attachment_payloads = scroll_payloads(
        client=client,
        collection_name=ATTACHMENT_COLLECTION,
        query_filter=Filter(
            should=[
                FieldCondition(key="parent_email_uid", match=MatchValue(value=email_id)),
                FieldCondition(key="parent_email_id", match=MatchValue(value=email_id)),
                FieldCondition(key="email_id", match=MatchValue(value=email_id)),
            ]
        ),
        limit=max(retrieval_limit * 4, 20),
    )

    if not attachment_payloads and email.get("attachments"):
        attachment_query = normalize_text(
            "\n".join(
                [
                    email.get("subject", ""),
                    *(attachment.get("filename", "") for attachment in email.get("attachments", [])),
                ]
            )
        )
        attachment_results = query_points(
            client=client,
            collection_name=ATTACHMENT_COLLECTION,
            vector=get_vector(embed_model, attachment_query),
            limit=max(retrieval_limit, len(email.get("attachments", []))),
        )
        attachment_payloads = [result.payload or {} for result in attachment_results]

    return {
        "email_id": email_id,
        "email_results": [
            {
                "score": round(result_score(result), 4),
                "subject": (result.payload or {}).get("subject", ""),
                "mail_category": (result.payload or {}).get("mail_category", ""),
                "business_refs": (result.payload or {}).get("business_refs", []),
                "vessel_names": (result.payload or {}).get("vessel_names", []),
                "preview": payload_preview(result.payload or {}),
            }
            for result in email_results
        ],
        "attachment_payloads": [
            {
                "filename": payload.get("filename", ""),
                "stored_name": payload.get("stored_name", ""),
                "file_group": payload.get("file_group", ""),
                "document_category": payload.get("document_category", ""),
                "document_category_label": payload.get("document_category_label", ""),
                "form_type": payload.get("form_type", ""),
                "business_refs": payload.get("business_refs", []),
                "vessel_names": payload.get("vessel_names", []),
                "chunk_type": payload.get("chunk_type", ""),
                "preview": payload_preview(payload),
            }
            for payload in attachment_payloads
        ],
    }


def build_prompt(email: dict[str, Any], evidence: dict[str, Any]) -> str:
    attachments = []
    for attachment in email.get("attachments", []):
        filename = attachment.get("filename", "")
        file_path = attachment.get("file_path", "")
        stored_name = stored_name_from_attachment_path(file_path, filename)
        file_group, mapped_category, mapped_label, mapped_form_type = classify_attachment(
            stored_name,
            filename,
            attachment.get("content_type", ""),
        )
        attachments.append(
            {
                "filename": filename,
                "stored_name": stored_name,
                "content_type": attachment.get("content_type", ""),
                "file_group": file_group,
                "mapped_category": mapped_category,
                "mapped_label": mapped_label,
                "mapped_form_type": mapped_form_type,
            }
        )

    prompt_payload = {
        "allowed_mail_categories": MAIL_CATEGORIES,
        "allowed_document_categories": DOCUMENT_CATEGORIES,
        "routing_labels_by_category": ROUTING_LABELS_BY_CATEGORY,
        "email": {
            "subject": email.get("subject", ""),
            "from": email.get("from", ""),
            "to": email.get("to", ""),
            "date": email.get("date", ""),
            "body": normalize_text(email.get("body", ""))[:2500],
            "attachments": attachments,
        },
        "qdrant_evidence": evidence,
        "required_json_schema": {
            "mail_category": "one of allowed_mail_categories",
            "business_refs": ["string"],
            "vessel_names": ["string"],
            "routing_labels": ["string"],
            "confidence": "number between 0 and 1",
            "reasons": ["short Korean reason strings"],
            "attachments": [
                {
                    "filename": "string",
                    "stored_name": "string",
                    "file_group": "string",
                    "document_category": "one of allowed_document_categories",
                    "document_category_label": "Korean label",
                    "form_type": "structured or unstructured",
                    "business_refs": ["string"],
                    "confidence": "number between 0 and 1",
                    "reasons": ["short Korean reason strings"],
                }
            ],
        },
    }

    return (
        "너는 선박 기자재 거래 이메일을 분류하는 업무 시스템이다.\n"
        "이메일 원문과 Qdrant 검색 근거만 사용해서 메일과 첨부파일을 분류하라.\n"
        "추측이 필요한 경우 confidence를 낮추고 reasons에 불확실성을 적어라.\n"
        "반드시 JSON 객체만 출력하라. 마크다운 코드블록은 쓰지 마라.\n\n"
        f"{json.dumps(prompt_payload, ensure_ascii=False, indent=2)}"
    )


def parse_llm_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def fallback_attachment_classification(attachment: dict[str, Any], evidence: dict[str, Any]) -> AttachmentClassification:
    filename = attachment.get("filename", "")
    stored_name = stored_name_from_attachment_path(attachment.get("file_path", ""), filename)
    matched_payload = next(
        (
            payload
            for payload in evidence.get("attachment_payloads", [])
            if payload.get("stored_name") == stored_name or payload.get("filename") == filename
        ),
        {},
    )
    file_group, category, label, form_type = classify_attachment(
        stored_name,
        filename,
        attachment.get("content_type", ""),
    )
    category = matched_payload.get("document_category") or category
    label = matched_payload.get("document_category_label") or label
    form_type = matched_payload.get("form_type") or form_type
    refs = sorted(set(extract_business_refs(f"{filename}\n{stored_name}") + clean_string_list(matched_payload.get("business_refs"))))

    return AttachmentClassification(
        filename=filename,
        stored_name=stored_name,
        file_group=matched_payload.get("file_group") or file_group,
        document_category=category if category in DOCUMENT_CATEGORIES else "unknown",
        document_category_label=label or DEFAULT_DOCUMENT_LABELS.get(category, "미분류"),
        form_type=form_type if form_type in {"structured", "unstructured"} else DEFAULT_FORM_TYPES.get(category, "unstructured"),
        business_refs=refs,
        confidence=0.55 if category != "unknown" else 0.25,
        reasons=["LLM 응답에서 첨부 분류가 누락되어 Qdrant payload/파일명 매핑으로 보완"],
    )


def build_classification_from_llm(
    email: dict[str, Any],
    evidence: dict[str, Any],
    llm_data: dict[str, Any],
) -> EmailClassification:
    mail_category = str(llm_data.get("mail_category") or "general")
    if mail_category not in MAIL_CATEGORIES:
        mail_category = "general"

    llm_attachments = llm_data.get("attachments") if isinstance(llm_data.get("attachments"), list) else []
    by_filename = {
        str(item.get("filename", "")): item
        for item in llm_attachments
        if isinstance(item, dict) and item.get("filename")
    }
    by_stored_name = {
        str(item.get("stored_name", "")): item
        for item in llm_attachments
        if isinstance(item, dict) and item.get("stored_name")
    }

    attachments = []
    for attachment in email.get("attachments", []):
        filename = attachment.get("filename", "")
        stored_name = stored_name_from_attachment_path(attachment.get("file_path", ""), filename)
        item = by_stored_name.get(stored_name) or by_filename.get(filename)
        if not isinstance(item, dict):
            attachments.append(fallback_attachment_classification(attachment, evidence))
            continue

        file_group, mapped_category, mapped_label, mapped_form_type = classify_attachment(
            stored_name,
            filename,
            attachment.get("content_type", ""),
        )
        category = str(item.get("document_category") or mapped_category)
        if category not in DOCUMENT_CATEGORIES:
            category = "unknown"
        label = str(item.get("document_category_label") or mapped_label or DEFAULT_DOCUMENT_LABELS[category])
        form_type = str(item.get("form_type") or mapped_form_type or DEFAULT_FORM_TYPES[category])
        if form_type not in {"structured", "unstructured"}:
            form_type = DEFAULT_FORM_TYPES[category]

        attachments.append(
            AttachmentClassification(
                filename=filename,
                stored_name=stored_name,
                file_group=str(item.get("file_group") or file_group),
                document_category=category,
                document_category_label=label,
                form_type=form_type,
                business_refs=clean_string_list(item.get("business_refs")),
                confidence=clamp_confidence(item.get("confidence"), 0.6),
                reasons=clean_string_list(item.get("reasons")) or ["LLM classified from Qdrant evidence"],
            )
        )

    refs = set(extract_business_refs(f"{email.get('subject', '')}\n{email.get('body', '')}"))
    refs.update(clean_string_list(llm_data.get("business_refs")))
    for attachment in attachments:
        refs.update(attachment.business_refs)

    vessels = set(extract_vessel_names(email.get("subject", ""), email.get("body", "")))
    vessels.update(clean_string_list(llm_data.get("vessel_names")))

    attachment_categories = sorted(
        {
            attachment.document_category
            for attachment in attachments
            if attachment.document_category != "unknown"
        }
    )
    has_structured = any(attachment.form_type == "structured" for attachment in attachments)
    has_unstructured = any(attachment.form_type == "unstructured" for attachment in attachments)
    routing_labels = clean_string_list(llm_data.get("routing_labels")) or ROUTING_LABELS_BY_CATEGORY[mail_category]

    reasons = clean_string_list(llm_data.get("reasons"))
    if not reasons:
        reasons = ["LLM classified using Qdrant email and attachment evidence"]
    reasons.append(
        f"qdrant evidence: emails={len(evidence.get('email_results', []))}, "
        f"attachments={len(evidence.get('attachment_payloads', []))}"
    )

    return EmailClassification(
        mail_category=mail_category,
        business_refs=sorted(refs),
        vessel_names=sorted(vessels),
        has_structured_document=has_structured,
        has_unstructured_document=has_unstructured,
        attachment_categories=attachment_categories,
        routing_labels=routing_labels,
        confidence=clamp_confidence(llm_data.get("confidence"), 0.6),
        reasons=reasons,
        attachments=attachments,
    )


def classify_email(
    email: dict[str, Any],
    client: QdrantClient,
    embed_model: OllamaEmbedding,
    llm: Ollama,
    retrieval_limit: int,
    with_summary: bool = False,
) -> EmailClassification:
    fast_path = build_rule_based_classification(email)
    if fast_path is not None:
        classification = fast_path
    else:
        evidence = collect_qdrant_evidence(client, embed_model, email, retrieval_limit)
        response = llm.complete(build_prompt(email, evidence))
        llm_data = parse_llm_json(str(response))
        classification = build_classification_from_llm(email, evidence, llm_data)

    if with_summary:
        classification.summary = generate_executive_summary(llm, email, classification)
    return classification


def classify_emails(
    emails: list[dict[str, Any]],
    client: QdrantClient,
    embed_model: OllamaEmbedding,
    llm: Ollama,
    limit: int | None = None,
    retrieval_limit: int = 5,
    with_summary: bool = False,
) -> list[EmailClassification]:
    selected_emails = emails if limit is None else emails[:limit]
    classifications = []
    for index, email in enumerate(selected_emails, start=1):
        print(f"[{index}/{len(selected_emails)}] LLM 분류 중: {email.get('subject', '')[:80]}")
        classifications.append(
            classify_email(
                email,
                client,
                embed_model,
                llm,
                retrieval_limit,
                with_summary=with_summary,
            )
        )
    return classifications


def select_emails_for_classification(
    emails: list[dict[str, Any]],
    database_url: str,
    *,
    limit: int | None = None,
    email_uids: list[str] | None = None,
    skip_existing: bool = True,
) -> list[dict[str, Any]]:
    selected = emails
    if email_uids:
        wanted = {str(uid) for uid in email_uids if str(uid).strip()}
        selected = [email for email in emails if email_uid_of(email) in wanted]

    if skip_existing and selected:
        store = PostgresEmailStore(database_url)
        try:
            existing = store.load_classifications([email_uid_of(email) for email in selected])
        finally:
            store.close()
        selected = [email for email in selected if email_uid_of(email) not in existing]

    if limit is not None:
        selected = selected[:limit]
    return selected


def print_classification_report(emails: list[dict[str, Any]], classifications: list[EmailClassification]) -> None:
    for index, (email, classification) in enumerate(zip(emails, classifications), start=1):
        print("=============================================")
        print(f"[{index}] {email.get('subject', '')}")
        print(
            f"mail_category={classification.mail_category} "
            f"confidence={classification.confidence:.2f} "
            f"routing={classification.routing_labels}"
        )
        print(f"refs={classification.business_refs}")
        print(f"vessels={classification.vessel_names}")
        print(f"reasons={classification.reasons}")
        if not classification.attachments:
            print("attachments=[]")
            continue
        print("attachments:")
        for attachment in classification.attachments:
            print(
                f"- {attachment.filename} / "
                f"{attachment.document_category} / "
                f"{attachment.form_type} / "
                f"confidence={attachment.confidence:.2f}"
            )
            print(f"  refs={attachment.business_refs}")
            print(f"  reasons={attachment.reasons}")


def save_json_report(path: Path, emails: list[dict[str, Any]], classifications: list[EmailClassification]) -> None:
    report = []
    for email, classification in zip(emails, classifications):
        report.append(
            {
                "subject": email.get("subject", ""),
                "from": email.get("from", ""),
                "date": email.get("date", ""),
                "classification": asdict(classification),
            }
        )
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def save_classifications_to_postgres(
    database_url: str,
    emails: list[dict[str, Any]],
    classifications: list[EmailClassification],
) -> None:
    store = PostgresEmailStore(database_url)
    try:
        for email, classification in zip(emails, classifications):
            store.upsert_classification(email, asdict(classification))
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Classify coramail emails with Qdrant evidence and an LLM.")
    parser.add_argument("--database-url", default=EMAIL_DATABASE_URL)
    parser.add_argument("--qdrant-path", type=Path, default=QDRANT_PATH)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--retrieval-limit", type=int, default=5)
    parser.add_argument("--embedding-model", default=EMBEDDING_MODEL)
    parser.add_argument("--llm-model", default=LLM_MODEL)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--no-db-save", action="store_true")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--with-summary", action="store_true")
    parser.add_argument("--email-uid", action="append", dest="email_uids")
    args = parser.parse_args()

    emails = load_emails_from_postgres(args.database_url)
    selected_emails = select_emails_for_classification(
        emails,
        args.database_url,
        limit=args.limit,
        email_uids=args.email_uids,
        skip_existing=args.skip_existing,
    )

    if not selected_emails:
        print("No emails require classification.")
        return

    client = QdrantClient(path=str(args.qdrant_path))
    try:
        embed_model = OllamaEmbedding(model_name=args.embedding_model, base_url=OLLAMA_BASE_URL)
        llm = Ollama(model=args.llm_model, base_url=OLLAMA_BASE_URL, request_timeout=1000.0)
        classifications = classify_emails(
            selected_emails,
            client=client,
            embed_model=embed_model,
            llm=llm,
            retrieval_limit=args.retrieval_limit,
            with_summary=args.with_summary,
        )
    finally:
        client.close()

    print_classification_report(selected_emails, classifications)

    if not args.no_db_save:
        save_classifications_to_postgres(args.database_url, selected_emails, classifications)
        print("\nSaved classifications to PostgreSQL: email_classifications")

    if args.output_json:
        save_json_report(args.output_json, selected_emails, classifications)
        print(f"\nSaved JSON report: {args.output_json}")


if __name__ == "__main__":
    main()
