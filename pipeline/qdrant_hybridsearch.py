import argparse
import json
import re
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from urllib.parse import urlparse

from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.llms.ollama import Ollama
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

from index import (
    ATTACHMENT_COLLECTION,
    EMAIL_COLLECTION,
    EMAIL_DATABASE_URL,
    EMBEDDING_MODEL,
    LLM_MODEL,
    OLLAMA_BASE_URL,
    QDRANT_PATH,
    get_vector,
    load_emails_from_postgres,
)


DOCUMENT_CATEGORY_KEYWORDS = {
    "quote": ["견적서", "견적", "quotation", "quote"],
    "rfq": ["견적의뢰서", "견적 의뢰", "견적요청", "견적 요청", "rfq"],
    "payment_request": ["입금요청서", "입금 요청", "송금", "입금"],
    "transaction_statement": ["거래명세서", "거래 명세서", "명세서"],
    "drawing_scan": ["도면", "drawing"],
    "manual_scan": ["매뉴얼", "manual"],
    "field_photo": ["현장 사진", "현장사진"],
    "part_photo": ["부품 사진", "부품사진", "pcb"],
    "document_scan": ["스캔본", "스캔"],
}

REQUESTED_FIELD_KEYWORDS = {
    "delivery_date": ["납기", "입고", "배송", "delivery"],
    "total_amount": ["총액", "합계", "금액", "total", "amount"],
    "unit_price": ["단가", "unit price"],
    "quantity": ["수량", "qty", "quantity"],
    "part_no": ["품번", "part no", "part number"],
    "vessel": ["선박", "호선", "vessel"],
    "company": ["업체", "거래처", "customer", "vendor"],
}

FILE_GROUP_KEYWORDS = {
    "image": ["사진", "이미지", "jpg", "jpeg", "png", "photo", "image"],
    "pdf": ["pdf", "문서", "첨부", "파일"],
}

CHUNK_TYPE_KEYWORDS = {
    "image_caption": ["사진", "이미지", "photo", "image"],
    "ocr_text": ["문서", "pdf", "텍스트", "견적서", "견적의뢰서", "명세서"],
}

METADATA_SCORE_WEIGHTS = {
    "business_ref": 0.50,
    "document_category": 0.25,
    "file_group": 0.15,
    "chunk_type": 0.10,
    "requested_keyword": 0.08,
    "filename": 0.08,
}

EMAIL_CLASSIFICATION_LABELS = ["발주", "문의", "서비스", "기술", "기타"]

EMAIL_LABEL_KEYWORDS = {
    "발주": [
        "발주",
        "발주서",
        "주문",
        "주문서",
        "purchase order",
        "po",
        "order",
    ],
    "문의": [
        "문의",
        "견적",
        "견적서",
        "견적문의",
        "견적 요청",
        "견적요청",
        "견적 의뢰",
        "견적의뢰",
        "rfq",
        "quote",
        "quote request",
        "quotation",
        "inquiry",
        "enquiry",
    ],
    "서비스": [
        "서비스",
        "a/s",
        "as ",
        "수리",
        "정비",
        "점검",
        "클레임",
        "불량",
        "warranty",
        "service",
        "repair",
        "maintenance",
    ],
    "기술": [
        "기술",
        "도면",
        "사양",
        "스펙",
        "매뉴얼",
        "호환",
        "부품",
        "설치",
        "trouble",
        "error",
        "drawing",
        "manual",
        "spec",
        "technical",
    ],
    "기타": [],
}

EMAIL_CATEGORY_TO_LABEL = {
    "order": "발주",
    "rfq": "문의",
    "quote": "문의",
    "delivery": "서비스",
    "payment": "기타",
    "technical": "기술",
    "general": "기타",
}

LABEL_SCORE_WEIGHTS = {
    "subject_keyword": 0.42,
    "body_keyword": 0.24,
    "attachment_keyword": 0.16,
    "qdrant_payload": 0.18,
}


@dataclass
class EmailLabelClassification:
    email_uid: str
    subject: str
    label: str
    confidence: float
    scores: dict[str, float]
    reasons: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""


