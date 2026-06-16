from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from email import message_from_bytes
from email.header import decode_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None


LOGGER = logging.getLogger("gmail_postgres_fetcher")
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
DEFAULT_DATABASE_URL = "postgresql://coramail:coramail@localhost:5432/coramail"
DEFAULT_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_dotenv_file(env_path: str | Path | None = None) -> Path:
    path = Path(env_path or os.getenv("CORAMAIL_ENV_FILE") or DEFAULT_ENV_PATH).expanduser().resolve()
    if not path.exists():
        return path

    raw_text = path.read_text(encoding="utf-8")
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = _strip_env_quotes(value.strip())
        os.environ.setdefault(key, value)
    return path


def _strip_env_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value


ACTIVE_ENV_PATH = load_dotenv_file()


def database_url_from_env(default: str = DEFAULT_DATABASE_URL) -> str:
    return os.getenv("CORAMAIL_DATABASE_URL") or os.getenv("DATABASE_URL") or default


def redact_database_url(database_url: str) -> str:
    try:
        parts = urlsplit(database_url)
    except ValueError:
        return "<invalid database url>"
    if not parts.password:
        return database_url
    username = parts.username or ""
    hostname = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{username}:***@{hostname}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@dataclass(frozen=True)
class GmailFetchConfig:
    base_dir: Path
    database_url: str
    attachments_dir: Path
    env_path: Path
    max_emails_limit: int = 100
    max_body_length: int = 3000
    api_retries: int = 3
    api_backoff_sec: int = 2
    mirror_json_path: Path | None = None

    @classmethod
    def from_base_dir(
        cls,
        base_dir: str | Path,
        *,
        database_url: str | None = None,
        attachments_name: str = "attachments",
        mirror_json_name: str | None = "emails.json",
        env_path: str | Path | None = None,
        max_emails_limit: int | None = None,
        max_body_length: int | None = None,
        api_retries: int | None = None,
        api_backoff_sec: int | None = None,
    ) -> "GmailFetchConfig":
        root = Path(base_dir).resolve()
        return cls(
            base_dir=root,
            database_url=database_url or database_url_from_env(),
            attachments_dir=root / attachments_name,
            env_path=Path(env_path or os.getenv("CORAMAIL_ENV_FILE") or ACTIVE_ENV_PATH).expanduser().resolve(),
            max_emails_limit=max_emails_limit or _env_int("FEBMAIL_MAX_EMAILS", 100, 1, 2000),
            max_body_length=max_body_length or _env_int("FEBMAIL_MAX_BODY_LENGTH", 3000, 500, 20000),
            api_retries=api_retries or _env_int("FEBMAIL_API_RETRIES", 3, 1, 10),
            api_backoff_sec=api_backoff_sec or _env_int("FEBMAIL_API_BACKOFF_SEC", 2, 1, 15),
            mirror_json_path=(root / mirror_json_name) if mirror_json_name else None,
        )


