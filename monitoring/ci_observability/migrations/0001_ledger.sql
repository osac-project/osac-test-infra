-- CI observability ledger, migration 0001.
--
-- Applied by ci_observability.ledger.apply_migrations, which creates the
-- target schema and sets search_path to it before running this file, so
-- every object below is created in that schema. Objects stay unqualified so
-- the same file creates the production ci_observability schema and the
-- disposable schemas used by the tests.
--
-- Invariants enforced here, not only in the Python client:
--   * Journal events are append-only: UPDATE and DELETE raise.
--   * Execution identity is immutable. The github_job_id binding, the
--     executed state, and the GitHub conclusions may each change only once,
--     from unknown (NULL) to a value, because each is a fact the trusted
--     boundary learns exactly once.
--   * A numeric GitHub job id is claimed by at most one record that is
--     known to have executed (executed IS TRUE). Records that are known not
--     to have executed (inherited or skipped) may reference the job id of
--     the attempt that ran, and records with an unknown execution state
--     claim nothing.
--   * ledger_journal.prefix_sequence is the contiguity checkpoint of each
--     journal: the API keeps the stored sequences of a journal exactly
--     {1..prefix_sequence}, so appending is bounded by the batch size
--     instead of the journal length.
--   * The event payload is stored as canonical JSON text in a json column,
--     exactly as serialized before the write, together with the SHA-256 of
--     that text. jsonb is deliberately not used: it normalizes numbers
--     (1e23 becomes an integer, -0.0 becomes 0.0) and its text form can be
--     orders of magnitude larger than the delivered payload, which would
--     both corrupt evidence fidelity and disagree with the size bound.
--     The json type keeps the delivered text verbatim and still supports
--     the -> and ->> operators.
--   * Timestamps are kept twice on purpose: the indexed timestamptz form
--     serves filters at microsecond precision, and the original text form
--     preserves up to nine fractional digits as delivered, because silently
--     truncated evidence cannot be replayed as the original.
--
-- No roles and no grants are created here. The ingest role
-- (ci_observability) and the reader role (ci_observability_reader) are
-- bootstrapped by the monitoring deployment, which owns GRANT statements.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ledger_execution (
    execution_id text PRIMARY KEY
        CHECK (execution_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$'),
    schema_version integer NOT NULL CHECK (schema_version >= 1),
    repository_id bigint NOT NULL CHECK (repository_id >= 1),
    run_id bigint NOT NULL CHECK (run_id >= 1),
    run_attempt integer NOT NULL CHECK (run_attempt >= 1),
    logical_job_key text NOT NULL
        CHECK (logical_job_key ~ '^[A-Za-z0-9][A-Za-z0-9._/#=-]{0,255}$'),
    github_job_id bigint CHECK (github_job_id IS NULL OR github_job_id >= 1),
    executed boolean,
    github_run_conclusion text
        CHECK (github_run_conclusion IS NULL OR char_length(github_run_conclusion) BETWEEN 1 AND 64),
    github_job_conclusion text
        CHECK (github_job_conclusion IS NULL OR char_length(github_job_conclusion) BETWEEN 1 AND 64),
    registered_at timestamptz NOT NULL DEFAULT now()
);

-- Filter and list queries: run identity, cross-attempt job history, and
-- time-bounded listing of recent registrations.
CREATE INDEX idx_execution_run ON ledger_execution (repository_id, run_id, run_attempt);
CREATE INDEX idx_execution_logical_job ON ledger_execution (logical_job_key, run_id);
CREATE INDEX idx_execution_registered ON ledger_execution (registered_at);

CREATE UNIQUE INDEX uq_execution_github_job
    ON ledger_execution (github_job_id)
    WHERE github_job_id IS NOT NULL AND executed IS TRUE;

CREATE TABLE IF NOT EXISTS ledger_journal (
    execution_id text NOT NULL REFERENCES ledger_execution (execution_id),
    journal_id text NOT NULL
        CHECK (journal_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
    prefix_sequence bigint NOT NULL DEFAULT 0 CHECK (prefix_sequence >= 0),
    PRIMARY KEY (execution_id, journal_id)
);

CREATE TABLE IF NOT EXISTS ledger_event (
    execution_id text NOT NULL REFERENCES ledger_execution (execution_id),
    journal_id text NOT NULL
        CHECK (journal_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
    sequence bigint NOT NULL CHECK (sequence >= 1),
    schema_version integer NOT NULL CHECK (schema_version >= 1),
    recorded_at timestamptz NOT NULL,
    recorded_at_text text NOT NULL
        CHECK (recorded_at_text ~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z$'),
    source_at timestamptz,
    source_at_text text
        CHECK (source_at_text IS NULL OR source_at_text ~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z$'),
    mono_ns bigint CHECK (mono_ns IS NULL OR mono_ns >= 0),
    invocation_id text
        CHECK (invocation_id IS NULL OR invocation_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
    clock_id text
        CHECK (clock_id IS NULL OR clock_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
    clock_uncertainty_ns bigint CHECK (clock_uncertainty_ns IS NULL OR clock_uncertainty_ns >= 0),
    event text NOT NULL CHECK (char_length(event) BETWEEN 1 AND 128),
    data json NOT NULL CHECK (octet_length(data::text) <= 1048576),
    data_sha256 text NOT NULL CHECK (data_sha256 ~ '^[0-9a-f]{64}$'),
    ingested_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (execution_id, journal_id, sequence)
);

-- Bounded timeline queries: the events of one execution ordered by
-- observation time, with journal identity as the deterministic tie-breaker.
CREATE INDEX idx_event_execution_recorded
    ON ledger_event (execution_id, recorded_at, journal_id, sequence);

-- The event vocabulary belongs to the journal contract and is not fixed
-- here: the event column is only bounded in length. New event names
-- (clock, progress, resource, fixture) require no storage change.

-- Guards. ERRCODE 25006 (read_only_sql_transaction) is reused so clients
-- map every rejection of a history-changing statement to one condition.
-- They constrain ordinary clients; the schema owner can always change
-- history through DDL, which is why retention stays a deployment-owned
-- maintenance path instead of an application feature.

CREATE OR REPLACE FUNCTION ledger_reject_write() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'table % is append-only', TG_TABLE_NAME
        USING ERRCODE = '25006';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION ledger_execution_update_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.execution_id IS DISTINCT FROM OLD.execution_id
       OR NEW.schema_version IS DISTINCT FROM OLD.schema_version
       OR NEW.repository_id IS DISTINCT FROM OLD.repository_id
       OR NEW.run_id IS DISTINCT FROM OLD.run_id
       OR NEW.run_attempt IS DISTINCT FROM OLD.run_attempt
       OR NEW.logical_job_key IS DISTINCT FROM OLD.logical_job_key
       OR NEW.registered_at IS DISTINCT FROM OLD.registered_at THEN
        RAISE EXCEPTION 'ledger_execution identity is immutable'
            USING ERRCODE = '25006';
    END IF;
    IF OLD.github_job_id IS NOT NULL
       AND NEW.github_job_id IS DISTINCT FROM OLD.github_job_id THEN
        RAISE EXCEPTION 'ledger_execution github_job_id is already bound'
            USING ERRCODE = '25006';
    END IF;
    IF OLD.executed IS NOT NULL
       AND NEW.executed IS DISTINCT FROM OLD.executed THEN
        RAISE EXCEPTION 'ledger_execution executed state is already resolved'
            USING ERRCODE = '25006';
    END IF;
    IF OLD.github_run_conclusion IS NOT NULL
       AND NEW.github_run_conclusion IS DISTINCT FROM OLD.github_run_conclusion THEN
        RAISE EXCEPTION 'ledger_execution github_run_conclusion is immutable'
            USING ERRCODE = '25006';
    END IF;
    IF OLD.github_job_conclusion IS NOT NULL
       AND NEW.github_job_conclusion IS DISTINCT FROM OLD.github_job_conclusion THEN
        RAISE EXCEPTION 'ledger_execution github_job_conclusion is immutable'
            USING ERRCODE = '25006';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION ledger_journal_update_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.execution_id IS DISTINCT FROM OLD.execution_id
       OR NEW.journal_id IS DISTINCT FROM OLD.journal_id THEN
        RAISE EXCEPTION 'ledger_journal identity is immutable'
            USING ERRCODE = '25006';
    END IF;
    IF NEW.prefix_sequence < OLD.prefix_sequence THEN
        RAISE EXCEPTION 'ledger_journal prefix_sequence cannot decrease'
            USING ERRCODE = '25006';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_event_append_only ON ledger_event;
CREATE TRIGGER trg_event_append_only
    BEFORE UPDATE OR DELETE ON ledger_event
    FOR EACH ROW EXECUTE FUNCTION ledger_reject_write();

DROP TRIGGER IF EXISTS trg_journal_no_delete ON ledger_journal;
CREATE TRIGGER trg_journal_no_delete
    BEFORE DELETE ON ledger_journal
    FOR EACH ROW EXECUTE FUNCTION ledger_reject_write();

DROP TRIGGER IF EXISTS trg_journal_update_guard ON ledger_journal;
CREATE TRIGGER trg_journal_update_guard
    BEFORE UPDATE ON ledger_journal
    FOR EACH ROW EXECUTE FUNCTION ledger_journal_update_guard();

DROP TRIGGER IF EXISTS trg_execution_no_delete ON ledger_execution;
CREATE TRIGGER trg_execution_no_delete
    BEFORE DELETE ON ledger_execution
    FOR EACH ROW EXECUTE FUNCTION ledger_reject_write();

DROP TRIGGER IF EXISTS trg_execution_update_guard ON ledger_execution;
CREATE TRIGGER trg_execution_update_guard
    BEFORE UPDATE ON ledger_execution
    FOR EACH ROW EXECUTE FUNCTION ledger_execution_update_guard();
