from __future__ import annotations

import os
from typing import Any

import requests
import streamlit as st


API_BASE_URL = os.getenv("CORAMAIL_API_BASE_URL", "http://127.0.0.1:8011").rstrip("/")

CATEGORY_LABELS = {
    "rfq": "문의",
    "quote": "견적",
    "order": "발주",
    "payment": "결제",
    "delivery": "납기",
    "technical": "기술",
    "general": "기타",
    "unclassified": "미분류",
}

ROUTE_LABELS = {
    "sales": "발주담당자 1",
    "quote_request": "견적담당자",
    "quote_sent": "영업담당자",
    "order": "발주담당자 2",
    "accounting": "회계담당자",
    "payment": "회계담당자",
    "logistics": "물류담당자",
    "delivery": "납기담당자",
    "engineering": "기술담당자",
    "technical": "기술담당자",
    "general": "공통메일함",
}


st.set_page_config(
    page_title="CoRA Mail AI Router Native",
    page_icon="✉️",
    layout="wide",
    initial_sidebar_state="expanded",
)


st.markdown(
    """
    <style>
      .stApp { background: #fbf9fb; color: #1b1b1d; }
      .block-container { padding-top: 1.4rem; max-width: 1480px; }
      section[data-testid="stSidebar"] {
        background: #05162b;
      }
      section[data-testid="stSidebar"] * {
        color: #fefcff;
      }
      h1, h2, h3 {
        color: #05162b;
        letter-spacing: 0;
      }
      .metric-card {
        background: #ffffff;
        border: 1px solid #d7d5d8;
        border-radius: 8px;
        padding: 18px 20px;
        min-height: 112px;
        box-shadow: 0 12px 34px rgba(5, 22, 43, .08);
      }
      .metric-label {
        color: #62666f;
        font-size: 12px;
        font-weight: 800;
        text-transform: uppercase;
      }
      .metric-value {
        color: #05162b;
        font-size: 34px;
        font-weight: 900;
        line-height: 44px;
        margin-top: 8px;
      }
      .panel {
        background: #ffffff;
        border: 1px solid #d7d5d8;
        border-radius: 8px;
        padding: 18px;
        box-shadow: 0 12px 34px rgba(5, 22, 43, .08);
      }
      .chip {
        display: inline-block;
        padding: 3px 8px;
        border-radius: 4px;
        background: #eae7ea;
        color: #05162b;
        font-size: 11px;
        font-weight: 800;
      }
      .chip-blue { background: #d8e2ff; color: #004395; }
      .chip-red { background: #ffdad6; color: #93000a; }
      .chip-gold { background: #fedeae; color: #584320; }
      .email-body {
        border-left: 3px solid #ba1a1a;
        padding-left: 16px;
        white-space: pre-wrap;
        line-height: 1.65;
        color: #272b31;
      }
      .subtle { color: #62666f; font-size: 13px; }
    </style>
    """,
    unsafe_allow_html=True,
)


def api_get(path: str, params: dict[str, Any] | None = None) -> Any:
    response = requests.get(f"{API_BASE_URL}{path}", params=params, timeout=20)
    response.raise_for_status()
    return response.json()


def api_post(path: str, payload: dict[str, Any]) -> Any:
    response = requests.post(f"{API_BASE_URL}{path}", json=payload, timeout=120)
    response.raise_for_status()
    return response.json()


@st.cache_data(ttl=5)
def load_summary() -> dict[str, Any]:
    return api_get("/api/summary")


@st.cache_data(ttl=5)
def load_emails(limit: int = 80) -> list[dict[str, Any]]:
    return api_get("/api/emails", {"limit": limit}).get("items", [])


def category_label(category: str | None) -> str:
    return CATEGORY_LABELS.get(category or "unclassified", category or "미분류")


def route_label(labels: list[str] | None) -> str:
    labels = labels or []
    if not labels:
        return "라우팅 대기"
    return ROUTE_LABELS.get(labels[0], labels[0])


def chip(text: str, kind: str = "") -> str:
    suffix = f" chip-{kind}" if kind else ""
    return f'<span class="chip{suffix}">{text}</span>'


