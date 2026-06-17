from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

PROJECT_DIR = Path(__file__).resolve().parents[2]
PIPELINE_DIR = PROJECT_DIR / "pipeline"
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from gmail_postgres_fetcher import (  # noqa: E402
    ACTIVE_ENV_PATH,
    GMAIL_MODIFY_SCOPES,
    GMAIL_READ_SCOPES,
    _load_json_env,
    _persist_json_env,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh Gmail OAuth token for fetch or trash actions.")
    parser.add_argument("--mode", choices=("fetch", "modify"), default="modify")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    scopes = GMAIL_MODIFY_SCOPES if args.mode == "modify" else GMAIL_READ_SCOPES
    credentials_info = _load_json_env("GOOGLE_CREDENTIALS_JSON")
    flow = InstalledAppFlow.from_client_config(credentials_info, scopes)
    creds = flow.run_local_server(port=0, open_browser=False)
    _persist_json_env("GOOGLE_TOKEN_JSON", json.loads(creds.to_json()), ACTIVE_ENV_PATH)
    print(f"Saved Gmail token with scopes={scopes} to {ACTIVE_ENV_PATH}")


if __name__ == "__main__":
    main()
