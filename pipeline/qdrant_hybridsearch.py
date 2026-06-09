import argparse
import re
from dataclasses import dataclass, field
from typing import Any

from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.llms.ollama import Ollama
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

from index import (
    ATTACHMENT_COLLECTION,
    EMAIL_COLLECTION,
    EMAIL_DB_PATH,
    EMBEDDING_MODEL,
    LLM_MODEL,
    QDRANT_PATH,
    get_vector,
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Qdrant metadata + dense hybrid search for coramail data.")
    parser.add_argument("query", nargs="?", default="FM250016318 견적서의 납기와 총액을 찾아줘")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--intent-only", action="store_true", help="쿼리 의도 분석 결과만 출력합니다.")
    parser.add_argument("--no-answer", action="store_true", help="검색 결과만 출력하고 LLM 답변 생성을 건너뜁니다.")
    args = parser.parse_args()

    if args.intent_only:
        print_intent(analyze_query_intent(args.query))
        return

    print(f"Email source DB: {EMAIL_DB_PATH}")
    client = QdrantClient(path=str(QDRANT_PATH))
    try:
        embed_model = OllamaEmbedding(model_name=EMBEDDING_MODEL)
        intent, results = hybrid_search(client, embed_model, args.query, limit=args.limit)
        print_intent(intent)
        print_results(results)
        if not args.no_answer:
            llm = Ollama(model=LLM_MODEL, request_timeout=1000.0)
            print_answer(generate_answer(llm, args.query, results))
    finally:
        client.close()


if __name__ == "__main__":
    main()