@dataclass
class QueryIntent:
    raw_query: str
    target_collection: str = ATTACHMENT_COLLECTION
    search_mode: str = "vector_first"
    semantic_query: str = ""
    business_refs: list[str] = field(default_factory=list)
    document_category: str | None = None
    file_group: str | None = None
    chunk_type: str | None = None
    requested_fields: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    limit_multiplier: int = 5

    @property
    def has_metadata_filter(self) -> bool:
        return any(
            [
                self.business_refs,
                self.document_category,
                self.file_group,
                self.chunk_type,
            ]
        )


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def extract_business_refs(query: str) -> list[str]:
    patterns = [
        r"\bFM\d{6,}(?:-\d+)?\b",
        r"\bC\d{6,}\b",
        r"\b[A-Z]{1,4}-?\d{6,}[A-Z]?\b",
        r"\b[A-Z]{1,4}\d{6,}[A-Z]?\b",
    ]
    refs: set[str] = set()
    for pattern in patterns:
        refs.update(re.findall(pattern, query or "", flags=re.IGNORECASE))
    return sorted(ref.upper() for ref in refs)


def find_first_keyword_match(query_lower: str, keyword_map: dict[str, list[str]]) -> str | None:
    for value, keywords in keyword_map.items():
        if any(keyword.lower() in query_lower for keyword in keywords):
            return value
    return None


def find_requested_fields(query_lower: str) -> list[str]:
    fields = []
    for field_name, keywords in REQUESTED_FIELD_KEYWORDS.items():
        if any(keyword.lower() in query_lower for keyword in keywords):
            fields.append(field_name)
    return fields


def extract_search_keywords(query_lower: str) -> list[str]:
    keywords: list[str] = []
    for field_name in find_requested_fields(query_lower):
        keywords.extend(REQUESTED_FIELD_KEYWORDS[field_name])
    document_category = find_first_keyword_match(query_lower, DOCUMENT_CATEGORY_KEYWORDS)
    if document_category:
        keywords.extend(DOCUMENT_CATEGORY_KEYWORDS[document_category])
    return sorted({keyword for keyword in keywords if keyword})


def choose_target_collection(query_lower: str) -> str:
    email_markers = ["메일", "이메일", "보낸 사람", "받는 사람", "제목", "본문", "email", "mail"]
    attachment_markers = ["첨부", "파일", "문서", "pdf", "사진", "이미지", "견적서", "견적의뢰서"]
    if any(marker in query_lower for marker in attachment_markers):
        return ATTACHMENT_COLLECTION
    if any(marker in query_lower for marker in email_markers):
        return EMAIL_COLLECTION
    return ATTACHMENT_COLLECTION


def choose_search_mode(intent: QueryIntent) -> str:
    if intent.has_metadata_filter and not intent.requested_fields and len(intent.raw_query.split()) <= 3:
        return "metadata_only"
    if intent.business_refs or intent.document_category or intent.file_group:
        return "metadata_first"
    return "vector_first"


def analyze_query_intent(query: str) -> QueryIntent:
    normalized_query = normalize_text(query)
    query_lower = normalized_query.lower()

    intent = QueryIntent(
        raw_query=normalized_query,
        target_collection=choose_target_collection(query_lower),
        semantic_query=normalized_query,
        business_refs=extract_business_refs(normalized_query),
        document_category=find_first_keyword_match(query_lower, DOCUMENT_CATEGORY_KEYWORDS),
        file_group=find_first_keyword_match(query_lower, FILE_GROUP_KEYWORDS),
        chunk_type=find_first_keyword_match(query_lower, CHUNK_TYPE_KEYWORDS),
        requested_fields=find_requested_fields(query_lower),
        keywords=extract_search_keywords(query_lower),
    )
    intent.search_mode = choose_search_mode(intent)
    return intent


def match_condition(key: str, values: list[str] | str) -> FieldCondition:
    if isinstance(values, str):
        return FieldCondition(key=key, match=MatchValue(value=values))
    if len(values) == 1:
        return FieldCondition(key=key, match=MatchValue(value=values[0]))
    return FieldCondition(key=key, match=MatchAny(any=values))


def build_qdrant_filter(intent: QueryIntent) -> Filter | None:
    must = []
    if intent.business_refs:
        must.append(match_condition("business_refs", intent.business_refs))
    if intent.document_category and intent.target_collection == ATTACHMENT_COLLECTION:
        must.append(match_condition("document_category", intent.document_category))
    if intent.file_group and intent.target_collection == ATTACHMENT_COLLECTION:
        must.append(match_condition("file_group", intent.file_group))
    if intent.chunk_type and intent.target_collection == ATTACHMENT_COLLECTION:
        must.append(match_condition("chunk_type", intent.chunk_type))
    if not must:
        return None
    return Filter(must=must)