class PostgresEmailStore:
    def __init__(
        self,
        database_url: str | None = None,
        max_body_length: int = 3000,
        base_dir: str | Path | None = None,
    ):
        self.database_url = database_url or database_url_from_env()
        self.max_body_length = max_body_length
        self.base_dir = Path(base_dir).resolve() if base_dir else Path(__file__).resolve().parent
        self._conn: psycopg.Connection | None = None
        self.init_db()

    def connect(self) -> psycopg.Connection:
        if self._conn is None:
            self._conn = psycopg.connect(self.database_url, row_factory=dict_row)
        return self._conn

    def init_db(self) -> None:
        conn = self.connect()
        if self._table_exists(conn, "emails"):
            self._ensure_legacy_tables(conn)
            return
        self._create_normalized_schema()

    def replace_emails(self, emails: list[dict[str, Any]]) -> None:
        conn = self.connect()
        if self._is_normalized_schema(conn):
            with conn.transaction():
                conn.execute("DELETE FROM emails")
            self._upsert_normalized_emails(conn, emails)
            return
        with conn.transaction():
            conn.execute("DELETE FROM emails")
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO emails(
                        email_uid, gmail_message_id, gmail_thread_id, rfc_message_id,
                        from_email, to_email, subject, date, body,
                        signature, has_attachment, attachments
                    )
                    VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            email.get("email_uid") or build_email_uid(
                                email.get("gmail_message_id"),
                                email.get("rfc_message_id"),
                                email.get("from", ""),
                                email.get("to", ""),
                                email.get("subject", ""),
                                email.get("date", ""),
                                email.get("body", ""),
                            ),
                            email.get("gmail_message_id", ""),
                            email.get("gmail_thread_id", ""),
                            email.get("rfc_message_id", ""),
                            email.get("from", ""),
                            email.get("to", ""),
                            email.get("subject", ""),
                            email.get("date", ""),
                            str(email.get("body", ""))[: self.max_body_length],
                            email.get("signature", ""),
                            bool(email.get("has_attachment")),
                            Jsonb(email.get("attachments", [])),
                        )
                        for email in emails
                    ],
                )

    def upsert_emails(self, emails: list[dict[str, Any]]) -> None:
        if not emails:
            return
        conn = self.connect()
        if self._is_normalized_schema(conn):
            self._upsert_normalized_emails(conn, emails)
            return
        self._upsert_legacy_emails(conn, emails)

    def load_emails(self) -> list[dict[str, Any]]:
        return self._coramail_db().load_emails()

    def upsert_classification(self, email: dict[str, Any], classification: dict[str, Any]) -> None:
        self._coramail_db().upsert_classification(email, classification)

    def load_classifications(self, email_uids: list[str] | None = None) -> dict[str, dict[str, Any]]:
        return self._coramail_db().load_classifications(email_uids)

    def count(self) -> int:
        row = self.connect().execute("SELECT COUNT(*) AS count FROM emails").fetchone()
        return int(row["count"] if row else 0)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _coramail_db(self) -> Any:
        root_dir = Path(__file__).resolve().parent.parent
        root_str = str(root_dir)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)
        from coramail_db import CoramailDB

        return CoramailDB(self.database_url)

    def _create_normalized_schema(self) -> None:
        from create_postgres_schema import create_schema

        create_schema(self.database_url)

    def _table_exists(self, conn: psycopg.Connection, table_name: str) -> bool:
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
        return bool(row["exists"]) if row else False

    def _column_exists(self, conn: psycopg.Connection, table_name: str, column_name: str) -> bool:
        row = conn.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = %s
                  AND column_name = %s
            ) AS exists
            """,
            (table_name, column_name),
        ).fetchone()
        return bool(row["exists"]) if row else False

    def _is_normalized_schema(self, conn: psycopg.Connection) -> bool:
        return self._column_exists(conn, "emails", "email_id")

    def _ensure_legacy_tables(self, conn: psycopg.Connection) -> None:
        if self._column_exists(conn, "emails", "email_id"):
            return
        with conn.transaction():
            conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_subject ON emails(subject)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_gmail_thread_id ON emails(gmail_thread_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_date ON emails(date)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS email_classifications (
                    email_uid TEXT PRIMARY KEY,
                    subject TEXT NOT NULL DEFAULT '',
                    from_email TEXT NOT NULL DEFAULT '',
                    date TEXT NOT NULL DEFAULT '',
                    classification JSONB NOT NULL DEFAULT '{}'::jsonb,
                    classified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_email_classifications_subject ON email_classifications(subject)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_email_classifications_mail_category "
                "ON email_classifications((classification->>'mail_category'))"
            )

    def _upsert_legacy_emails(self, conn: psycopg.Connection, emails: list[dict[str, Any]]) -> None:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO emails(
                        email_uid, gmail_message_id, gmail_thread_id, rfc_message_id,
                        from_email, to_email, subject, date, body,
                        signature, has_attachment, attachments, updated_at
                    )
                    VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (email_uid) DO UPDATE SET
                        gmail_message_id = EXCLUDED.gmail_message_id,
                        gmail_thread_id = EXCLUDED.gmail_thread_id,
                        rfc_message_id = EXCLUDED.rfc_message_id,
                        from_email = EXCLUDED.from_email,
                        to_email = EXCLUDED.to_email,
                        subject = EXCLUDED.subject,
                        date = EXCLUDED.date,
                        body = EXCLUDED.body,
                        signature = EXCLUDED.signature,
                        has_attachment = EXCLUDED.has_attachment,
                        attachments = EXCLUDED.attachments,
                        updated_at = now()
                    """,
                    [
                        (
                            email.get("email_uid") or build_email_uid(
                                email.get("gmail_message_id"),
                                email.get("rfc_message_id"),
                                email.get("from", ""),
                                email.get("to", ""),
                                email.get("subject", ""),
                                email.get("date", ""),
                                email.get("body", ""),
                            ),
                            email.get("gmail_message_id", ""),
                            email.get("gmail_thread_id", ""),
                            email.get("rfc_message_id", ""),
                            email.get("from", ""),
                            email.get("to", ""),
                            email.get("subject", ""),
                            email.get("date", ""),
                            str(email.get("body", ""))[: self.max_body_length],
                            email.get("signature", ""),
                            bool(email.get("has_attachment")),
                            Jsonb(email.get("attachments", [])),
                        )
                        for email in emails
                    ],
                )

    def _upsert_normalized_emails(self, conn: psycopg.Connection, emails: list[dict[str, Any]]) -> None:
        with conn.transaction():
            for email in emails:
                email_uid = email.get("email_uid") or build_email_uid(
                    email.get("gmail_message_id"),
                    email.get("rfc_message_id"),
                    email.get("from", ""),
                    email.get("to", ""),
                    email.get("subject", ""),
                    email.get("date", ""),
                    email.get("body", ""),
                )
                sender_name, sender_email = parseaddr(str(email.get("from", "")))
                recipients = _normalize_email_list(email.get("to", ""))
                cc_emails = _normalize_email_list(email.get("cc", ""))
                received_at = _parse_email_datetime(email.get("date", ""))
                body_text = str(email.get("body", ""))[: self.max_body_length]
                body_hash = hashlib.sha256(body_text.encode("utf-8", errors="ignore")).hexdigest() if body_text else None
                attachments = email.get("attachments", []) if isinstance(email.get("attachments"), list) else []
                raw_headers = {
                    "from": str(email.get("from", "")),
                    "to": str(email.get("to", "")),
                    "cc": str(email.get("cc", "")),
                    "date": str(email.get("date", "")),
                    "subject": str(email.get("subject", "")),
                }
                inserted = conn.execute(
                    """
                    INSERT INTO emails(
                        email_uid, gmail_message_id, gmail_thread_id, rfc_message_id,
                        sender_email, sender_name, recipient_emails, cc_emails, subject,
                        received_at, body_text, body_hash, has_attachments, raw_headers,
                        fetched_at, updated_at
                    )
                    VALUES(
                        %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        now(), now()
                    )
                    ON CONFLICT (email_uid) DO UPDATE SET
                        gmail_message_id = EXCLUDED.gmail_message_id,
                        gmail_thread_id = EXCLUDED.gmail_thread_id,
                        rfc_message_id = EXCLUDED.rfc_message_id,
                        sender_email = EXCLUDED.sender_email,
                        sender_name = EXCLUDED.sender_name,
                        recipient_emails = EXCLUDED.recipient_emails,
                        cc_emails = EXCLUDED.cc_emails,
                        subject = EXCLUDED.subject,
                        received_at = EXCLUDED.received_at,
                        body_text = EXCLUDED.body_text,
                        body_hash = EXCLUDED.body_hash,
                        has_attachments = EXCLUDED.has_attachments,
                        raw_headers = EXCLUDED.raw_headers,
                        fetched_at = now(),
                        updated_at = now()
                    RETURNING email_id
                    """,
                    (
                        email_uid,
                        email.get("gmail_message_id") or None,
                        email.get("gmail_thread_id") or None,
                        email.get("rfc_message_id") or None,
                        sender_email or None,
                        sender_name or None,
                        recipients,
                        cc_emails,
                        str(email.get("subject", "")),
                        received_at,
                        body_text,
                        body_hash,
                        bool(email.get("has_attachment")),
                        Jsonb(raw_headers),
                    ),
                ).fetchone()
                email_id = int(inserted["email_id"])
                self._sync_normalized_attachments(conn, email_id, str(email_uid), attachments)

    def _sync_normalized_attachments(
        self,
        conn: psycopg.Connection,
        email_id: int,
        email_uid: str,
        attachments: list[dict[str, Any]],
    ) -> None:
        attachment_uids: list[str] = []
        for index, attachment in enumerate(attachments):
            if not isinstance(attachment, dict):
                continue
            attachment_uid = build_attachment_uid(email_uid, attachment, index)
            attachment_uids.append(attachment_uid)
            filename = str(attachment.get("filename") or "")
            mime_type = str(attachment.get("content_type") or "")
            parsed_text = str(attachment.get("content") or "")[: self.max_body_length]
            storage_path = str(attachment.get("file_path") or "")
            content_hash = _content_hash_for_attachment(attachment)
            file_size_bytes = _attachment_file_size(self.base_dir, storage_path)
            conn.execute(
                """
                INSERT INTO attachments(
                    attachment_uid, email_id, gmail_attachment_id, filename,
                    file_ext, file_type, mime_type, file_size_bytes, content_hash,
                    storage_path, image_caption, parsed_text, parse_status, updated_at
                )
                VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (attachment_uid) DO UPDATE SET
                    gmail_attachment_id = EXCLUDED.gmail_attachment_id,
                    filename = EXCLUDED.filename,
                    file_ext = EXCLUDED.file_ext,
                    file_type = EXCLUDED.file_type,
                    mime_type = EXCLUDED.mime_type,
                    file_size_bytes = EXCLUDED.file_size_bytes,
                    content_hash = EXCLUDED.content_hash,
                    storage_path = EXCLUDED.storage_path,
                    image_caption = EXCLUDED.image_caption,
                    parsed_text = EXCLUDED.parsed_text,
                    parse_status = EXCLUDED.parse_status,
                    updated_at = now()
                """,
                (
                    attachment_uid,
                    email_id,
                    attachment.get("gmail_attachment_id"),
                    filename,
                    _infer_file_ext(filename),
                    _infer_file_type(filename, mime_type),
                    mime_type or None,
                    file_size_bytes,
                    content_hash,
                    storage_path or None,
                    str(attachment.get("image_caption") or ""),
                    parsed_text or None,
                    "parsed" if parsed_text else "pending",
                ),
            )

        if attachment_uids:
            conn.execute(
                "DELETE FROM attachments WHERE email_id = %s AND attachment_uid <> ALL(%s)",
                (email_id, attachment_uids),
            )
        else:
            conn.execute("DELETE FROM attachments WHERE email_id = %s", (email_id,))


