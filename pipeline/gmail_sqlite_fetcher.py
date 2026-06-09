from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from email import message_from_bytes
from email.header import decode_header
from pathlib import Path
from typing import Any, Callable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None


LOGGER = logging.getLogger("gmail_sqlite_fetcher")
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


@dataclass(frozen=True)
class GmailFetchConfig:
    base_dir: Path
    db_path: Path
    credentials_path: Path
    token_path: Path
    attachments_dir: Path
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
        db_name: str = "emails.db",
        credentials_name: str = "credentials.json",
        token_name: str = "token.json",
        attachments_name: str = "attachments",
        mirror_json_name: str | None = "emails.json",
        max_emails_limit: int | None = None,
        max_body_length: int | None = None,
        api_retries: int | None = None,
        api_backoff_sec: int | None = None,
    ) -> "GmailFetchConfig":
        root = Path(base_dir).resolve()
        return cls(
            base_dir=root,
            db_path=root / db_name,
            credentials_path=root / credentials_name,
            token_path=root / token_name,
            attachments_dir=root / attachments_name,
            max_emails_limit=max_emails_limit or _env_int("FEBMAIL_MAX_EMAILS", 100, 1, 2000),
            max_body_length=max_body_length or _env_int("FEBMAIL_MAX_BODY_LENGTH", 3000, 500, 20000),
            api_retries=api_retries or _env_int("FEBMAIL_API_RETRIES", 3, 1, 10),
            api_backoff_sec=api_backoff_sec or _env_int("FEBMAIL_API_BACKOFF_SEC", 2, 1, 15),
            mirror_json_path=(root / mirror_json_name) if mirror_json_name else None,
        )


class SQLiteEmailStore:
    def __init__(self, db_path: str | Path, max_body_length: int = 3000):
        self.db_path = Path(db_path)
        self.max_body_length = max_body_length
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
        return self._conn

    def init_db(self) -> None:
        conn = self.connect()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_uid TEXT,
                gmail_message_id TEXT,
                gmail_thread_id TEXT,
                rfc_message_id TEXT,
                from_email TEXT,
                to_email TEXT,
                subject TEXT,
                date TEXT,
                body TEXT,
                signature TEXT,
                has_attachment INTEGER,
                attachments TEXT
            )
            """
        )
        self._ensure_email_columns(
            conn,
            {
                "email_uid": "TEXT",
                "gmail_message_id": "TEXT",
                "gmail_thread_id": "TEXT",
                "rfc_message_id": "TEXT",
            },
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_emails_email_uid ON emails(email_uid);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_subject ON emails(subject);")
        conn.commit()

    def _ensure_email_columns(self, conn: sqlite3.Connection, columns: dict[str, str]) -> None:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(emails)").fetchall()}
        for name, column_type in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE emails ADD COLUMN {name} {column_type}")

    def replace_emails(self, emails: list[dict[str, Any]]) -> None:
        conn = self.connect()
        with conn:
            conn.execute("DELETE FROM emails")
            conn.executemany(
                """
                INSERT INTO emails(
                    email_uid, gmail_message_id, gmail_thread_id, rfc_message_id,
                    from_email, to_email, subject, date, body,
                    signature, has_attachment, attachments
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        1 if email.get("has_attachment") else 0,
                        json.dumps(email.get("attachments", []), ensure_ascii=False),
                    )
                    for email in emails
                ],
            )

    def load_emails(self) -> list[dict[str, Any]]:
        rows = self.connect().execute(
            """
            SELECT email_uid, gmail_message_id, gmail_thread_id, rfc_message_id,
                   from_email, to_email, subject, date, body,
                   signature, has_attachment, attachments
            FROM emails
            ORDER BY id
            """
        ).fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            try:
                attachments = json.loads(row["attachments"]) if row["attachments"] else []
            except json.JSONDecodeError:
                attachments = []
            email_uid = row["email_uid"] or build_email_uid(
                row["gmail_message_id"],
                row["rfc_message_id"],
                row["from_email"],
                row["to_email"],
                row["subject"],
                row["date"],
                row["body"],
            )
            results.append(
                {
                    "email_uid": email_uid,
                    "gmail_message_id": row["gmail_message_id"],
                    "gmail_thread_id": row["gmail_thread_id"],
                    "rfc_message_id": row["rfc_message_id"],
                    "from": row["from_email"],
                    "to": row["to_email"],
                    "subject": row["subject"],
                    "date": row["date"],
                    "body": str(row["body"])[: self.max_body_length],
                    "signature": row["signature"],
                    "has_attachment": bool(row["has_attachment"]),
                    "attachments": attachments,
                }
            )
        return results

    def count(self) -> int:
        return self.connect().execute("SELECT COUNT(*) FROM emails").fetchone()[0]

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


class GmailEmailFetcher:
    def __init__(
        self,
        config: GmailFetchConfig,
        *,
        store: SQLiteEmailStore | None = None,
        logger: logging.Logger | None = None,
    ):
        self.config = config
        self.store = store or SQLiteEmailStore(config.db_path, config.max_body_length)
        self.logger = logger or LOGGER
        self.config.attachments_dir.mkdir(parents=True, exist_ok=True)

    def fetch_and_save(
        self,
        search_keyword: str = "quote order purchase",
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

        self.store.replace_emails(emails)
        if self.config.mirror_json_path:
            atomic_json_dump(self.config.mirror_json_path, emails)

        self.logger.info("Gmail emails saved. count=%s db=%s", len(emails), self.config.db_path)
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
        if self.config.token_path.exists():
            creds = Credentials.from_authorized_user_file(str(self.config.token_path), GMAIL_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception:
                    self.logger.warning("Stored Gmail token is invalid. Re-authentication required.")
                    self.config.token_path.unlink(missing_ok=True)
                    creds = None

            if not creds:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.config.credentials_path),
                    GMAIL_SCOPES,
                )
                creds = flow.run_local_server(port=0)

            self.config.token_path.write_text(creds.to_json(), encoding="utf-8")

        return creds

    def _list_message_refs(self, service: Any, query: str, max_count: int) -> list[dict[str, str]]:
        message_refs: list[dict[str, str]] = []
        page_token = None

        while len(message_refs) < max_count:
            params: dict[str, Any] = {
                "userId": "me",
                "q": query,
                "maxResults": min(50, max_count - len(message_refs)),
            }
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


def fetch_and_save_emails_to_sqlite(
    search_keyword: str = "quote order purchase",
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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Gmail messages and save them to emails.db.")
    parser.add_argument("--keyword", default="quote order purchase")
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
        emails = fetch_and_save_emails_to_sqlite(
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

    print(f"Saved {len(emails)} emails to SQLite.")


if __name__ == "__main__":
    main()