def query_points(
    client: QdrantClient,
    collection_name: str,
    vector: list[float],
    limit: int,
    query_filter: Filter | None = None,
) -> list[Any]:
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


def metadata_only_search(client: QdrantClient, collection_name: str, query_filter: Filter, limit: int) -> list[Any]:
    records, _ = client.scroll(
        collection_name=collection_name,
        scroll_filter=query_filter,
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )
    return list(records)


def payload_text(payload: dict[str, Any]) -> str:
    parts = [
        payload.get("filename"),
        payload.get("stored_name"),
        payload.get("document_category_label"),
        payload.get("parent_email_subject"),
        payload.get("subject"),
        payload.get("raw_text"),
        payload.get("embedding_text"),
        payload.get("caption"),
    ]
    return normalize_text(" ".join(str(part) for part in parts if part)).lower()


def payload_has_business_ref(payload: dict[str, Any], business_refs: list[str]) -> bool:
    payload_refs = {str(ref).upper() for ref in payload.get("business_refs", [])}
    filename = str(payload.get("filename") or "").upper()
    stored_name = str(payload.get("stored_name") or "").upper()
    return any(ref in payload_refs or ref in filename or ref in stored_name for ref in business_refs)


def metadata_score(payload: dict[str, Any], intent: QueryIntent) -> float:
    score = 0.0
    text = payload_text(payload)

    if intent.business_refs and payload_has_business_ref(payload, intent.business_refs):
        score += METADATA_SCORE_WEIGHTS["business_ref"]
    if intent.document_category and payload.get("document_category") == intent.document_category:
        score += METADATA_SCORE_WEIGHTS["document_category"]
    if intent.file_group and payload.get("file_group") == intent.file_group:
        score += METADATA_SCORE_WEIGHTS["file_group"]
    if intent.chunk_type and payload.get("chunk_type") == intent.chunk_type:
        score += METADATA_SCORE_WEIGHTS["chunk_type"]

    matched_keywords = [keyword for keyword in intent.keywords if keyword.lower() in text]
    score += min(len(matched_keywords), 5) * METADATA_SCORE_WEIGHTS["requested_keyword"]

    if intent.business_refs:
        filename_text = f"{payload.get('filename', '')} {payload.get('stored_name', '')}".upper()
        if any(ref in filename_text for ref in intent.business_refs):
            score += METADATA_SCORE_WEIGHTS["filename"]

    return score


def vector_score(result: Any) -> float:
    score = getattr(result, "score", None)
    if isinstance(score, (int, float)):
        return float(score)
    return 0.0


def rerank_results(results: list[Any], intent: QueryIntent, limit: int) -> list[tuple[Any, float]]:
    scored_results = []
    for result in results:
        payload = getattr(result, "payload", None) or {}
        final_score = vector_score(result) + metadata_score(payload, intent)
        scored_results.append((result, final_score))
    scored_results.sort(key=lambda item: item[1], reverse=True)
    return scored_results[:limit]


def hybrid_search(
    client: QdrantClient,
    embed_model: OllamaEmbedding,
    query: str,
    limit: int = 5,
) -> tuple[QueryIntent, list[tuple[Any, float]]]:
    intent = analyze_query_intent(query)
    query_filter = build_qdrant_filter(intent)

    if intent.search_mode == "metadata_only" and query_filter is not None:
        records = metadata_only_search(client, intent.target_collection, query_filter, limit * intent.limit_multiplier)
        return intent, rerank_results(records, intent, limit)

    candidate_limit = max(limit * intent.limit_multiplier, limit)
    vector = get_vector(embed_model, intent.semantic_query)
    candidates = query_points(
        client=client,
        collection_name=intent.target_collection,
        vector=vector,
        limit=candidate_limit,
        query_filter=query_filter if intent.search_mode == "metadata_first" else None,
    )

    return intent, rerank_results(candidates, intent, limit)


def email_uid_of(email: dict[str, Any]) -> str:
    return str(
        email.get("email_uid")
        or email.get("gmail_message_id")
        or email.get("rfc_message_id")
        or ""
    )