class GmailEmailFetcher:
    def __init__(
        self,
        config: GmailFetchConfig,
        *,
        store: PostgresEmailStore | None = None,
        logger: logging.Logger | None = None,
    ):
        self.config = config
        self.store = store or PostgresEmailStore(config.database_url, config.max_body_length, config.base_dir)
        self.logger = logger or LOGGER
        self.config.attachments_dir.mkdir(parents=True, exist_ok=True)

    def fetch_and_save(
        self,
        search_keyword: str = "",
        max_emails: int | None = None,
        *,
        date_after: str | None = None,
        date_before: str | None = None,
        sender: str | None = None,
        has_attachment: bool | None = None,
        progress: Callable[[Any], Any] | None = None,
    ) -> list[dict[str, Any]]:
        max_count = self._normalize_max_emails(max_emails)
        service = self._build_service()
        query = build_gmail_query(search_keyword, date_after, date_before, sender, has_attachment)
        self.logger.info("Gmail fetch started. query=%s max=%s", query, max_count)

        message_refs = self._list_message_refs(service, query, max_count)
        emails = self._load_messages(service, message_refs, progress=progress)

        self.store.upsert_emails(emails)
        if self.config.mirror_json_path:
            atomic_json_dump(self.config.mirror_json_path, emails)

        self.logger.info(
            "Gmail emails saved. count=%s database=%s",
            len(emails),
            redact_database_url(self.config.database_url),
        )
        return emails

    def _normalize_max_emails(self, max_emails: int | None) -> int:
        requested = self.config.max_emails_limit if max_emails is None else int(max_emails)
        return max(1, min(requested, self.config.max_emails_limit))

    def _build_service(self) -> Any:
        creds = self._load_credentials()
        return with_retries(
            lambda: build("gmail", "v1", credentials=creds),
            self.config.api_retries,
            self.config.api_backoff_sec,
            "gmail-build",
            self.logger,
        )

    def _load_credentials(self) -> Credentials:
        creds = None
        token_info = _load_json_env("GOOGLE_TOKEN_JSON", required=False)
        if token_info:
            creds = Credentials.from_authorized_user_info(token_info, GMAIL_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    _persist_json_env("GOOGLE_TOKEN_JSON", json.loads(creds.to_json()), self.config.env_path)
                except Exception:
                    self.logger.warning("Stored Gmail token is invalid. Re-authentication required.")
                    _clear_json_env("GOOGLE_TOKEN_JSON", self.config.env_path)
                    creds = None

            if not creds:
                credentials_info = _load_json_env("GOOGLE_CREDENTIALS_JSON")
                flow = InstalledAppFlow.from_client_config(credentials_info, GMAIL_SCOPES)
                creds = flow.run_local_server(port=0)
                _persist_json_env("GOOGLE_TOKEN_JSON", json.loads(creds.to_json()), self.config.env_path)

        return creds

    def _list_message_refs(self, service: Any, query: str, max_count: int) -> list[dict[str, str]]:
        message_refs: list[dict[str, str]] = []
        page_token = None

        while len(message_refs) < max_count:
            params: dict[str, Any] = {
                "userId": "me",
                "maxResults": min(50, max_count - len(message_refs)),
            }
            if query:
                params["q"] = query
            if page_token:
                params["pageToken"] = page_token

            response = with_retries(
                lambda: service.users().messages().list(**params).execute(),
                self.config.api_retries,
                self.config.api_backoff_sec,
                "gmail-list",
                self.logger,
            )
            message_refs.extend(response.get("messages", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return message_refs[:max_count]

    def _load_messages(
        self,
        service: Any,
        message_refs: list[dict[str, str]],
        *,
        progress: Callable[[Any], Any] | None,
    ) -> list[dict[str, Any]]:
        emails: list[dict[str, Any]] = []
        iterator = progress(message_refs) if progress else _default_progress(message_refs)

        for message_ref in iterator:
            try:
                response = with_retries(
                    lambda: service.users().messages().get(
                        userId="me",
                        id=message_ref["id"],
                        format="raw",
                    ).execute(),
                    self.config.api_retries,
                    self.config.api_backoff_sec,
                    "gmail-get-message",
                    self.logger,
                )
                emails.append(self._parse_message_response(response))
            except Exception as exc:
                self.logger.exception("Email fetch failed for id=%s: %s", message_ref.get("id"), exc)

        return emails

    def _parse_message_response(self, response: dict[str, Any]) -> dict[str, Any]:
        parsed = message_from_bytes(base64.urlsafe_b64decode(response["raw"]))
        attachments = extract_attachments(
            parsed,
            attachments_dir=self.config.attachments_dir,
            relative_prefix="attachments",
            max_body_length=self.config.max_body_length,
        )
        body = extract_text_body(parsed)[: self.config.max_body_length]
        from_email = decode_mime_header(parsed.get("From"))
        to_email = decode_mime_header(parsed.get("To"))
        subject = decode_mime_header(parsed.get("Subject"))
        date = decode_mime_header(parsed.get("Date"))
        rfc_message_id = decode_mime_header(parsed.get("Message-ID"))
        gmail_message_id = response.get("id", "")
        gmail_thread_id = response.get("threadId", "")
        return {
            "email_uid": build_email_uid(
                gmail_message_id,
                rfc_message_id,
                from_email,
                to_email,
                subject,
                date,
                body,
            ),
            "gmail_message_id": gmail_message_id,
            "gmail_thread_id": gmail_thread_id,
            "rfc_message_id": rfc_message_id,
            "from": from_email,
            "to": to_email,
            "subject": subject,
            "date": date,
            "body": body,
            "signature": "",
            "has_attachment": len(attachments) > 0,
            "attachments": attachments,
        }


def build_gmail_query(
    keyword: str | None,
    date_after: str | None = None,
    date_before: str | None = None,
    sender: str | None = None,
    has_attachment: bool | None = None,
) -> str:
    query_parts = []
    keyword = (keyword or "").strip()
    if keyword:
        query_parts.append(f"{{{keyword}}}")
    if date_after:
        query_parts.append(f"after:{date_after}")
    if date_before:
        query_parts.append(f"before:{date_before}")
    if sender:
        query_parts.append(f"from:{sender}")
    if has_attachment:
        query_parts.append("has:attachment")
    return " ".join(query_parts)


def build_email_uid(
    gmail_message_id: str | None,
    rfc_message_id: str | None,
    from_email: str,
    to_email: str,
    subject: str,
    date: str,
    body: str,
) -> str:
    if gmail_message_id:
        return f"gmail:{gmail_message_id}"
    if rfc_message_id:
        return f"rfc822:{rfc_message_id.strip('<>')}"

    fingerprint = "\n".join([from_email or "", to_email or "", subject or "", date or "", body or ""])
    return f"sha256:{hashlib.sha256(fingerprint.encode('utf-8', errors='ignore')).hexdigest()}"


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
    return hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()


def decode_mime_header(header_value: str | None) -> str:
    if not header_value:
        return ""

    decoded_parts = []
    for part, charset in decode_header(header_value):
        if isinstance(part, bytes):
            decoded_parts.append(part.decode(charset or "utf-8", errors="ignore"))
        else:
            decoded_parts.append(part)
    return "".join(decoded_parts)


def extract_text_body(message: Any) -> str:
    body = ""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body += payload.decode(charset, errors="ignore")
    else:
        payload = message.get_payload(decode=True)
        if payload:
            charset = message.get_content_charset() or "utf-8"
            body += payload.decode(charset, errors="ignore")
    return body


def extract_attachments(
    message: Any,
    *,
    attachments_dir: Path,
    relative_prefix: str,
    max_body_length: int,
) -> list[dict[str, Any]]:
    attachments = []
    preprocessor = _load_attachment_preprocessor()

    if not message.is_multipart():
        return attachments

    for part in message.walk():
        if part.get_content_disposition() != "attachment":
            continue

        filename = part.get_filename()
        if not filename:
            continue

        filename = decode_mime_header(filename)
        content_type = part.get_content_type()
        payload = part.get_payload(decode=True)

        if preprocessor and payload:
            attachment = preprocessor(filename, content_type, payload, max_body_length)
        else:
            attachment = {
                "filename": filename,
                "content_type": content_type,
                "content": _fallback_attachment_content(part, payload, content_type, max_body_length),
                "parsed": False,
            }

        if payload:
            file_path = save_attachment_file(attachments_dir, relative_prefix, filename, payload)
            if file_path:
                attachment["file_path"] = file_path

        attachments.append(attachment)

    return attachments


def save_attachment_file(
    attachments_dir: Path,
    relative_prefix: str,
    filename: str,
    payload: bytes,
) -> str | None:
    try:
        attachments_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.md5(payload).hexdigest()[:10]
        safe_name = re.sub(r"[^\w.\-]", "_", filename)
        target = attachments_dir / f"{digest}_{safe_name}"
        if not target.exists():
            target.write_bytes(payload)
        return f"{relative_prefix}/{target.name}"
    except Exception:
        LOGGER.exception("Attachment save failed. filename=%s", filename)
        return None


def with_retries(
    fn: Callable[[], Any],
    retries: int,
    backoff_sec: int,
    action_name: str,
    logger: logging.Logger,
) -> Any:
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as exc:
            last_error = exc
            if attempt >= retries:
                break
            sleep_time = backoff_sec * attempt
            logger.warning(
                "%s failed (%d/%d): %s. retry in %.1fs",
                action_name,
                attempt,
                retries,
                exc,
                sleep_time,
            )
            time.sleep(sleep_time)
    raise last_error


def atomic_json_dump(path: str | Path, data: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f".tmp_{target.name}")
    try:
        temp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=4, default=str),
            encoding="utf-8",
        )
        os.replace(temp_path, target)
    finally:
        temp_path.unlink(missing_ok=True)


def fetch_and_save_emails_to_postgres(
    search_keyword: str = "",
    max_emails: int | None = None,
    *,
    base_dir: str | Path | None = None,
    date_after: str | None = None,
    date_before: str | None = None,
    sender: str | None = None,
    has_attachment: bool | None = None,
    mirror_json: bool = True,
) -> list[dict[str, Any]]:
    root = Path(base_dir).resolve() if base_dir else Path(__file__).resolve().parent
    config = GmailFetchConfig.from_base_dir(
        root,
        mirror_json_name="emails.json" if mirror_json else None,
    )
    return GmailEmailFetcher(config).fetch_and_save(
        search_keyword=search_keyword,
        max_emails=max_emails,
        date_after=date_after,
        date_before=date_before,
        sender=sender,
        has_attachment=has_attachment,
    )


def fetch_and_save_emails_to_sqlite(
    search_keyword: str = "",
    max_emails: int | None = None,
    *,
    base_dir: str | Path | None = None,
    date_after: str | None = None,
    date_before: str | None = None,
    sender: str | None = None,
    has_attachment: bool | None = None,
    mirror_json: bool = True,
) -> list[dict[str, Any]]:
    return fetch_and_save_emails_to_postgres(
        search_keyword=search_keyword,
        max_emails=max_emails,
        base_dir=base_dir,
        date_after=date_after,
        date_before=date_before,
        sender=sender,
        has_attachment=has_attachment,
        mirror_json=mirror_json,
    )


def _fallback_attachment_content(part: Any, payload: bytes | None, content_type: str, max_body_length: int) -> str:
    if not payload:
        return ""
    if content_type in ("text/plain", "text/html"):
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="ignore")[:max_body_length]
    if content_type == "application/pdf":
        return "[PDF file]"
    if "word" in content_type or "document" in content_type:
        return "[Word document]"
    if "excel" in content_type or "spreadsheet" in content_type:
        return "[Excel file]"
    if "image" in content_type:
        return "[Image file]"
    return payload.decode("utf-8", errors="ignore")[:500] or "[Binary file]"


