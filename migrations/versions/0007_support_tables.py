"""idempotency_keys, risk_rules (+seed), risk_flags, reconciliation runs/issues, job_locks (+seed)

Tables from DESIGN.md §3.2, indexes from §3.4, constraints from docs/schema.md §4. Grants are
least privilege for wallet_app (runtime API and worker):
  - idempotency_keys: insert the key, complete it once, delete old keys (cleanup job)
  - risk_rules:       read only; thresholds are changed by migration or by the owner
  - risk_flags:       insert when a rule fires; fill the LLM explanation later
  - reconciliation_*: create and finish runs; issues are insert-only findings
  - job_locks:        UPDATE (last_started_at) is required for SELECT ... FOR UPDATE NOWAIT,
                      not only for touching the timestamp

Seeds are built with jsonb_build_object, so no JSON colons reach the SQL text (op.execute
wraps statements in text(), where ":word" would become a bind parameter).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    # --- idempotency_keys: one row per (user, key); stores the response for replay (§5) ---
    """
    CREATE TABLE idempotency_keys (
        id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        user_id       BIGINT      REFERENCES users (id),   -- NULL: unauthenticated POST /users
        key           UUID        NOT NULL,
        request_hash  CHAR(64)    NOT NULL,                -- sha256 hex
        status        TEXT        NOT NULL DEFAULT 'in_progress',
        response_code INTEGER,
        response_body JSONB,
        completed_at  TIMESTAMPTZ,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

        CONSTRAINT idempotency_keys_user_key_key UNIQUE NULLS NOT DISTINCT (user_id, key),
        CONSTRAINT idempotency_keys_status_check CHECK (status IN ('in_progress', 'completed')),
        CONSTRAINT idempotency_keys_request_hash_check CHECK (request_hash ~ '^[0-9a-f]{64}$'),
        -- Only 2xx and 4xx are stored: a 5xx rolls back the key with everything else.
        CONSTRAINT idempotency_keys_response_code_check CHECK (
            response_code BETWEEN 200 AND 499
        ),
        -- A completed key always carries its full response; an in-progress one carries none.
        CONSTRAINT idempotency_keys_completion_check CHECK (
            CASE status
                WHEN 'completed' THEN response_code IS NOT NULL
                                  AND response_body IS NOT NULL
                                  AND completed_at  IS NOT NULL
                ELSE response_code IS NULL
                 AND response_body IS NULL
                 AND completed_at  IS NULL
            END
        )
    )
    """,
    "CREATE INDEX idempotency_keys_created_idx ON idempotency_keys (created_at)",
    # --- risk_rules: thresholds as data (§9) ---
    """
    CREATE TABLE risk_rules (
        id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        code       TEXT        NOT NULL,
        action     TEXT        NOT NULL,
        params     JSONB       NOT NULL,
        enabled    BOOLEAN     NOT NULL DEFAULT true,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

        CONSTRAINT risk_rules_code_key UNIQUE (code),
        CONSTRAINT risk_rules_code_check CHECK (
            code IN ('velocity', 'amount_spike', 'new_payee_large')
        ),
        CONSTRAINT risk_rules_action_check CHECK (action IN ('hold', 'flag')),
        CONSTRAINT risk_rules_params_check CHECK (jsonb_typeof(params) = 'object')
    )
    """,
    """
    INSERT INTO risk_rules (code, action, params) VALUES
        ('velocity',        'hold',
            jsonb_build_object('window_minutes', 10, 'max_count', 5)),
        ('amount_spike',    'hold',
            jsonb_build_object('lookback_days', 30, 'multiplier', 5)),
        ('new_payee_large', 'flag',
            jsonb_build_object('threshold_paise', 1000000))
    """,
    # --- risk_flags: a rule fired on a transfer; the LLM explanation is filled in later ---
    """
    CREATE TABLE risk_flags (
        id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        transfer_id     BIGINT      NOT NULL REFERENCES transfers (id),
        rule_id         BIGINT      NOT NULL REFERENCES risk_rules (id),
        action_taken    TEXT        NOT NULL,
        details         JSONB       NOT NULL,   -- observed values
        llm_explanation TEXT,
        llm_label       TEXT,
        llm_model       TEXT,
        explained_at    TIMESTAMPTZ,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

        CONSTRAINT risk_flags_transfer_rule_key UNIQUE (transfer_id, rule_id),
        CONSTRAINT risk_flags_action_taken_check CHECK (action_taken IN ('hold', 'flag')),
        CONSTRAINT risk_flags_details_check CHECK (jsonb_typeof(details) = 'object'),
        CONSTRAINT risk_flags_llm_label_check CHECK (
            llm_label IN ('likely_ok', 'review', 'likely_fraud')
        ),
        -- An explanation is saved whole or not at all (invalid LLM output is never stored).
        CONSTRAINT risk_flags_explanation_check CHECK (
            (llm_label IS NULL AND llm_explanation IS NULL
                AND llm_model IS NULL AND explained_at IS NULL)
            OR (llm_label IS NOT NULL AND llm_explanation IS NOT NULL
                AND llm_model IS NOT NULL AND explained_at IS NOT NULL)
        )
    )
    """,
    "CREATE INDEX risk_flags_unexplained_idx ON risk_flags (created_at) WHERE llm_label IS NULL",
    # --- reconciliation_runs: lifecycle and outcome of one run (§8) ---
    """
    CREATE TABLE reconciliation_runs (
        id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        trigger       TEXT        NOT NULL,
        status        TEXT        NOT NULL DEFAULT 'queued',
        started_at    TIMESTAMPTZ,
        finished_at   TIMESTAMPTZ,
        checks_run    INTEGER     NOT NULL DEFAULT 0,
        issue_count   INTEGER     NOT NULL DEFAULT 0,
        error_message TEXT,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

        CONSTRAINT reconciliation_runs_trigger_check CHECK (trigger IN ('scheduled', 'manual')),
        CONSTRAINT reconciliation_runs_status_check CHECK (
            status IN ('queued', 'running', 'passed', 'failed', 'skipped', 'error')
        ),
        CONSTRAINT reconciliation_runs_counts_check CHECK (checks_run >= 0 AND issue_count >= 0),
        -- queued: not started; running: started, not finished; the rest: started and finished.
        CONSTRAINT reconciliation_runs_lifecycle_check CHECK (
            CASE status
                WHEN 'queued'  THEN started_at IS NULL     AND finished_at IS NULL
                WHEN 'running' THEN started_at IS NOT NULL AND finished_at IS NULL
                ELSE                started_at IS NOT NULL AND finished_at IS NOT NULL
                                AND finished_at >= started_at
            END
        ),
        CONSTRAINT reconciliation_runs_outcome_check CHECK (
            (status <> 'passed' OR issue_count = 0)
            AND (status <> 'failed' OR issue_count > 0)
            AND (status <> 'error' OR error_message IS NOT NULL)
        )
    )
    """,
    # --- reconciliation_issues: one root-cause finding of a run ---
    """
    CREATE TABLE reconciliation_issues (
        id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        run_id      BIGINT      NOT NULL REFERENCES reconciliation_runs (id),
        check_name  TEXT        NOT NULL,
        entity_type TEXT        NOT NULL,
        entity_id   BIGINT,               -- no FK on purpose: may point at corrupt/missing rows
        expected    BIGINT,
        actual      BIGINT,
        details     JSONB,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

        CONSTRAINT reconciliation_issues_entity_type_check CHECK (
            entity_type IN ('transfer', 'account', 'entry', 'ledger')
        ),
        CONSTRAINT reconciliation_issues_entity_check CHECK (
            (entity_type = 'ledger') = (entity_id IS NULL)
        )
    )
    """,
    "CREATE INDEX reconciliation_issues_run_idx ON reconciliation_issues (run_id)",
    # --- job_locks: one row per scheduled job, locked FOR UPDATE NOWAIT ---
    """
    CREATE TABLE job_locks (
        id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        job_name        TEXT        NOT NULL,
        last_started_at TIMESTAMPTZ,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

        CONSTRAINT job_locks_job_name_key UNIQUE (job_name)
    )
    """,
    """
    INSERT INTO job_locks (job_name) VALUES
        ('reconciliation'), ('flag_explanations'), ('idempotency_cleanup')
    """,
    # --- grants for the runtime role ---
    "GRANT SELECT, INSERT, DELETE ON idempotency_keys TO wallet_app",
    "GRANT UPDATE (status, response_code, response_body, completed_at) "
    "ON idempotency_keys TO wallet_app",
    "GRANT SELECT ON risk_rules TO wallet_app",
    "GRANT SELECT, INSERT ON risk_flags TO wallet_app",
    "GRANT UPDATE (llm_explanation, llm_label, llm_model, explained_at) "
    "ON risk_flags TO wallet_app",
    "GRANT SELECT, INSERT ON reconciliation_runs TO wallet_app",
    "GRANT UPDATE (status, started_at, finished_at, checks_run, issue_count, error_message) "
    "ON reconciliation_runs TO wallet_app",
    "GRANT SELECT, INSERT ON reconciliation_issues TO wallet_app",
    "GRANT SELECT ON job_locks TO wallet_app",
    "GRANT UPDATE (last_started_at) ON job_locks TO wallet_app",
]

DOWNGRADE = [
    "DROP TABLE job_locks",
    "DROP TABLE reconciliation_issues",
    "DROP TABLE reconciliation_runs",
    "DROP TABLE risk_flags",
    "DROP TABLE risk_rules",
    "DROP TABLE idempotency_keys",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
