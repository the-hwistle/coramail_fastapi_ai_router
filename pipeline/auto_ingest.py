from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from email_classification import main as classify_main
from gmail_postgres_fetcher import PostgresEmailStore, fetch_and_save_emails_to_postgres
from index import stable_email_id
from index import main as reindex_main


BASE_DIR = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Gmail, rebuild Qdrant, and save LLM classifications.")
    parser.add_argument("--keyword", default="", help="Optional Gmail search keyword. Empty value fetches all messages.")
    parser.add_argument("--max", type=int, default=100)
    parser.add_argument("--date-after", default=None)
    parser.add_argument("--date-before", default=None)
    parser.add_argument("--sender", default=None)
    parser.add_argument("--has-attachment", action="store_true")
    parser.add_argument("--classification-limit", type=int, default=None)
    parser.add_argument("--retrieval-limit", type=int, default=5)
    return parser.parse_args()


def email_fingerprint(email: dict) -> str:
    attachments = email.get("attachments", []) or []
    attachment_keys = sorted(
        f"{item.get('filename', '')}|{item.get('file_path', '')}|{item.get('content_type', '')}"
        for item in attachments
    )
    payload = "\n".join(
        [
            stable_email_id(email),
            str(email.get("subject", "")),
            str(email.get("date", "")),
            str(email.get("body", "")),
            *attachment_keys,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()


def main() -> None:
    args = parse_args()
    store = PostgresEmailStore()
    try:
        previous_emails = store.load_emails()
    finally:
        store.close()
    previous_by_uid = {stable_email_id(email): email_fingerprint(email) for email in previous_emails}

    print("[1/3] Gmail 수집 및 PostgreSQL 저장 시작")
    emails = fetch_and_save_emails_to_postgres(
        search_keyword=args.keyword,
        max_emails=args.max,
        base_dir=BASE_DIR,
        date_after=args.date_after,
        date_before=args.date_before,
        sender=args.sender,
        has_attachment=args.has_attachment or None,
        mirror_json=True,
    )
    print(f"[1/3] 저장 완료: {len(emails)} emails")
    changed_emails = [
        email
        for email in emails
        if previous_by_uid.get(stable_email_id(email)) != email_fingerprint(email)
    ]
    print(f"[1/3] 신규/변경 메일 감지: {len(changed_emails)} emails")

    print("[2/3] Qdrant 재인덱싱 시작")
    if changed_emails:
        reindex_args = ["--incremental"]
        for email in changed_emails:
            email_uid = str(email.get("email_uid", "")).strip()
            if email_uid:
                reindex_args.extend(["--email-uid", email_uid])
        reindex_main(reindex_args)
    else:
        print("[2/3] 건너뜀: 신규/변경 메일이 없어 증분 인덱싱 불필요")
    print("[2/3] Qdrant 재인덱싱 완료")

    print("[3/3] LLM 분류 결과 PostgreSQL 저장 시작")
    classify_args = [
        "--retrieval-limit",
        str(args.retrieval_limit),
        "--skip-existing",
        "--with-summary",
    ]
    if args.classification_limit:
        classify_args.extend(["--limit", str(args.classification_limit)])
    for email in changed_emails:
        email_uid = str(email.get("email_uid", "")).strip()
        if email_uid:
            classify_args.extend(["--email-uid", email_uid])
    if changed_emails:
        import sys

        original_argv = sys.argv
        try:
            sys.argv = ["email_classification.py", *classify_args]
            classify_main()
        finally:
            sys.argv = original_argv
    else:
        print("[3/3] 건너뜀: 신규/변경 메일이 없어 분류 불필요")
    print("[3/3] LLM 분류 결과 PostgreSQL 저장 완료")


if __name__ == "__main__":
    main()
