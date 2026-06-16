from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from gmail_postgres_fetcher import PostgresEmailStore, database_url_from_env, redact_database_url


BASE_DIR = Path(__file__).resolve().parent


def load_json_emails(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON array: {path}")
    return [item for item in data if isinstance(item, dict)]


def import_emails_json(path: Path, database_url: str) -> int:
    emails = load_json_emails(path)
    store = PostgresEmailStore(database_url)
    try:
        store.replace_emails(emails)
    finally:
        store.close()
    return len(emails)


def main() -> None:
    parser = argparse.ArgumentParser(description="Import an emails.json snapshot into PostgreSQL.")
    parser.add_argument("--input", type=Path, default=BASE_DIR / "emails.json")
    parser.add_argument("--database-url", default=database_url_from_env())
    args = parser.parse_args()

    count = import_emails_json(args.input, args.database_url)
    print(f"Imported {count} emails into {redact_database_url(args.database_url)}")


if __name__ == "__main__":
    main()
