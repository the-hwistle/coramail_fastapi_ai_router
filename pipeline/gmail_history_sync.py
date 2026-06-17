from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from googleapiclient.errors import HttpError

from gmail_postgres_fetcher import (
    GmailEmailFetcher,
    GmailFetchConfig,
    PostgresEmailStore,
    atomic_json_dump,
    build_gmail_query,
    redact_database_url,
    with_retries,
)

LOGGER = logging.getLogger("gmail_history_sync")
INBOX_LABEL = "INBOX"
TRASH_LABEL = "TRASH"
STATE_TABLE = "gmail_sync_state"
DEFAULT_MAILBOX = "me"


class GmailHistoryCursorExpired(RuntimeError):
    """Raised when Gmail no longer accepts the stored startHistoryId."""


def sync_gmail_history_to_postgres(
    search_keyword: str = "",
    max_emails: int | None = None,
    *,
    base_dir: str | Path | None = None,
    date_after: str | None = None,
    date_before: str | None = None,
    sender: str | None = None,
    has_attachment: bool | None = None,
    mirror_json: bool = True,
    progress: Callable[[Any], Any] | None = None,
) -> list[dict[str, Any]]:
    """Synchronize Gmail INBOX changes into PostgreSQL using users.history.list.

    The first run, or a run after an expired Gmail history cursor, rebuilds the
    local email table from the current INBOX snapshot and stores the mailbox
    history cursor. Later runs read Gmail history records and reconcile only the
    messages whose labels or deletion state changed.
    """

    root = Path(base_dir).resolve() if base_dir else Path(__file__).resolve().parent
    config = GmailFetchConfig.from_base_dir(
        root,
        mirror_json_name="emails.json" if mirror_json else None,
    )
    fetcher = GmailEmailFetcher(config, logger=LOGGER)
    service = fetcher._build_service()
    profile = _get_profile(fetcher, service)
    mailbox_email = str(profile.get("emailAddress") or DEFAULT_MAILBOX)
    profile_history_id = str(profile.get("historyId") or "")

    query = build_gmail_query(search_keyword, date_after, date_before, sender, has_attachment)
    if _has_user_filters(search_keyword, date_after, date_before, sender, has_attachment):
        LOGGER.warning(
            "Filtered Gmail fetch is not cursor-synchronized. Falling back to the existing filtered upsert flow. query=%s",
            query,
        )
        return fetcher.fetch_and_save(
            search_keyword=search_keyword,
            max_emails=max_emails,
            date_after=date_after,
            date_before=date_before,
            sender=sender,
            has_attachment=has_attachment,
            progress=progress,
        )

    inbox_query = query or "in:inbox"
    conn = fetcher.store.connect()
    _ensure_sync_state_table(conn)
    last_history_id = _load_last_history_id(conn, mailbox_email)

    try:
        if not last_history_id:
            LOGGER.info("Gmail history cursor not found. Running full INBOX snapshot sync.")
            return _run_full_snapshot_sync(
                fetcher,
                service,
                mailbox_email,
                profile_history_id,
                inbox_query,
                max_emails,
                progress,
                full_sync_reason="initial",
            )

        LOGGER.info("Gmail history sync started. mailbox=%s startHistoryId=%s", mailbox_email, last_history_id)
        latest_history_id = _apply_history_delta(fetcher, service, last_history_id)
        if latest_history_id:
            _save_history_id(fetcher.store, mailbox_email, latest_history_id, full_sync=False)
        emails = fetcher.store.load_emails()
        if config.mirror_json_path:
            atomic_json_dump(config.mirror_json_path, emails)
        LOGGER.info(
            "Gmail history sync completed. emails=%s historyId=%s database=%s",
            len(emails),
            latest_history_id or last_history_id,
            redact_database_url(config.database_url),
        )
        return emails
    except GmailHistoryCursorExpired:
        LOGGER.warning("Stored Gmail history cursor expired. Rebuilding INBOX snapshot.")
        return _run_full_snapshot_sync(
            fetcher,
            service,
            mailbox_email,
            profile_history_id,
            inbox_query,
            max_emails,
            progress,
            full_sync_reason="history-expired",
        )


def _has_user_filters(
    search_keyword: str | None,
    date_after: str | None,
    date_before: str | None,
    sender: str | None,
    has_attachment: bool | None,
) -> bool:
    return any([search_keyword, date_after, date_before, sender, has_attachment])


