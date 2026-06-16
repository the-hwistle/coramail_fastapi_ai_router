from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


@dataclass(frozen=True)
class SchemaInfo:
    normalized_emails: bool
    legacy_emails: bool
    normalized_analysis: bool
    legacy_classifications: bool
    routing_table: bool


_SCHEMA_CACHE_LOCK = Lock()
_SCHEMA_CACHE: dict[str, SchemaInfo] = {}


class CoramailDB:
    def __init__(self, database_url: str):
        self.database_url = database_url

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(self.database_url, row_factory=dict_row)

    def schema_info(self) -> SchemaInfo:
        with _SCHEMA_CACHE_LOCK:
            cached = _SCHEMA_CACHE.get(self.database_url)
        if cached is not None:
            return cached

        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name IN ('emails', 'attachments', 'analysis_results', 'email_classifications', 'routing_table')
                """
            ).fetchall()
        columns: dict[str, set[str]] = {}
        for row in rows:
            columns.setdefault(str(row["table_name"]), set()).add(str(row["column_name"]))
        schema = SchemaInfo(
            normalized_emails="email_id" in columns.get("emails", set()),
            legacy_emails="id" in columns.get("emails", set()),
            normalized_analysis=bool(columns.get("analysis_results")),
            legacy_classifications=bool(columns.get("email_classifications")),
            routing_table=bool(columns.get("routing_table")),
        )
        with _SCHEMA_CACHE_LOCK:
            _SCHEMA_CACHE[self.database_url] = schema
        return schema

    def data_version(self) -> str:
        schema = self.schema_info()
        with self.connect() as conn:
            if schema.normalized_emails:
                row = conn.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM emails) AS email_count,
                        (SELECT MAX(updated_at) FROM emails) AS email_updated_at,
                        (SELECT COUNT(*) FROM attachments) AS attachment_count,
                        (SELECT MAX(updated_at) FROM attachments) AS attachment_updated_at,
                        (
                            SELECT COUNT(*)
                            FROM analysis_results
                        ) AS classification_count,
                        (
                            SELECT MAX(created_at)
                            FROM analysis_results
                        ) AS classification_updated_at
                    """
                ).fetchone()
            elif schema.legacy_emails:
                row = conn.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM emails) AS email_count,
                        (SELECT MAX(updated_at) FROM emails) AS email_updated_at,
                        0 AS attachment_count,
                        NULL::timestamp AS attachment_updated_at,
                        (
                            SELECT COUNT(*)
                            FROM email_classifications
                        ) AS classification_count,
                        (
                            SELECT MAX(updated_at)
                            FROM email_classifications
                        ) AS classification_updated_at
                    """
                ).fetchone()
            else:
                return "empty"
        parts = [
            str(row.get("email_count") or 0),
            self._iso_value(row.get("email_updated_at")),
            str(row.get("attachment_count") or 0),
            self._iso_value(row.get("attachment_updated_at")),
            str(row.get("classification_count") or 0),
            self._iso_value(row.get("classification_updated_at")),
        ]
        return "|".join(parts)

    def load_emails(self) -> list[dict[str, Any]]:
        schema = self.schema_info()
        if schema.normalized_emails:
            return self._load_normalized_emails()
        if schema.legacy_emails:
            return self._load_legacy_emails()
        return []

    def load_classifications(self, email_uids: list[str] | None = None) -> dict[str, dict[str, Any]]:
        schema = self.schema_info()
        if schema.normalized_emails and schema.normalized_analysis:
            return self._load_normalized_classifications(email_uids)
        if schema.legacy_classifications:
            return self._load_legacy_classifications(email_uids)
        return {}

    def upsert_classification(self, email: dict[str, Any], classification: dict[str, Any]) -> None:
        schema = self.schema_info()
        if schema.normalized_emails and schema.normalized_analysis:
            self._upsert_normalized_classification(email, classification)
            return
        if schema.legacy_classifications:
            self._upsert_legacy_classification(email, classification)
            return
        raise RuntimeError("No classification table available in PostgreSQL.")

    def classification_store_name(self) -> str:
        schema = self.schema_info()
        if schema.normalized_emails and schema.normalized_analysis:
            return "postgresql:analysis_results"
        if schema.legacy_classifications:
            return "postgresql:email_classifications"
        return "postgresql:unavailable"

    def load_email_headers(self) -> list[dict[str, Any]]:
        schema = self.schema_info()
        if schema.normalized_emails:
            with self.connect() as conn:
                rows = conn.execute(
                    """
                    SELECT email_id, email_uid, gmail_message_id, gmail_thread_id, rfc_message_id,
                           sender_email, recipient_emails, cc_emails, subject, received_at,
                           has_attachments, raw_headers
                    FROM emails
                    ORDER BY received_at DESC NULLS LAST, created_at DESC, email_id DESC
                    """
                ).fetchall()
            return [self._map_normalized_email_row(row) for row in rows]
        if schema.legacy_emails:
            with self.connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id, email_uid, gmail_message_id, gmail_thread_id, rfc_message_id,
                           from_email, to_email, subject, date, has_attachment
                    FROM emails
                    ORDER BY id DESC
                    """
                ).fetchall()
            return [self._map_legacy_email_row(row) for row in rows]
        return []

    def load_email_detail(self, email_uid: str) -> dict[str, Any] | None:
        schema = self.schema_info()
        target_uid = str(email_uid or "").strip()
        if not target_uid:
            return None

        if schema.normalized_emails:
            with self.connect() as conn:
                email_row = conn.execute(
                    """
                    SELECT email_id, email_uid, gmail_message_id, gmail_thread_id, rfc_message_id,
                           sender_email, recipient_emails, cc_emails, subject, received_at,
                           body_text, has_attachments, raw_headers
                    FROM emails
                    WHERE email_uid = %s
                    """,
                    (target_uid,),
                ).fetchone()
                if not email_row:
                    return None
                attachment_rows = conn.execute(
                    """
                    SELECT email_id, attachment_uid, gmail_attachment_id, filename, file_ext, file_type,
                           mime_type, file_size_bytes, content_hash, storage_path, image_caption,
                           parsed_text, parse_status, parse_error
                    FROM attachments
                    WHERE email_id = %s
                    ORDER BY attachment_id
                    """,
                    (int(email_row["email_id"]),),
                ).fetchall()
            attachments = [self._map_attachment_row(row) for row in attachment_rows]
            return self._map_normalized_email_row(email_row, body_text=email_row.get("body_text"), attachments=attachments)

        if schema.legacy_emails:
            with self.connect() as conn:
                row = conn.execute(
                    """
                    SELECT id, email_uid, gmail_message_id, gmail_thread_id, rfc_message_id,
                           from_email, to_email, subject, date, body, signature,
                           has_attachment, attachments
                    FROM emails
                    WHERE email_uid = %s
                    """,
                    (target_uid,),
                ).fetchone()
            if not row:
                return None
            return self._map_legacy_email_row(row, include_body=True, attachments=row.get("attachments"))
        return None

    def email_metrics(self) -> dict[str, int]:
        schema = self.schema_info()
        with self.connect() as conn:
            if schema.normalized_emails:
                row = conn.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM emails) AS email_count,
                        (SELECT COUNT(*) FROM attachments) AS attachment_count,
                        (
                            SELECT COUNT(*)
                            FROM analysis_results ar
                            JOIN (
                                SELECT email_id, MAX(analysis_id) AS analysis_id
                                FROM analysis_results
                                GROUP BY email_id
                            ) latest ON latest.analysis_id = ar.analysis_id
                            WHERE COALESCE(jsonb_array_length(COALESCE(ar.raw_result->'routing_labels', '[]'::jsonb)), 0) > 0
                        ) AS routed_count,
                        (
                            SELECT COUNT(*)
                            FROM analysis_results ar
                            JOIN (
                                SELECT email_id, MAX(analysis_id) AS analysis_id
                                FROM analysis_results
                                GROUP BY email_id
                            ) latest ON latest.analysis_id = ar.analysis_id
                        ) AS classified_count,
                        (
                            SELECT GREATEST(
                                COUNT(*) - COUNT(DISTINCT COALESCE(NULLIF(gmail_thread_id, ''), COALESCE(subject, ''))),
                                0
                            )
                            FROM emails
                        ) AS duplicate_risk_count
                    """
                ).fetchone()
            elif schema.legacy_emails:
                row = conn.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM emails) AS email_count,
                        0 AS attachment_count,
                        (
                            SELECT COUNT(*)
                            FROM email_classifications
                        ) AS classified_count,
                        0 AS routed_count,
                        (
                            SELECT GREATEST(
                                COUNT(*) - COUNT(DISTINCT COALESCE(NULLIF(gmail_thread_id, ''), COALESCE(subject, ''))),
                                0
                            )
                            FROM emails
                        ) AS duplicate_risk_count
                    """
                ).fetchone()
            else:
                return {
                    "email_count": 0,
                    "attachment_count": 0,
                    "classified_count": 0,
                    "routed_count": 0,
                    "duplicate_risk_count": 0,
                }
        return {
            "email_count": int(row.get("email_count") or 0),
            "attachment_count": int(row.get("attachment_count") or 0),
            "classified_count": int(row.get("classified_count") or 0),
            "routed_count": int(row.get("routed_count") or 0),
            "duplicate_risk_count": int(row.get("duplicate_risk_count") or 0),
        }

    def _load_normalized_emails(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            email_rows = conn.execute(
                """
                SELECT email_id, email_uid, gmail_message_id, gmail_thread_id, rfc_message_id,
                       sender_email, recipient_emails, cc_emails, subject, received_at,
                       body_text, has_attachments, raw_headers, created_at, updated_at
                FROM emails
                ORDER BY received_at DESC NULLS LAST, created_at DESC, email_id DESC
                """
            ).fetchall()
            email_ids = [int(row["email_id"]) for row in email_rows]
            attachment_rows = []
            if email_ids:
                attachment_rows = conn.execute(
                    """
                    SELECT email_id, attachment_uid, gmail_attachment_id, filename, file_ext, file_type,
                           mime_type, file_size_bytes, content_hash, storage_path, image_caption,
                           parsed_text, parse_status, parse_error, created_at, updated_at
                    FROM attachments
                    WHERE email_id = ANY(%s)
                    ORDER BY email_id, attachment_id
                    """,
                    (email_ids,),
                ).fetchall()

        attachments_by_email: dict[int, list[dict[str, Any]]] = {}
        for row in attachment_rows:
            attachments_by_email.setdefault(int(row["email_id"]), []).append(self._map_attachment_row(row))

        emails: list[dict[str, Any]] = []
        for row in email_rows:
            emails.append(
                self._map_normalized_email_row(
                    row,
                    body_text=row.get("body_text"),
                    attachments=attachments_by_email.get(int(row["email_id"]), []),
                )
            )
        return emails

    def _load_legacy_emails(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, email_uid, gmail_message_id, gmail_thread_id, rfc_message_id,
                       from_email, to_email, subject, date, body, signature,
                       has_attachment, attachments, created_at, updated_at
                FROM emails
                ORDER BY id DESC
                """
            ).fetchall()
        return [self._map_legacy_email_row(row, include_body=True, attachments=row.get("attachments")) for row in rows]

    def _load_normalized_classifications(self, email_uids: list[str] | None) -> dict[str, dict[str, Any]]:
        query = """
            SELECT DISTINCT ON (e.email_uid)
                   e.email_uid,
                   ar.analysis_id,
                   ar.model_name,
                   ar.prompt_version,
                   ar.summary,
                   ar.email_type,
                   ar.urgency,
                   ar.confidence,
                   ar.business_refs,
                   ar.vessel_names,
                   ar.routing_reason,
                   ar.raw_result,
                   ar.status,
                   ar.error_message,
                   rt.assignee_name,
                   rt.routing_labels AS assignee_labels
            FROM analysis_results ar
            JOIN emails e ON e.email_id = ar.email_id
            LEFT JOIN routing_table rt ON rt.assignee_id = ar.assignee_id
        """
        params: tuple[Any, ...] = ()
        if email_uids:
            query += " WHERE e.email_uid = ANY(%s)"
            params = (email_uids,)
        query += " ORDER BY e.email_uid, ar.created_at DESC, ar.analysis_id DESC"

        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()

        results: dict[str, dict[str, Any]] = {}
        for row in rows:
            raw_result = row["raw_result"] if isinstance(row["raw_result"], dict) else {}
            routing_labels = raw_result.get("routing_labels")
            if not isinstance(routing_labels, list):
                routing_labels = row["assignee_labels"] if isinstance(row["assignee_labels"], list) else []
            results[str(row["email_uid"])] = {
                **raw_result,
                "analysis_id": row["analysis_id"],
                "model_name": row["model_name"] or "",
                "prompt_version": row["prompt_version"] or "",
                "summary": row["summary"] or raw_result.get("summary") or raw_result.get("executive_summary") or "",
                "executive_summary": row["summary"] or raw_result.get("executive_summary") or raw_result.get("summary") or "",
                "mail_category": raw_result.get("mail_category") or row["email_type"] or "general",
                "urgency": row["urgency"] or raw_result.get("urgency") or "normal",
                "confidence": row["confidence"] if row["confidence"] is not None else raw_result.get("confidence", 0),
                "business_refs": row["business_refs"] if isinstance(row["business_refs"], list) else raw_result.get("business_refs", []),
                "vessel_names": row["vessel_names"] if isinstance(row["vessel_names"], list) else raw_result.get("vessel_names", []),
                "routing_reason": row["routing_reason"] or raw_result.get("routing_reason") or "",
                "routing_labels": routing_labels,
                "assignee_name": row["assignee_name"] or "",
                "status": row["status"] or "completed",
                "error_message": row["error_message"] or "",
            }
        return results

    def _load_legacy_classifications(self, email_uids: list[str] | None) -> dict[str, dict[str, Any]]:
        with self.connect() as conn:
            if email_uids:
                rows = conn.execute(
                    """
                    SELECT email_uid, classification
                    FROM email_classifications
                    WHERE email_uid = ANY(%s)
                    """,
                    (email_uids,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT email_uid, classification FROM email_classifications"
                ).fetchall()
        return {
            str(row["email_uid"]): (row["classification"] if isinstance(row["classification"], dict) else {})
            for row in rows
        }

    def _upsert_normalized_classification(self, email: dict[str, Any], classification: dict[str, Any]) -> None:
        email_uid = str(email.get("email_uid") or "")
        if not email_uid:
            raise RuntimeError("email_uid is required to save analysis results.")

        with self.connect() as conn:
            email_row = conn.execute(
                "SELECT email_id FROM emails WHERE email_uid = %s",
                (email_uid,),
            ).fetchone()
            if not email_row:
                raise RuntimeError(f"Normalized emails row not found for {email_uid}")

            summary = (
                classification.get("summary")
                or classification.get("executive_summary")
                or ""
            )
            routing_reason = classification.get("routing_reason") or "\n".join(classification.get("reasons", []))
            conn.execute(
                """
                INSERT INTO analysis_results(
                    email_id, model_name, prompt_version, summary, email_type,
                    urgency, confidence, business_refs, vessel_names,
                    routing_reason, raw_result, status
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
                    int(email_row["email_id"]),
                    str(classification.get("model_name") or "coramail-auto"),
                    str(classification.get("prompt_version") or "normalized-v1"),
                    summary,
                    str(classification.get("mail_category") or "general"),
                    str(classification.get("urgency") or "normal"),
                    classification.get("confidence"),
                    classification.get("business_refs", []),
                    classification.get("vessel_names", []),
                    routing_reason,
                    Jsonb(classification),
                    str(classification.get("status") or "completed"),
                ),
            )

    def _upsert_legacy_classification(self, email: dict[str, Any], classification: dict[str, Any]) -> None:
        email_uid = str(email.get("email_uid") or "")
        if not email_uid:
            raise RuntimeError("email_uid is required to save legacy classification.")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO email_classifications(
                    email_uid, subject, from_email, date, classification, classified_at, updated_at
                )
                VALUES(%s, %s, %s, %s, %s, now(), now())
                ON CONFLICT (email_uid) DO UPDATE SET
                    subject = EXCLUDED.subject,
                    from_email = EXCLUDED.from_email,
                    date = EXCLUDED.date,
                    classification = EXCLUDED.classification,
                    updated_at = now()
                """,
                (
                    email_uid,
                    email.get("subject", ""),
                    email.get("from", ""),
                    email.get("date", ""),
                    Jsonb(classification),
                ),
            )

    @staticmethod
    def _iso_value(value: Any) -> str:
        if hasattr(value, "isoformat"):
            return str(value.isoformat())
        return str(value or "")

    def _map_attachment_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "attachment_uid": row["attachment_uid"],
            "gmail_attachment_id": row["gmail_attachment_id"],
            "filename": row["filename"] or "",
            "content_type": row["mime_type"] or "",
            "content_hash": row["content_hash"],
            "file_ext": row["file_ext"] or "",
            "file_type": row["file_type"] or "other",
            "file_size_bytes": row["file_size_bytes"],
            "file_path": row["storage_path"] or "",
            "image_caption": row["image_caption"] or "",
            "content": row.get("parsed_text") or "",
            "parse_status": row.get("parse_status") or "pending",
            "parse_error": row.get("parse_error"),
        }

    def _map_normalized_email_row(
        self,
        row: dict[str, Any],
        body_text: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        recipients = row["recipient_emails"] or []
        return {
            "db_email_id": row["email_id"],
            "email_uid": row["email_uid"] or "",
            "gmail_message_id": row["gmail_message_id"] or "",
            "gmail_thread_id": row["gmail_thread_id"] or "",
            "rfc_message_id": row["rfc_message_id"] or "",
            "from": row["sender_email"] or "",
            "to": ", ".join(recipients) if isinstance(recipients, list) else str(recipients or ""),
            "recipient_emails": recipients if isinstance(recipients, list) else [],
            "cc_emails": row["cc_emails"] if isinstance(row["cc_emails"], list) else [],
            "subject": row["subject"] or "",
            "date": row["received_at"].isoformat() if row["received_at"] else "",
            "body": body_text or "",
            "has_attachment": bool(row["has_attachments"]),
            "attachments": attachments or [],
            "raw_headers": row["raw_headers"] or {},
        }

    def _map_legacy_email_row(
        self,
        row: dict[str, Any],
        include_body: bool = False,
        attachments: Any = None,
    ) -> dict[str, Any]:
        return {
            "db_email_id": row["id"],
            "email_uid": row["email_uid"] or "",
            "gmail_message_id": row["gmail_message_id"] or "",
            "gmail_thread_id": row["gmail_thread_id"] or "",
            "rfc_message_id": row["rfc_message_id"] or "",
            "from": row["from_email"] or "",
            "to": row["to_email"] or "",
            "subject": row["subject"] or "",
            "date": row["date"] or "",
            "body": (row.get("body") or "") if include_body else "",
            "signature": row.get("signature") or "",
            "has_attachment": bool(row["has_attachment"]),
            "attachments": attachments if isinstance(attachments, list) else [],
        }