def email_text_for_classification(email: dict[str, Any]) -> str:
    attachments = email.get("attachments", []) or []
    attachment_text = "\n".join(
        " ".join(
            str(part)
            for part in [
                attachment.get("filename", ""),
                attachment.get("content_type", ""),
                attachment.get("content", ""),
                attachment.get("image_caption", ""),
            ]
            if part
        )
        for attachment in attachments
        if isinstance(attachment, dict)
    )
    return normalize_text(
        "\n".join(
            [
                f"메일 제목: {email.get('subject', '')}",
                f"보낸 사람: {email.get('from', '')}",
                f"받는 사람: {email.get('to', '')}",
                f"본문: {email.get('body', '')}",
                f"첨부파일: {attachment_text}",
            ]
        )
    )


def keyword_hits(text: str, keywords: list[str]) -> list[str]:
    text_lower = text.casefold()
    hits = []
    for keyword in keywords:
        normalized_keyword = keyword.casefold().strip()
        if not normalized_keyword:
            continue
        if re.fullmatch(r"[a-z0-9][a-z0-9 /_-]*", normalized_keyword):
            pattern = rf"(?<![a-z0-9]){re.escape(normalized_keyword)}(?![a-z0-9])"
            if re.search(pattern, text_lower):
                hits.append(keyword)
            continue
        if normalized_keyword in text_lower:
            hits.append(keyword)
    return hits


def label_from_payload(payload: dict[str, Any]) -> str:
    mail_category = str(payload.get("mail_category") or "")
    if mail_category in EMAIL_CATEGORY_TO_LABEL:
        return EMAIL_CATEGORY_TO_LABEL[mail_category]

    document_category = str(payload.get("document_category") or "")
    if document_category in {"rfq", "quote"}:
        return "문의"
    if document_category in {"drawing_scan", "manual_scan", "field_photo", "part_photo", "document_scan"}:
        return "기술"
    if document_category in {"payment_request", "transaction_statement"}:
        return "기타"
    return ""


def add_keyword_scores(
    scores: dict[str, float],
    email: dict[str, Any],
    reasons: list[str],
) -> None:
    subject = str(email.get("subject", ""))
    body = str(email.get("body", ""))
    attachments = " ".join(
        str(attachment.get("filename", ""))
        for attachment in email.get("attachments", []) or []
        if isinstance(attachment, dict)
    )

    for label, keywords in EMAIL_LABEL_KEYWORDS.items():
        if label == "기타":
            continue

        subject_hits = keyword_hits(subject, keywords)
        body_hits = keyword_hits(body, keywords)
        attachment_hits = keyword_hits(attachments, keywords)
        if subject_hits:
            scores[label] += min(len(subject_hits), 3) * LABEL_SCORE_WEIGHTS["subject_keyword"]
        if body_hits:
            scores[label] += min(len(body_hits), 4) * LABEL_SCORE_WEIGHTS["body_keyword"]
        if attachment_hits:
            scores[label] += min(len(attachment_hits), 3) * LABEL_SCORE_WEIGHTS["attachment_keyword"]

        hits = sorted(set(subject_hits + body_hits + attachment_hits))
        if hits:
            reasons.append(f"{label}: 키워드 매칭({', '.join(hits[:5])})")