def _get_profile(fetcher: GmailEmailFetcher, service: Any) -> dict[str, Any]:
    return with_retries(
        lambda: service.users().getProfile(userId="me").execute(),
        fetcher.config.api_retries,
        fetcher.config.api_backoff_sec,
        "gmail-profile",
        fetcher.logger,
    )


def _run_full_snapshot_sync(
    fetcher: GmailEmailFetcher,
    service: Any,
    mailbox_email: str,
    profile_history_id: str,
    inbox_query: str,
    max_emails: int | None,
    progress: Callable[[Any], Any] | None,
    *,
    full_sync_reason: str,
) -> list[dict[str, Any]]:
    max_count = fetcher._normalize_max_emails(max_emails)
    LOGGER.info(
        "Gmail full snapshot sync started. reason=%s query=%s max=%s",
        full_sync_reason,
        inbox_query,
        max_count,
    )
    message_refs = fetcher._list_message_refs(service, inbox_query, max_count)
    emails = fetcher._load_messages(service, message_refs, progress=progress)
    fetcher.store.replace_emails(emails)

    latest_history_id = ""
    if profile_history_id:
        try:
            latest_history_id = _apply_history_delta(fetcher, service, profile_history_id)
        except GmailHistoryCursorExpired:
            latest_history_id = ""
    if not latest_history_id:
        latest_history_id = str(_get_profile(fetcher, service).get("historyId") or profile_history_id or "")
    if latest_history_id:
        _save_history_id(fetcher.store, mailbox_email, latest_history_id, full_sync=True)

    current_emails = fetcher.store.load_emails()
    if fetcher.config.mirror_json_path:
        atomic_json_dump(fetcher.config.mirror_json_path, current_emails)
    LOGGER.info(
        "Gmail full snapshot sync completed. emails=%s historyId=%s database=%s",
        len(current_emails),
        latest_history_id,
        redact_database_url(fetcher.config.database_url),
    )
    return current_emails


