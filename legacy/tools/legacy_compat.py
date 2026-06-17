from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[2]
PIPELINE_DIR = PROJECT_DIR / "pipeline"
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from gmail_postgres_fetcher import fetch_and_save_emails_to_postgres
from index import load_emails_from_postgres


def fetch_and_save_emails_to_sqlite(
    search_keyword: str = "",
    max_emails: int | None = None,
    *,
    base_dir: str | Path | None = None,
    inbox_only: bool = False,
    date_after: str | None = None,
    date_before: str | None = None,
    sender: str | None = None,
    has_attachment: bool | None = None,
    mirror_json: bool = False,
) -> list[dict[str, Any]]:
    return fetch_and_save_emails_to_postgres(
        search_keyword=search_keyword,
        max_emails=max_emails,
        base_dir=base_dir,
        inbox_only=inbox_only,
        date_after=date_after,
        date_before=date_before,
        sender=sender,
        has_attachment=has_attachment,
        mirror_json=mirror_json,
    )


def load_emails_from_sqlite(db_path: Path | None = None) -> list[dict[str, Any]]:
    del db_path
    return load_emails_from_postgres()