def metric_card(label: str, value: Any) -> None:
    st.markdown(
        f"""
        <div class="metric-card">
          <div class="metric-label">{label}</div>
          <div class="metric-value">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_dashboard(summary: dict[str, Any], emails: list[dict[str, Any]]) -> None:
    st.title("Mail Classification Dashboard")
    st.caption("Real-time supply chain communication triage")

    classified_pct = round((summary.get("classified_count", 0) / summary.get("email_count", 1)) * 100)
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        metric_card("Total Mails", summary.get("email_count", 0))
    with c2:
        metric_card("AI Auto-Routed", f"{classified_pct}%")
    with c3:
        metric_card("Duplicate Risk", summary.get("duplicate_risk_count", 0))
    with c4:
        metric_card("Attachments", summary.get("attachment_count", 0))

    left, right = st.columns([2, 1], gap="large")
    rows = []
    for email in emails[:15]:
        classification = email.get("classification") or {}
        rows.append(
            {
                "Status": "AI OK" if classification.get("mail_category") else "PENDING",
                "Sender": email.get("from", ""),
                "Subject": email.get("subject", ""),
                "Classification": category_label(classification.get("mail_category")),
                "Receiver": route_label(classification.get("routing_labels")),
                "Time": str(email.get("date", ""))[:24],
            }
        )
    with left:
        st.subheader("Recent Mail Streams")
        st.dataframe(rows, use_container_width=True, hide_index=True, height=520)
    with right:
        st.subheader("Category Distribution")
        st.write({category_label(k): v for k, v in (summary.get("mail_categories") or {}).items()})
        st.subheader("Qdrant Points")
        st.json(summary.get("qdrant_points") or {})


def render_inbox(emails: list[dict[str, Any]]) -> None:
    st.title("Email Detail & AI Analysis")
    if not emails:
        st.info("표시할 이메일이 없습니다.")
        return

    options = {f"[{email['index']}] {email.get('subject') or '(no subject)'}": email for email in emails}
    selected_label = st.selectbox("Mail stream", list(options.keys()), label_visibility="collapsed")
    email = options[selected_label]
    classification = email.get("classification") or {}

    left, right = st.columns([1.05, 1], gap="large")
    with left:
        st.subheader(email.get("subject") or "(no subject)")
        st.caption(f"{email.get('from', '')} · {email.get('date', '')}")
        st.markdown(chip(category_label(classification.get("mail_category")), "red"), unsafe_allow_html=True)
        st.markdown(f'<div class="email-body">{email.get("body") or email.get("body_preview") or "본문 없음"}</div>', unsafe_allow_html=True)
        st.markdown("#### Attachments")
        for item in email.get("attachments") or []:
            st.write(f"📎 **{item.get('filename', 'attachment')}** · {item.get('content_type', '')}")

    with right:
        action1, action2 = st.columns(2)
        with action1:
            st.button("Confirm Routing", type="primary", use_container_width=True)
        with action2:
            if st.button("Classify", use_container_width=True):
                with st.spinner("LLM 분류 중..."):
                    result = api_post("/api/classify", {"email_index": email["index"], "retrieval_limit": 5})
                st.session_state["latest_classification"] = result.get("classification", {})
                st.success("분류 완료")

        shown = st.session_state.get("latest_classification") if st.session_state.get("latest_classification") else classification
        st.subheader("AI Executive Summary")
        st.write("\n".join(f"- {reason}" for reason in shown.get("reasons", [])) or "아직 분류 결과가 없습니다.")
        st.subheader("Extracted Entities")
        st.json(
            {
                "business_refs": shown.get("business_refs", []),
                "vessel_names": shown.get("vessel_names", []),
                "routing": route_label(shown.get("routing_labels")),
                "confidence": shown.get("confidence", 0),
                "attachments": shown.get("attachments", []),
            }
        )


def render_search() -> None:
    st.title("Search Knowledge")
    query = st.text_input("Natural language query", value="FM250016318 견적서의 납기와 총액을 찾아줘")
    col1, col2 = st.columns([1, 5])
    with col1:
        limit = st.selectbox("Limit", [5, 10, 20], index=0)
    with col2:
        run = st.button("Search", type="primary", use_container_width=True)
    if run:
        with st.spinner("Qdrant 검색 및 답변 생성 중..."):
            result = api_post("/api/search", {"query": query, "limit": limit, "with_answer": True})
        st.subheader("Generated Answer")
        st.write(result.get("answer") or "답변 없음")
        st.subheader("Query Intent")
        st.json(result.get("intent") or {})
        st.subheader("Evidence Results")
        for item in result.get("results") or []:
            with st.expander(f"{item.get('source')} · score {item.get('final_score')}"):
                st.write(item.get("preview") or "본문 미리보기 없음")
                st.json(item)


def render_settings(summary: dict[str, Any]) -> None:
    st.title("Workflow Settings")
    st.caption("Local LLM, Qdrant, routing rules")
    c1, c2, c3 = st.columns(3)
    with c1:
        metric_card("LLM Connection", "Ollama")
    with c2:
        qdrant = summary.get("qdrant_points") or {}
        metric_card("Vector DB", f"{qdrant.get('emails', '-')} / {qdrant.get('attachments', '-')}")
    with c3:
        metric_card("Duplicate Prevention", "0.85")

    st.subheader("Email Classification Rules")
    st.dataframe(
        [
            {"No": "01", "Condition": "quote / rfq / order", "Action": "Route To", "Target": "발주·견적 담당자"},
            {"No": "02", "Condition": "technical", "Action": "Route To", "Target": "기술 담당자"},
            {"No": "03", "Condition": "payment", "Action": "Route To", "Target": "회계 담당자"},
            {"No": "04", "Condition": "structured document", "Action": "Candidate", "Target": "자동 견적 폼"},
        ],
        use_container_width=True,
        hide_index=True,
    )


with st.sidebar:
    st.markdown("## ForgeMail AI")
    st.caption("Supply Chain Intelligence")
    page = st.radio("Navigation", ["Dashboard", "Inbox", "Search", "Settings"], label_visibility="collapsed")
    st.divider()
    st.caption(f"API: {API_BASE_URL}")
    if st.button("Refresh"):
        st.cache_data.clear()
        st.rerun()


try:
    summary_data = load_summary()
    email_items = load_emails()
except requests.RequestException as exc:
    st.error(f"FastAPI 백엔드 연결 실패: {exc}")
    st.stop()


if page == "Dashboard":
    render_dashboard(summary_data, email_items)
elif page == "Inbox":
    render_inbox(email_items)
elif page == "Search":
    render_search()
else:
    render_settings(summary_data)