def collect_email_label_evidence(
    client: QdrantClient,
    embed_model: OllamaEmbedding,
    email: dict[str, Any],
    retrieval_limit: int = 5,
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    email_text = email_text_for_classification(email)

    if client.collection_exists(EMAIL_COLLECTION):
        vector = get_vector(embed_model, email_text)
        for result in query_points(client, EMAIL_COLLECTION, vector, retrieval_limit):
            payload = getattr(result, "payload", None) or {}
            evidence.append(
                {
                    "source": "email",
                    "score": round(vector_score(result), 4),
                    "label": label_from_payload(payload),
                    "subject": payload.get("subject", ""),
                    "mail_category": payload.get("mail_category", ""),
                    "business_refs": payload.get("business_refs", []),
                    "preview": normalize_text(str(payload.get("raw_text") or payload.get("embedding_text") or ""))[:500],
                }
            )

    email_uid = email_uid_of(email)
    if email_uid and client.collection_exists(ATTACHMENT_COLLECTION):
        records, _ = client.scroll(
            collection_name=ATTACHMENT_COLLECTION,
            scroll_filter=Filter(
                should=[
                    FieldCondition(key="parent_email_uid", match=MatchValue(value=email_uid)),
                    FieldCondition(key="parent_email_id", match=MatchValue(value=email_uid)),
                    FieldCondition(key="email_id", match=MatchValue(value=email_uid)),
                ]
            ),
            limit=max(retrieval_limit * 4, 20),
            with_payload=True,
            with_vectors=False,
        )
        for record in records:
            payload = record.payload or {}
            evidence.append(
                {
                    "source": "attachment",
                    "score": 0.0,
                    "label": label_from_payload(payload),
                    "filename": payload.get("filename", ""),
                    "document_category": payload.get("document_category", ""),
                    "document_category_label": payload.get("document_category_label", ""),
                    "business_refs": payload.get("business_refs", []),
                    "preview": normalize_text(str(payload.get("raw_text") or payload.get("caption") or ""))[:500],
                }
            )
    return evidence


def add_evidence_scores(scores: dict[str, float], evidence: list[dict[str, Any]], reasons: list[str]) -> None:
    for item in evidence:
        label = str(item.get("label") or "")
        if label not in scores or label == "기타":
            continue
        vector_component = item.get("score") if isinstance(item.get("score"), (int, float)) else 0.0
        scores[label] += LABEL_SCORE_WEIGHTS["qdrant_payload"] + max(0.0, min(float(vector_component), 1.0)) * 0.12
        source = item.get("filename") or item.get("subject") or item.get("source")
        reasons.append(f"{label}: Qdrant 근거 매칭({source})")


def choose_email_label(scores: dict[str, float]) -> tuple[str, float]:
    sorted_scores = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    top_label, top_score = sorted_scores[0]
    second_score = sorted_scores[1][1] if len(sorted_scores) > 1 else 0.0

    if top_score <= 0:
        return "기타", 0.4
    if top_label != "기타" and top_score < 0.35:
        return "기타", 0.45

    margin = top_score - second_score
    confidence = 0.5 + min(margin, 1.0) * 0.35 + min(top_score, 1.0) * 0.15
    return top_label, round(max(0.0, min(confidence, 0.98)), 2)


def generate_executive_summary(
    llm: Ollama,
    email: dict[str, Any],
    classification: EmailLabelClassification,
) -> str:
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
{classification.label}

[제목]
{email.get('subject', '')}

[발신자]
{email.get('from', '')}

[본문]
{str(email.get('body', ''))[:1800]}
""".strip()
    return "\n".join(line.strip() for line in str(llm.complete(prompt)).splitlines() if line.strip())


def classify_email_label(
    email: dict[str, Any],
    client: QdrantClient,
    embed_model: OllamaEmbedding,
    llm: Ollama | None = None,
    retrieval_limit: int = 5,
    use_qdrant_evidence: bool = True,
) -> EmailLabelClassification:
    scores = {label: 0.0 for label in EMAIL_CLASSIFICATION_LABELS}
    reasons: list[str] = []

    add_keyword_scores(scores, email, reasons)
    if use_qdrant_evidence:
        try:
            evidence = collect_email_label_evidence(client, embed_model, email, retrieval_limit=retrieval_limit)
        except Exception as exc:  # noqa: BLE001
            evidence = []
            reasons.append(f"Qdrant/Ollama 근거 수집 실패: {exc}")
    else:
        evidence = []
        reasons.append("Ollama 연결 불가로 Qdrant 벡터 근거 수집 생략")
    add_evidence_scores(scores, evidence, reasons)

    label, confidence = choose_email_label(scores)
    classification = EmailLabelClassification(
        email_uid=email_uid_of(email),
        subject=str(email.get("subject", "")),
        label=label,
        confidence=confidence,
        scores={key: round(value, 4) for key, value in scores.items()},
        reasons=sorted(set(reasons))[:12] or ["분류 신호가 약해 기타로 분류"],
        evidence=evidence[:retrieval_limit],
    )

    if llm is not None:
        try:
            classification.summary = generate_executive_summary(llm, email, classification)
        except Exception as exc:  # noqa: BLE001
            classification.reasons.append(f"executive summary 생성 실패: {exc}")
    return classification


def classify_email_labels(
    emails: list[dict[str, Any]],
    client: QdrantClient,
    embed_model: OllamaEmbedding,
    llm: Ollama | None = None,
    limit: int | None = None,
    retrieval_limit: int = 5,
    use_qdrant_evidence: bool = True,
    progress: bool = False,
) -> list[EmailLabelClassification]:
    selected = emails if limit is None else emails[:limit]
    classifications = []
    total = len(selected)
    for index, email in enumerate(selected, start=1):
        if progress:
            print(f"[{index}/{total}] classify: {str(email.get('subject', ''))[:90]}", flush=True)
        classification = classify_email_label(
            email,
            client=client,
            embed_model=embed_model,
            llm=llm,
            retrieval_limit=retrieval_limit,
            use_qdrant_evidence=use_qdrant_evidence,
        )
        classifications.append(classification)
        if progress:
            summary_state = " summary" if classification.summary else ""
            print(
                f"[{index}/{total}] done: label={classification.label} "
                f"confidence={classification.confidence:.2f}{summary_state}",
                flush=True,
            )
    return classifications


def print_intent(intent: QueryIntent) -> None:
    print("=============================================")
    print("Query Intent")
    print(f"- search_mode: {intent.search_mode}")
    print(f"- target_collection: {intent.target_collection}")
    print(f"- business_refs: {intent.business_refs}")
    print(f"- document_category: {intent.document_category}")
    print(f"- file_group: {intent.file_group}")
    print(f"- chunk_type: {intent.chunk_type}")
    print(f"- requested_fields: {intent.requested_fields}")
    print("=============================================")


def print_results(results: list[tuple[Any, float]]) -> None:
    for rank, (result, final_score) in enumerate(results, start=1):
        payload = getattr(result, "payload", None) or {}
        print(
            f"{rank}. final_score={final_score:.4f} / "
            f"vector_score={vector_score(result):.4f} / "
            f"{payload.get('document_category_label') or payload.get('mail_category')} / "
            f"{payload.get('filename') or payload.get('subject')} / "
            f"{payload.get('parent_email_subject') or payload.get('from')}"
        )
        raw_text = normalize_text(str(payload.get("raw_text") or payload.get("caption") or ""))
        if raw_text:
            print(f"   preview: {raw_text[:180]}")


def result_source_label(payload: dict[str, Any]) -> str:
    return str(
        payload.get("filename")
        or payload.get("stored_name")
        or payload.get("subject")
        or payload.get("chunk_id")
        or "unknown"
    )


def build_answer_context(results: list[tuple[Any, float]], max_chars: int = 9000) -> str:
    context_parts = []
    used_chars = 0

    for rank, (result, final_score) in enumerate(results, start=1):
        payload = getattr(result, "payload", None) or {}
        source = result_source_label(payload)
        title = payload.get("parent_email_subject") or payload.get("subject") or ""
        text = normalize_text(str(payload.get("raw_text") or payload.get("caption") or payload.get("embedding_text") or ""))
        if not text:
            continue

        part = (
            f"[검색결과 {rank}]\n"
            f"source: {source}\n"
            f"score: {final_score:.4f}\n"
            f"title: {title}\n"
            f"content:\n{text}\n"
        )
        remaining = max_chars - used_chars
        if remaining <= 0:
            break
        if len(part) > remaining:
            part = part[:remaining]
        context_parts.append(part)
        used_chars += len(part)

    return "\n\n".join(context_parts)


def generate_answer(llm: Ollama, query: str, results: list[tuple[Any, float]]) -> str:
    context = build_answer_context(results)
    if not context:
        return "검색 결과에서 답변에 사용할 수 있는 본문을 찾지 못했습니다."

    prompt = f"""
다음 검색 결과만 근거로 사용자 질문에 답변하세요.
근거에 없는 내용은 추측하지 말고 모른다고 말하세요.
금액, 납기, 수량, 품번처럼 중요한 값은 원문 표현을 유지하세요.
마지막 줄에 참고한 source 파일명을 적으세요.

[사용자 질문]
{query}

[검색 결과]
{context}

[답변]
""".strip()
    response = llm.complete(prompt)
    return str(response).strip()


def print_answer(answer: str) -> None:
    print("=============================================")
    print("Generated Answer")
    print(answer)
    print("=============================================")


def is_ollama_available(base_url: str, timeout: float = 1.0) -> bool:
    parsed = urlparse(base_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ollama_has_model(base_url: str, model_name: str, timeout: float = 2.0) -> bool:
    if not is_ollama_available(base_url, timeout=timeout):
        return False
    request = Request(f"{base_url.rstrip('/')}/api/tags")
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return False
    models = payload.get("models", [])
    if not isinstance(models, list):
        return False
    wanted = model_name.split(":")[0]
    for item in models:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("model") or "")
        if name == model_name or name.split(":")[0] == wanted:
            return True
    return False


def print_email_label_classifications(classifications: list[EmailLabelClassification]) -> None:
    print("=============================================")
    print("Email Label Classifications")
    for index, item in enumerate(classifications, start=1):
        print(
            f"{index}. label={item.label} confidence={item.confidence:.2f} "
            f"email_uid={item.email_uid} subject={item.subject}"
        )
        print(f"   scores={item.scores}")
        if item.summary:
            print(f"   summary={item.summary}")
        if item.reasons:
            print(f"   reasons={'; '.join(item.reasons[:4])}")
    print("=============================================")


def main() -> None:
    parser = argparse.ArgumentParser(description="Qdrant metadata + dense hybrid search for coramail data.")
    parser.add_argument("query", nargs="?", default="FM250016318 견적서의 납기와 총액을 찾아줘")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--intent-only", action="store_true", help="쿼리 의도 분석 결과만 출력합니다.")
    parser.add_argument("--no-answer", action="store_true", help="검색 결과만 출력하고 LLM 답변 생성을 건너뜁니다.")
    parser.add_argument("--classify-db", action="store_true", help="PostgreSQL에 저장된 이메일을 5개 업무 레이블로 분류합니다.")
    parser.add_argument("--classification-limit", type=int, help="분류할 이메일 최대 개수입니다.")
    parser.add_argument("--retrieval-limit", type=int, default=5, help="분류 근거로 가져올 Qdrant 결과 수입니다.")
    parser.add_argument("--with-summary", action="store_true", help="분류 결과별 executive summary를 LLM으로 생성합니다.")
    parser.add_argument("--output-json", type=Path, help="분류 결과를 JSON 파일로 저장합니다.")
    args = parser.parse_args()

    if args.intent_only:
        print_intent(analyze_query_intent(args.query))
        return

    print(f"Email source database: {EMAIL_DATABASE_URL}")
    client = QdrantClient(path=str(QDRANT_PATH))
    try:
        embed_model = OllamaEmbedding(model_name=EMBEDDING_MODEL, base_url=OLLAMA_BASE_URL)
        if args.classify_db:
            emails = load_emails_from_postgres(EMAIL_DATABASE_URL)
            ollama_available = ollama_has_model(OLLAMA_BASE_URL, EMBEDDING_MODEL)
            if not ollama_available:
                print(
                    f"Ollama embedding model '{EMBEDDING_MODEL}' is unavailable at {OLLAMA_BASE_URL}; "
                    "Qdrant vector evidence and summaries will be skipped. "
                    f"Run: ollama pull {EMBEDDING_MODEL}"
                )
            llm_available = args.with_summary and ollama_has_model(OLLAMA_BASE_URL, LLM_MODEL)
            if args.with_summary and not llm_available:
                print(f"Ollama LLM model '{LLM_MODEL}' is unavailable; summaries will be skipped. Run: ollama pull {LLM_MODEL}")
            llm = (
                Ollama(
                    model=LLM_MODEL,
                    base_url=OLLAMA_BASE_URL,
                    request_timeout=120.0,
                    temperature=0.1,
                    context_window=2048,
                    thinking=False,
                    additional_kwargs={"num_predict": 160},
                )
                if llm_available
                else None
            )
            classifications = classify_email_labels(
                emails,
                client=client,
                embed_model=embed_model,
                llm=llm,
                limit=args.classification_limit,
                retrieval_limit=args.retrieval_limit,
                use_qdrant_evidence=ollama_available,
                progress=True,
            )
            print_email_label_classifications(classifications)
            if args.output_json:
                args.output_json.write_text(
                    json.dumps([asdict(item) for item in classifications], ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(f"Saved JSON report: {args.output_json}")
            return

        intent, results = hybrid_search(client, embed_model, args.query, limit=args.limit)
        print_intent(intent)
        print_results(results)
        if not args.no_answer:
            llm = Ollama(model=LLM_MODEL, base_url=OLLAMA_BASE_URL, request_timeout=1000.0)
            print_answer(generate_answer(llm, args.query, results))
    finally:
        client.close()


if __name__ == "__main__":
    main()