def _parse_email_datetime(value: str | None) -> Any:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def _normalize_email_list(value: str | list[str] | None) -> list[str]:
    if isinstance(value, list):
        candidates = [str(item) for item in value]
        return [item.strip() for item in candidates if item.strip()]
    if not value:
        return []
    return [email.strip() for _name, email in getaddresses([str(value)]) if email.strip()]


def _infer_file_ext(filename: str) -> str:
    name = str(filename or "").strip()
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


def _infer_file_type(filename: str, mime_type: str) -> str:
    ext = _infer_file_ext(filename)
    mime = str(mime_type or "").lower()
    if ext == "pdf" or "pdf" in mime:
        return "pdf"
    if ext in {"png", "jpg", "jpeg", "gif", "bmp", "webp"} or mime.startswith("image/"):
        return "image"
    if ext in {"xls", "xlsx", "csv"} or "excel" in mime or "sheet" in mime:
        return "excel"
    if ext in {"doc", "docx"} or "word" in mime or "document" in mime:
        return "word"
    if ext in {"txt", "md"} or mime.startswith("text/"):
        return "text"
    return "other"


def _content_hash_for_attachment(attachment: dict[str, Any]) -> str | None:
    file_path = str(attachment.get("file_path") or "").strip()
    if file_path:
        absolute_path = Path(__file__).resolve().parent / file_path
        if absolute_path.exists():
            return hashlib.sha256(absolute_path.read_bytes()).hexdigest()
    content = str(attachment.get("content") or "")
    if content:
        return hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()
    return None