def _apply_history_delta(fetcher: GmailEmailFetcher, service: Any, start_history_id: str) -> str:
    touched_ids: set[str] = set()
    deleted_ids: set[str] = set()
    latest_history_id = ""
    page_token = None

    while True:
        params: dict[str, Any] = {
            "userId": "me",
            "startHistoryId": start_history_id,
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            response = with_retries(
                lambda: service.users().history().list(**params).execute(),
                fetcher.config.api_retries,
                fetcher.config.api_backoff_sec,
                "gmail-history-list",
                fetcher.logger,
            )
        except HttpError as exc:
            if getattr(exc.resp, "status", None) == 404:
                raise GmailHistoryCursorExpired(str(exc)) from exc
            raise

        latest_history_id = str(response.get("historyId") or latest_history_id)
        for history in response.get("history", []) or []:
            _collect_history_message_ids(history, touched_ids, deleted_ids)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    if touched_ids or deleted_ids:
        LOGGER.info(
            "Gmail history changes detected. touched=%s deleted=%s",
            len(touched_ids),
            len(deleted_ids),
        )
    else:
        LOGGER.info("Gmail history changes not found.")

    if deleted_ids:
        _delete_messages_from_store(fetcher.store, deleted_ids)
    _reconcile_current_message_state(fetcher, service, touched_ids - deleted_ids)
    return latest_history_id


def _collect_history_message_ids(history: dict[str, Any], touched_ids: set[str], deleted_ids: set[str]) -> None:
    for item in history.get("messagesAdded", []) or []:
        message_id = _history_message_id(item)
        if message_id:
            touched_ids.add(message_id)

    for item in history.get("messagesDeleted", []) or []:
        message_id = _history_message_id(item)
        if message_id:
            deleted_ids.add(message_id)

    for item in history.get("labelsAdded", []) or []:
        message_id = _history_message_id(item)
        labels = set(item.get("labelIds") or [])
        if not message_id:
            continue
        if INBOX_LABEL in labels:
            touched_ids.add(message_id)
        if TRASH_LABEL in labels:
            touched_ids.add(message_id)

    for item in history.get("labelsRemoved", []) or []:
        message_id = _history_message_id(item)
        labels = set(item.get("labelIds") or [])
        if message_id and INBOX_LABEL in labels:
            touched_ids.add(message_id)


def _history_message_id(item: dict[str, Any]) -> str:
    message = item.get("message") if isinstance(item, dict) else None
    if isinstance(message, dict):
        return str(message.get("id") or "")
    return ""


def _reconcile_current_message_state(fetcher: GmailEmailFetcher, service: Any, message_ids: set[str]) -> None:
    if not message_ids:
        return

    emails_to_upsert: list[dict[str, Any]] = []
    ids_to_remove: set[str] = set()
    for message_id in sorted(message_ids):
        try:
            response = _get_message_raw(fetcher, service, message_id)
        except HttpError as exc:
            if getattr(exc.resp, "status", None) == 404:
                ids_to_remove.add(message_id)
                continue
            raise

        labels = set(response.get("labelIds") or [])
        if INBOX_LABEL in labels and TRASH_LABEL not in labels:
            emails_to_upsert.append(fetcher._parse_message_response(response))
        else:
            ids_to_remove.add(message_id)

    if ids_to_remove:
        _delete_messages_from_store(fetcher.store, ids_to_remove)
    if emails_to_upsert:
        fetcher.store.upsert_emails(emails_to_upsert)


def _get_message_raw(fetcher: GmailEmailFetcher, service: Any, message_id: str) -> dict[str, Any]:
    return with_retries(
        lambda: service.users().messages().get(
            userId="me",
            id=message_id,
            format="raw",
        ).execute(),
        fetcher.config.api_retries,
        fetcher.config.api_backoff_sec,
        "gmail-get-message",
        fetcher.logger,
    )


def _delete_messages_from_store(store: PostgresEmailStore, gmail_message_ids: set[str]) -> list[str]:
    ids = sorted(str(message_id) for message_id in gmail_message_ids if str(message_id).strip())
    if not ids:
        return []

    conn = store.connect()
    rows = conn.execute(
        "SELECT email_uid FROM emails WHERE gmail_message_id = ANY(%s)",
        (ids,),
    ).fetchall()
    email_uids = [str(row["email_uid"]) for row in rows if row.get("email_uid")]

    with conn.transaction():
        if email_uids and store._table_exists(conn, "email_classifications"):
            conn.execute(
                "DELETE FROM email_classifications WHERE email_uid = ANY(%s)",
                (email_uids,),
            )
        conn.execute(
            "DELETE FROM emails WHERE gmail_message_id = ANY(%s)",
            (ids,),
        )
    if email_uids:
        LOGGER.info("Removed Gmail messages from PostgreSQL. count=%s", len(email_uids))
    return email_uids


def _ensure_sync_state_table(conn: Any) -> None:
    with conn.transaction():
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {STATE_TABLE} (
                mailbox_email TEXT PRIMARY KEY,
                last_history_id TEXT,
                last_full_sync_at TIMESTAMPTZ,
                last_partial_sync_at TIMESTAMPTZ,
                watch_expiration_at TIMESTAMPTZ,
                sync_status TEXT NOT NULL DEFAULT 'idle',
                last_error TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )


def _load_last_history_id(conn: Any, mailbox_email: str) -> str | None:
    row = conn.execute(
        f"SELECT last_history_id FROM {STATE_TABLE} WHERE mailbox_email = %s",
        (mailbox_email,),
    ).fetchone()
    if not row:
        return None
    value = row.get("last_history_id")
    return str(value) if value else None


def _save_history_id(store: PostgresEmailStore, mailbox_email: str, history_id: str, *, full_sync: bool) -> None:
    conn = store.connect()
    _ensure_sync_state_table(conn)
    if full_sync:
        timestamp_column = "last_full_sync_at"
    else:
        timestamp_column = "last_partial_sync_at"
    with conn.transaction():
        conn.execute(
            f"""
            INSERT INTO {STATE_TABLE}(
                mailbox_email, last_history_id, {timestamp_column}, sync_status, last_error, updated_at
            )
            VALUES(%s, %s, now(), 'idle', NULL, now())
            ON CONFLICT (mailbox_email) DO UPDATE SET
                last_history_id = EXCLUDED.last_history_id,
                {timestamp_column} = now(),
                sync_status = 'idle',
                last_error = NULL,
                updated_at = now()
            """,
            (mailbox_email, str(history_id)),
        )
