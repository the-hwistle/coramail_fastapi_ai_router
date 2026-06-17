from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

PROJECT_DIR = Path(__file__).resolve().parents[2]
PIPELINE_DIR = PROJECT_DIR / "pipeline"
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from create_postgres_schema import CONSTRAINT_STATEMENTS, DDL_STATEMENTS
from gmail_postgres_fetcher import database_url_from_env, redact_database_url


def table_exists(conn: psycopg.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = %s
        ) AS exists
        """,
        (table_name,),
    ).fetchone()
    return bool(row[0]) if row else False


def parse_datetime(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def normalize_list(value: Any) -> list[str]:
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
    else:
        items = []
    return [str(item).strip() for item in items if str(item).strip()]


def json_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def build_attachment_uid(email_uid: str, attachment: dict[str, Any], index: int) -> str:
    base = "|".join(
        [
            email_uid,
            str(attachment.get("gmail_attachment_id", "")),
            str(attachment.get("file_path", "")),
            str(attachment.get("filename", "")),
            str(index),
        ]
    )
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def infer_file_ext(filename: str) -> str:
    if "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower().strip()


def infer_file_type(filename: str, mime_type: str) -> str:
    ext = infer_file_ext(filename)
    mime = (mime_type or "").lower()
    if ext in {"pdf"} or "pdf" in mime:
        return "pdf"
    if ext in {"png", "jpg", "jpeg", "gif", "bmp", "webp"} or mime.startswith("image/"):
        return "image"
    if ext in {"xls", "xlsx", "csv"} or "sheet" in mime or "excel" in mime:
        return "excel"
    if ext in {"doc", "docx"} or "word" in mime:
        return "word"
    if ext in {"txt", "md"} or mime.startswith("text/"):
        return "text"
    return "other"


def create_schema_on_conn(conn: psycopg.Connection) -> None:
    for statement in DDL_STATEMENTS:
        conn.execute(statement)
    for statement in CONSTRAINT_STATEMENTS:
        try:
            conn.execute(statement)
        except psycopg.errors.DuplicateObject:
            continue


def migrate(database_url: str, *, drop_legacy: bool = False) -> dict[str, int]:
    counts = {"emails": 0, "attachments": 0, "analysis_results": 0}

    with psycopg.connect(database_url) as conn:
        legacy_emails_exists = table_exists(conn, "emails")
        legacy_classifications_exists = table_exists(conn, "email_classifications")

        legacy_emails = []
        legacy_classifications: dict[str, dict[str, Any]] = {}

        if legacy_emails_exists:
            legacy_emails = conn.execute(
                """
                SELECT id, email_uid, gmail_message_id, gmail_thread_id, rfc_message_id,
                       from_email, to_email, subject, date, body, signature,
                       has_attachment, attachments, created_at, updated_at
                FROM emails
                ORDER BY id
                """
            ).fetchall()

        if legacy_classifications_exists:
            legacy_classifications = {
                str(row[0]): row[1] or {}
                for row in conn.execute(
                    "SELECT email_uid, classification FROM email_classifications"
                ).fetchall()
            }

        with conn.transaction():
            if legacy_classifications_exists:
                conn.execute("ALTER TABLE email_classifications RENAME TO email_classifications_legacy")
            if legacy_emails_exists:
                conn.execute("ALTER TABLE emails RENAME TO emails_legacy")

            create_schema_on_conn(conn)

            email_id_map: dict[str, int] = {}

            for row in legacy_emails:
                email_uid = row[1]
                received_at = parse_datetime(row[8])
                body_text = row[9] or ""
                body_hash = hashlib.sha256(body_text.encode("utf-8")).hexdigest() if body_text else None
                raw_headers = {
                    "legacy_date": row[8] or "",
                    "legacy_signature": row[10] or "",
                }

                inserted = conn.execute(
                    """
                    INSERT INTO emails(
                        email_uid, gmail_message_id, gmail_thread_id, rfc_message_id,
                        sender_email, recipient_emails, subject, received_at,
                        body_text, body_hash, has_attachments, raw_headers,
                        created_at, updated_at
                    )
                    VALUES(
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s
                    )
                    ON CONFLICT (email_uid) DO UPDATE SET
                        gmail_message_id = EXCLUDED.gmail_message_id,
                        gmail_thread_id = EXCLUDED.gmail_thread_id,
                        rfc_message_id = EXCLUDED.rfc_message_id,
                        sender_email = EXCLUDED.sender_email,
                        recipient_emails = EXCLUDED.recipient_emails,
                        subject = EXCLUDED.subject,
                        received_at = EXCLUDED.received_at,
                        body_text = EXCLUDED.body_text,
                        body_hash = EXCLUDED.body_hash,
                        has_attachments = EXCLUDED.has_attachments,
                        raw_headers = EXCLUDED.raw_headers,
                        updated_at = EXCLUDED.updated_at
                    RETURNING email_id
                    """,
                    (
                        email_uid,
                        row[2] or None,
                        row[3] or None,
                        row[4] or None,
                        row[5] or None,
                        normalize_list(row[6]),
                        row[7] or "",
                        received_at,
                        body_text,
                        body_hash,
                        bool(row[11]),
                        Jsonb(raw_headers),
                        row[13],
                        row[14],
                    ),
                ).fetchone()
                email_id = int(inserted[0])
                email_id_map[email_uid] = email_id
                counts["emails"] += 1

                attachments = row[12] if isinstance(row[12], list) else []
                for index, attachment in enumerate(attachments):
                    if not isinstance(attachment, dict):
                        continue
                    filename = str(attachment.get("filename", "")).strip()
                    mime_type = str(attachment.get("content_type", "")).strip() or None
                    parsed_text = json_text(attachment.get("content"))
                    conn.execute(
                        """
                        INSERT INTO attachments(
                            attachment_uid, email_id, gmail_attachment_id, filename,
                            file_ext, file_type, mime_type, storage_path,
                            parsed_text, parse_status, created_at, updated_at
                        )
                        VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
                        ON CONFLICT (attachment_uid) DO NOTHING
                        """,
                        (
                            build_attachment_uid(email_uid, attachment, index),
                            email_id,
                            attachment.get("gmail_attachment_id"),
                            filename,
                            infer_file_ext(filename),
                            infer_file_type(filename, mime_type or ""),
                            mime_type,
                            attachment.get("file_path"),
                            parsed_text or None,
                            "parsed" if parsed_text else "pending",
                        ),
                    )
                    counts["attachments"] += 1

            for email_uid, classification in legacy_classifications.items():
                email_id = email_id_map.get(email_uid)
                if not email_id:
                    continue
                if not isinstance(classification, dict):
                    classification = {}
                conn.execute(
                    """
                    INSERT INTO analysis_results(
                        email_id, model_name, prompt_version, summary,
                        email_type, urgency, confidence, business_refs,
                        vessel_names, routing_reason, raw_result, status
                    )
                    VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (email_id, model_name, prompt_version) DO UPDATE SET
                        summary = EXCLUDED.summary,
                        email_type = EXCLUDED.email_type,
                        urgency = EXCLUDED.urgency,
                        confidence = EXCLUDED.confidence,
                        business_refs = EXCLUDED.business_refs,
                        vessel_names = EXCLUDED.vessel_names,
                        routing_reason = EXCLUDED.routing_reason,
                        raw_result = EXCLUDED.raw_result,
                        status = EXCLUDED.status
                    """,
                    (
                        email_id,
                        "legacy-email-classifier",
                        "legacy-v1",
                        classification.get("summary") or "",
                        classification.get("mail_category") or "general",
                        classification.get("urgency") or "normal",
                        classification.get("confidence"),
                        normalize_list(classification.get("business_refs")),
                        normalize_list(classification.get("vessel_names")),
                        json_text(classification.get("reasons")),
                        Jsonb(classification),
                        "completed",
                    ),
                )
                counts["analysis_results"] += 1

            if drop_legacy:
                if table_exists(conn, "emails_legacy"):
                    conn.execute("DROP TABLE emails_legacy")
                if table_exists(conn, "email_classifications_legacy"):
                    conn.execute("DROP TABLE email_classifications_legacy")

    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate legacy PostgreSQL tables to the normalized CoRA Mail schema.")
    parser.add_argument("--database-url", default=database_url_from_env())
    parser.add_argument("--drop-legacy", action="store_true", help="Drop legacy tables after a successful migration.")
    args = parser.parse_args()

    counts = migrate(args.database_url, drop_legacy=args.drop_legacy)
    print(
        "Migrated legacy schema in "
        f"{redact_database_url(args.database_url)} "
        f"(emails={counts['emails']}, attachments={counts['attachments']}, analysis_results={counts['analysis_results']})"
    )


if __name__ == "__main__":
    main()