def _attachment_file_size(base_dir: Path, storage_path: str) -> int | None:
    relative_path = str(storage_path or "").strip()
    if not relative_path:
        return None
    absolute_path = (base_dir / relative_path).resolve()
    if not absolute_path.exists():
        return None
    try:
        return absolute_path.stat().st_size
    except OSError:
        return None


def _load_attachment_preprocessor() -> Callable[..., dict[str, Any]] | None:
    try:
        from agents.preprocessor import process_attachment

        return process_attachment
    except ImportError:
        return None


def _default_progress(items: list[Any]) -> Any:
    if tqdm:
        return tqdm(items, desc="Fetching emails", unit="email")
    return items


def _env_int(name: str, default: int, min_value: int, max_value: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return max(min_value, min(parsed, max_value))


def _load_json_env(name: str, *, required: bool = True) -> dict[str, Any] | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        if required:
            raise RuntimeError(f"Missing required env var: {name}")
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Env var {name} must contain valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Env var {name} must contain a JSON object.")
    return parsed


def _persist_json_env(name: str, payload: dict[str, Any], env_path: Path) -> None:
    json_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    os.environ[name] = json_text
    line = f"{name}='{json_text}'"
    existing_lines: list[str] = []
    if env_path.exists():
        existing_lines = env_path.read_text(encoding="utf-8").splitlines()

    replaced = False
    updated_lines: list[str] = []
    for existing_line in existing_lines:
        stripped = existing_line.strip()
        if stripped.startswith(f"{name}="):
            updated_lines.append(line)
            replaced = True
        else:
            updated_lines.append(existing_line)
    if not replaced:
        if updated_lines and updated_lines[-1].strip():
            updated_lines.append("")
        updated_lines.append(line)

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")


def _clear_json_env(name: str, env_path: Path) -> None:
    os.environ.pop(name, None)
    if not env_path.exists():
        return
    updated_lines = [
        line for line in env_path.read_text(encoding="utf-8").splitlines() if not line.strip().startswith(f"{name}=")
    ]
    env_path.write_text("\n".join(updated_lines) + ("\n" if updated_lines else ""), encoding="utf-8")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Gmail messages and save them to PostgreSQL.")
    parser.add_argument("--keyword", default="", help="Optional Gmail search keyword. Empty value fetches all messages.")
    parser.add_argument("--max", type=int, default=None)
    parser.add_argument("--date-after", default=None)
    parser.add_argument("--date-before", default=None)
    parser.add_argument("--sender", default=None)
    parser.add_argument("--has-attachment", action="store_true")
    parser.add_argument("--base-dir", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--no-json", action="store_true")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    args = _parse_args()
    try:
        emails = fetch_and_save_emails_to_postgres(
            search_keyword=args.keyword,
            max_emails=args.max,
            base_dir=args.base_dir,
            date_after=args.date_after,
            date_before=args.date_before,
            sender=args.sender,
            has_attachment=args.has_attachment or None,
            mirror_json=not args.no_json,
        )
    except HttpError as exc:
        LOGGER.exception("Gmail HTTP error: %s", exc)
        raise SystemExit(1) from exc

    print(f"Saved {len(emails)} emails to PostgreSQL.")


if __name__ == "__main__":
    main()
