from __future__ import annotations

import argparse

import psycopg

from gmail_postgres_fetcher import database_url_from_env, redact_database_url


DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS gmail_sync_state (
        mailbox_email TEXT PRIMARY KEY,
        last_history_id TEXT,
        last_full_sync_at TIMESTAMPTZ,
        last_partial_sync_at TIMESTAMPTZ,
        watch_expiration_at TIMESTAMPTZ,
        sync_status TEXT NOT NULL DEFAULT 'idle',
        last_error TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS emails (
        email_id BIGSERIAL PRIMARY KEY,
        email_uid TEXT NOT NULL UNIQUE,
        gmail_message_id TEXT UNIQUE,
        gmail_thread_id TEXT,
        gmail_history_id TEXT,
        gmail_internal_date_ms BIGINT,
        rfc_message_id TEXT,
        thread_key TEXT,
        mailbox_email TEXT NOT NULL DEFAULT 'me',
        sender_email TEXT,
        sender_name TEXT,
        recipient_emails TEXT[] NOT NULL DEFAULT '{}',
        cc_emails TEXT[] NOT NULL DEFAULT '{}',
        gmail_label_ids TEXT[] NOT NULL DEFAULT '{}',
        subject TEXT NOT NULL DEFAULT '',
        snippet TEXT NOT NULL DEFAULT '',
        received_at TIMESTAMPTZ,
        fetched_at TIMESTAMPTZ,
        body_text TEXT NOT NULL DEFAULT '',
        body_html TEXT,
        body_hash TEXT,
        has_attachments BOOLEAN NOT NULL DEFAULT FALSE,
        gmail_size_estimate INTEGER,
        is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
        raw_headers JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS attachments (
        attachment_id BIGSERIAL PRIMARY KEY,
        attachment_uid TEXT NOT NULL UNIQUE,
        email_id BIGINT NOT NULL REFERENCES emails(email_id) ON DELETE CASCADE,
        gmail_part_id TEXT,
        gmail_attachment_id TEXT,
        filename TEXT NOT NULL DEFAULT '',
        file_ext TEXT NOT NULL DEFAULT '',
        file_type TEXT NOT NULL DEFAULT 'other',
        mime_type TEXT,
        file_size_bytes BIGINT,
        content_hash TEXT,
        storage_path TEXT,
        is_inline BOOLEAN NOT NULL DEFAULT FALSE,
        content_disposition TEXT,
        image_caption TEXT,
        ocr_text TEXT,
        parsed_text TEXT,
        parser_name TEXT,
        parser_version TEXT,
        parse_status TEXT NOT NULL DEFAULT 'pending',
        parse_error TEXT,
        parse_started_at TIMESTAMPTZ,
        parsed_at TIMESTAMPTZ,
        parse_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS routing_table (
        assignee_id BIGSERIAL PRIMARY KEY,
        assignee_name TEXT NOT NULL,
        department TEXT,
        position TEXT,
        email_address TEXT NOT NULL UNIQUE,
        routing_labels TEXT[] NOT NULL DEFAULT '{}',
        priority INTEGER NOT NULL DEFAULT 100,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS analysis_results (
        analysis_id BIGSERIAL PRIMARY KEY,
        email_id BIGINT NOT NULL REFERENCES emails(email_id) ON DELETE CASCADE,
        model_name TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        input_hash TEXT,
        prompt_hash TEXT,
        routing_policy_version TEXT,
        llm_request_id TEXT,
        summary TEXT NOT NULL DEFAULT '',
        email_type TEXT NOT NULL DEFAULT '기타',
        assignee_id BIGINT REFERENCES routing_table(assignee_id) ON DELETE SET NULL,
        urgency TEXT NOT NULL DEFAULT 'normal',
        confidence NUMERIC(4,3),
        business_refs TEXT[] NOT NULL DEFAULT '{}',
        vessel_names TEXT[] NOT NULL DEFAULT '{}',
        due_date DATE,
        total_amount NUMERIC(18,2),
        currency TEXT,
        routing_reason TEXT,
        raw_result JSONB NOT NULL DEFAULT '{}'::jsonb,
        status TEXT NOT NULL DEFAULT 'completed',
        error_message TEXT,
        latency_ms INTEGER,
        input_tokens INTEGER,
        output_tokens INTEGER,
        cost_usd NUMERIC(12,6),
        started_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS email_work_items (
        email_id BIGINT PRIMARY KEY REFERENCES emails(email_id) ON DELETE CASCADE,
        selected_analysis_id BIGINT REFERENCES analysis_results(analysis_id) ON DELETE SET NULL,
        final_email_type TEXT,
        final_assignee_id BIGINT REFERENCES routing_table(assignee_id) ON DELETE SET NULL,
        final_urgency TEXT NOT NULL DEFAULT 'normal',
        workflow_status TEXT NOT NULL DEFAULT 'new',
        reviewed_by TEXT,
        reviewed_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS email_events (
        event_id BIGSERIAL PRIMARY KEY,
        email_id BIGINT NOT NULL REFERENCES emails(email_id) ON DELETE CASCADE,
        actor_type TEXT NOT NULL,
        actor_id TEXT,
        event_type TEXT NOT NULL,
        before_value JSONB NOT NULL DEFAULT '{}'::jsonb,
        after_value JSONB NOT NULL DEFAULT '{}'::jsonb,
        note TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS assignment_history (
        assignment_id BIGSERIAL PRIMARY KEY,
        email_id BIGINT NOT NULL REFERENCES emails(email_id) ON DELETE CASCADE,
        from_assignee_id BIGINT REFERENCES routing_table(assignee_id) ON DELETE SET NULL,
        to_assignee_id BIGINT REFERENCES routing_table(assignee_id) ON DELETE SET NULL,
        assigned_by TEXT,
        assignment_source TEXT NOT NULL,
        reason TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS delivery_log (
        delivery_id BIGSERIAL PRIMARY KEY,
        email_id BIGINT NOT NULL REFERENCES emails(email_id) ON DELETE CASCADE,
        assignee_id BIGINT REFERENCES routing_table(assignee_id) ON DELETE SET NULL,
        channel TEXT NOT NULL,
        delivery_key TEXT NOT NULL UNIQUE,
        delivery_status TEXT NOT NULL DEFAULT 'pending',
        sent_at TIMESTAMPTZ,
        error_message TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_emails_gmail_thread_id ON emails(gmail_thread_id)",
    "CREATE INDEX IF NOT EXISTS idx_emails_received_at ON emails(received_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_emails_mailbox_received_at ON emails(mailbox_email, received_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_emails_sender_email ON emails(sender_email)",
    "CREATE INDEX IF NOT EXISTS idx_emails_recipient_emails ON emails USING GIN(recipient_emails)",
    "CREATE INDEX IF NOT EXISTS idx_emails_gmail_label_ids ON emails USING GIN(gmail_label_ids)",
    "CREATE INDEX IF NOT EXISTS idx_attachments_email_id ON attachments(email_id)",
    "CREATE INDEX IF NOT EXISTS idx_attachments_content_hash ON attachments(content_hash)",
    "CREATE INDEX IF NOT EXISTS idx_attachments_file_type ON attachments(file_type)",
    "CREATE INDEX IF NOT EXISTS idx_attachments_parse_status ON attachments(parse_status)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_attachments_email_part_id ON attachments(email_id, gmail_part_id) WHERE gmail_part_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_analysis_results_email_id ON analysis_results(email_id)",
    "CREATE INDEX IF NOT EXISTS idx_analysis_results_email_type ON analysis_results(email_type)",
    "CREATE INDEX IF NOT EXISTS idx_analysis_results_assignee_id ON analysis_results(assignee_id)",
    "CREATE INDEX IF NOT EXISTS idx_analysis_results_urgency ON analysis_results(urgency)",
    "CREATE INDEX IF NOT EXISTS idx_analysis_results_status ON analysis_results(status)",
    "CREATE INDEX IF NOT EXISTS idx_analysis_results_created_at ON analysis_results(created_at DESC)",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_analysis_results_email_model_prompt
    ON analysis_results(email_id, model_name, prompt_version)
    """,
    "CREATE INDEX IF NOT EXISTS idx_routing_table_department ON routing_table(department)",
    "CREATE INDEX IF NOT EXISTS idx_routing_table_labels ON routing_table USING GIN(routing_labels)",
    "CREATE INDEX IF NOT EXISTS idx_routing_table_is_active ON routing_table(is_active)",
    "CREATE INDEX IF NOT EXISTS idx_work_items_status_updated_at ON email_work_items(workflow_status, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_work_items_assignee_status ON email_work_items(final_assignee_id, workflow_status)",
    "CREATE INDEX IF NOT EXISTS idx_email_events_email_created_at ON email_events(email_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_assignment_history_email_created_at ON assignment_history(email_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_delivery_log_email_assignee ON delivery_log(email_id, assignee_id)",
]


CONSTRAINT_STATEMENTS = [
    """
    ALTER TABLE gmail_sync_state
    ADD CONSTRAINT chk_gmail_sync_state_status
    CHECK (sync_status IN ('idle', 'running', 'failed', 'needs_full_sync'))
    """,
    """
    ALTER TABLE emails
    ADD CONSTRAINT chk_emails_gmail_internal_date_ms
    CHECK (gmail_internal_date_ms IS NULL OR gmail_internal_date_ms > 0)
    """,
    """
    ALTER TABLE attachments
    ADD CONSTRAINT chk_attachments_file_size_bytes
    CHECK (file_size_bytes IS NULL OR file_size_bytes >= 0)
    """,
    """
    ALTER TABLE attachments
    ADD CONSTRAINT chk_attachments_parse_status
    CHECK (parse_status IN ('pending', 'running', 'parsed', 'failed', 'skipped'))
    """,
    """
    ALTER TABLE analysis_results
    ADD CONSTRAINT chk_analysis_results_confidence
    CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
    """,
    """
    ALTER TABLE analysis_results
    ADD CONSTRAINT chk_analysis_results_urgency
    CHECK (urgency IN ('low', 'normal', 'high', 'urgent'))
    """,
    """
    ALTER TABLE analysis_results
    ADD CONSTRAINT chk_analysis_results_status
    CHECK (status IN ('pending', 'running', 'completed', 'failed', 'needs_review'))
    """,
    """
    ALTER TABLE email_work_items
    ADD CONSTRAINT chk_email_work_items_final_urgency
    CHECK (final_urgency IN ('low', 'normal', 'high', 'urgent'))
    """,
    """
    ALTER TABLE email_work_items
    ADD CONSTRAINT chk_email_work_items_workflow_status
    CHECK (workflow_status IN ('new', 'analyzing', 'needs_review', 'assigned', 'notified', 'in_progress', 'done', 'ignored', 'failed'))
    """,
    """
    ALTER TABLE email_events
    ADD CONSTRAINT chk_email_events_actor_type
    CHECK (actor_type IN ('system', 'llm', 'human'))
    """,
    """
    ALTER TABLE assignment_history
    ADD CONSTRAINT chk_assignment_history_source
    CHECK (assignment_source IN ('llm', 'rule', 'human', 'fallback'))
    """,
    """
    ALTER TABLE delivery_log
    ADD CONSTRAINT chk_delivery_log_channel
    CHECK (channel IN ('gmail', 'slack', 'teams', 'web'))
    """,
    """
    ALTER TABLE delivery_log
    ADD CONSTRAINT chk_delivery_log_status
    CHECK (delivery_status IN ('pending', 'sent', 'failed', 'skipped'))
    """,
]


def create_schema(database_url: str) -> None:
    with psycopg.connect(database_url) as conn:
        for statement in DDL_STATEMENTS:
            with conn.transaction():
                conn.execute(statement)
        for statement in CONSTRAINT_STATEMENTS:
            try:
                with conn.transaction():
                    conn.execute(statement)
            except psycopg.errors.DuplicateObject:
                continue


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the normalized PostgreSQL schema for CoRA Mail.")
    parser.add_argument("--database-url", default=database_url_from_env())
    args = parser.parse_args()

    create_schema(args.database_url)
    print(f"Created normalized schema in {redact_database_url(args.database_url)}")


if __name__ == "__main__":
    main()
