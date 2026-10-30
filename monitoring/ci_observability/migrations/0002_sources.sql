-- CI observability source receipts, migration 0002 (W01b).
--
-- Applied after 0001 by ci_observability.ledger.apply_migrations under the
-- same rules: the target schema exists and search_path points at it, so every
-- object below lands in that schema and stays unqualified. The production
-- schema and the disposable test schemas get the same objects.
--
-- What this migration owns:
--   * source_receipt: one row per (trusted source identity, received digest).
--     The trusted source identity comes from the central caller, never from a
--     payload. Content columns are immutable; only the lifecycle status, the
--     committed-event count, and the failure code move, forward only. A
--     receipt keeps the raw delivered manifest text, the per-file digests,
--     the claimed execution id, the claimed redaction state, and the claimed
--     collection-source states as durable private evidence whether or not the
--     delivery is ever published.
--   * source_execution: the first receipt that published an execution id and
--     the verified job it is bound to. There is deliberately no manifest
--     digest pin: a delivery digest identifies a receipt, not an execution's
--     lifetime, so later deliveries may extend the stored event prefixes with
--     new records, journals, and files while the ledger keeps existing event
--     hashes immutable.
--   * source_github_observation: append-only evidence of the GitHub metadata
--     each delivery was reconciled against (raw run, job, artifact, and
--     commit-status records), the bounded trusted context, and the gate
--     decision (publication_authorized: publication was authorized, not that
--     it happened). The actual outcome is the receipt's forward-only status
--     and set-once failure code — an authorized delivery that fails
--     publication is 'failed' there, never a published claim here. GitHub
--     metadata evolves, so observations append; the conclusions they justify
--     are learned once, in ledger_execution.
--   * source_redaction_proof: append-only trusted redaction proofs (the W06
--     sanitizer's attestation interface). A receipt's redaction state is
--     derived from the proofs recorded for it, so a later proof can promote a
--     quarantined delivery without rewriting the receipt's immutable content.
--
-- Publication model: only deliveries with a VERIFIED job binding from trusted
-- metadata, a full-coverage trusted redaction proof (manifest plus every
-- listed file), and a non-contradicted trusted source proof reach the ledger.
-- Everything else stays durably quarantined: status 'quarantined', zero
-- canonical writes, bytes retained privately.
--
-- Invariants enforced here, not only in the Python client:
--   * Receipt delivery identity is unique: (source kind, repository, run id,
--     artifact name, artifact id, origin, received digest). Absent identity
--     parts are stored as '' or 0 instead of NULL because unique indexes
--     treat NULLs as distinct and would not deduplicate them.
--   * Receipt content is immutable. Status moves only forward:
--     received -> partial -> committed, received -> quarantined (promotable
--     to partial/committed when proof arrives), or -> failed (terminal).
--     events_committed never decreases and freezes at committed; a failure
--     code is set once.
--   * source_execution, source_github_observation, and source_redaction_proof
--     are append-only.
--
-- Reader isolation: the raw manifest text, GitHub snapshots, and redaction
-- evidence in these tables are unredacted producer data. The deployment's
-- default privileges grant the reader role SELECT on every table created
-- here, so this migration revokes every non-owner SELECT grant on the source
-- tables it creates: the reader keeps ledger access only. No roles are
-- created and no grants are added.

CREATE TABLE IF NOT EXISTS source_receipt (
    receipt_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_kind text NOT NULL CHECK (source_kind IN ('archive', 'directory')),
    source_repository text NOT NULL DEFAULT ''
        CHECK (source_repository = '' OR source_repository ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
    source_run_id bigint NOT NULL DEFAULT 0 CHECK (source_run_id >= 0),
    source_artifact_name text NOT NULL DEFAULT '' CHECK (char_length(source_artifact_name) <= 256),
    source_artifact_id bigint NOT NULL DEFAULT 0 CHECK (source_artifact_id >= 0),
    source_origin text NOT NULL DEFAULT '' CHECK (char_length(source_origin) <= 128),
    received_digest text NOT NULL CHECK (received_digest ~ '^sha256:[0-9a-f]{64}$'),
    manifest_sha256 text NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    manifest json NOT NULL,
    files json NOT NULL,
    file_count integer NOT NULL CHECK (file_count >= 0),
    total_bytes bigint NOT NULL CHECK (total_bytes >= 0),
    total_records bigint NOT NULL CHECK (total_records >= 0),
    execution_id text NOT NULL CHECK (execution_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$'),
    quarantine_dir text NOT NULL CHECK (char_length(quarantine_dir) <= 128),
    redaction_claim json NOT NULL,
    source_states json NOT NULL,
    status text NOT NULL DEFAULT 'received'
        CHECK (status IN ('received', 'partial', 'committed', 'quarantined', 'failed')),
    events_committed bigint NOT NULL DEFAULT 0 CHECK (events_committed >= 0),
    failure_code text CHECK (failure_code IS NULL OR char_length(failure_code) <= 128),
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Delivery deduplication: the same trusted source delivering the same
-- received bytes is the same receipt. Every identity column is NOT NULL
-- (with '' / 0 defaults) so the index actually deduplicates.
CREATE UNIQUE INDEX IF NOT EXISTS uq_source_receipt_delivery
    ON source_receipt (source_kind, source_repository, source_run_id,
                       source_artifact_name, source_artifact_id, source_origin,
                       received_digest);

CREATE INDEX IF NOT EXISTS idx_source_receipt_execution ON source_receipt (execution_id);
CREATE INDEX IF NOT EXISTS idx_source_receipt_status ON source_receipt (status);

CREATE TABLE IF NOT EXISTS source_execution (
    execution_id text PRIMARY KEY
        CHECK (execution_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$'),
    receipt_id bigint NOT NULL REFERENCES source_receipt (receipt_id),
    verified_job_id bigint NOT NULL CHECK (verified_job_id >= 1),
    published_at timestamptz NOT NULL DEFAULT now(),
    -- The owner row can only exist once the ledger registered the execution,
    -- which is the ingest order: authorize under the execution lock, register,
    -- then claim ownership.
    FOREIGN KEY (execution_id) REFERENCES ledger_execution (execution_id)
);

CREATE INDEX IF NOT EXISTS idx_source_execution_receipt ON source_execution (receipt_id);

CREATE TABLE IF NOT EXISTS source_github_observation (
    observation_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    receipt_id bigint NOT NULL REFERENCES source_receipt (receipt_id),
    execution_id text NOT NULL
        CHECK (execution_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$'),
    observed_at timestamptz NOT NULL DEFAULT now(),
    run_record json,
    job_records json,
    artifact_records json,
    status_records json,
    trusted_context json NOT NULL,
    job_verdict text NOT NULL
        CHECK (job_verdict IN ('verified', 'candidate', 'claimed', 'unknown')),
    job_reason text NOT NULL CHECK (char_length(job_reason) <= 128),
    numeric_job_id bigint CHECK (numeric_job_id IS NULL OR numeric_job_id >= 1),
    executed_in_attempt boolean,
    execution_attempt integer CHECK (execution_attempt IS NULL OR execution_attempt >= 1),
    job_conclusion text CHECK (job_conclusion IS NULL OR char_length(job_conclusion) BETWEEN 1 AND 64),
    run_conclusion text CHECK (run_conclusion IS NULL OR char_length(run_conclusion) BETWEEN 1 AND 64),
    artifact_verdict text CHECK (artifact_verdict IN ('verified', 'candidate', 'claimed', 'unknown')),
    artifact_reason text CHECK (artifact_reason IS NULL OR char_length(artifact_reason) <= 128),
    artifact_integrity text CHECK (artifact_integrity IN ('verified', 'mismatch', 'not_checked')),
    artifact_expired boolean,
    attributed_attempt integer CHECK (attributed_attempt IS NULL OR attributed_attempt >= 1),
    revision_verdict text CHECK (revision_verdict IN ('verified', 'candidate', 'claimed', 'unknown')),
    revision_reason text CHECK (revision_reason IS NULL OR char_length(revision_reason) <= 128),
    tested_sha text CHECK (tested_sha IS NULL OR tested_sha ~ '^[0-9a-f]{40}$'),
    revision_provenance text CHECK (revision_provenance IS NULL OR char_length(revision_provenance) <= 64),
    publication_authorized boolean NOT NULL,
    quarantine_reason text CHECK (quarantine_reason IS NULL OR char_length(quarantine_reason) <= 128),
    binding_evidence json NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_observation_execution ON source_github_observation (execution_id);
CREATE INDEX IF NOT EXISTS idx_observation_receipt ON source_github_observation (receipt_id);

CREATE TABLE IF NOT EXISTS source_redaction_proof (
    proof_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    receipt_id bigint NOT NULL REFERENCES source_receipt (receipt_id),
    observed_at timestamptz NOT NULL DEFAULT now(),
    policy text NOT NULL CHECK (char_length(policy) BETWEEN 1 AND 128),
    covered_digests json NOT NULL,
    covered_all boolean NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_redaction_proof_receipt ON source_redaction_proof (receipt_id);

-- Guards. ERRCODE 25006 (read_only_sql_transaction) is reused, as in 0001,
-- so clients map every rejection of a history-changing statement to one
-- condition. json columns compare through ::text because the json type has
-- no equality operator.

CREATE OR REPLACE FUNCTION source_receipt_update_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.receipt_id IS DISTINCT FROM OLD.receipt_id
       OR NEW.source_kind IS DISTINCT FROM OLD.source_kind
       OR NEW.source_repository IS DISTINCT FROM OLD.source_repository
       OR NEW.source_run_id IS DISTINCT FROM OLD.source_run_id
       OR NEW.source_artifact_name IS DISTINCT FROM OLD.source_artifact_name
       OR NEW.source_artifact_id IS DISTINCT FROM OLD.source_artifact_id
       OR NEW.source_origin IS DISTINCT FROM OLD.source_origin
       OR NEW.received_digest IS DISTINCT FROM OLD.received_digest
       OR NEW.manifest_sha256 IS DISTINCT FROM OLD.manifest_sha256
       OR NEW.manifest::text IS DISTINCT FROM OLD.manifest::text
       OR NEW.files::text IS DISTINCT FROM OLD.files::text
       OR NEW.file_count IS DISTINCT FROM OLD.file_count
       OR NEW.total_bytes IS DISTINCT FROM OLD.total_bytes
       OR NEW.total_records IS DISTINCT FROM OLD.total_records
       OR NEW.execution_id IS DISTINCT FROM OLD.execution_id
       OR NEW.quarantine_dir IS DISTINCT FROM OLD.quarantine_dir
       OR NEW.redaction_claim::text IS DISTINCT FROM OLD.redaction_claim::text
       OR NEW.source_states::text IS DISTINCT FROM OLD.source_states::text
       OR NEW.first_seen_at IS DISTINCT FROM OLD.first_seen_at THEN
        RAISE EXCEPTION 'source_receipt content is immutable'
            USING ERRCODE = '25006';
    END IF;
    IF NEW.events_committed < OLD.events_committed THEN
        RAISE EXCEPTION 'source_receipt events_committed cannot decrease'
            USING ERRCODE = '25006';
    END IF;
    IF OLD.failure_code IS NOT NULL
       AND NEW.failure_code IS DISTINCT FROM OLD.failure_code THEN
        RAISE EXCEPTION 'source_receipt failure_code is immutable once set'
            USING ERRCODE = '25006';
    END IF;
    IF NOT (
        (OLD.status = 'received'
             AND NEW.status IN ('received', 'partial', 'committed', 'quarantined', 'failed'))
        OR (OLD.status = 'partial' AND NEW.status IN ('partial', 'committed', 'failed'))
        OR (OLD.status = 'quarantined' AND NEW.status IN ('quarantined', 'partial', 'committed'))
        OR (OLD.status = 'committed' AND NEW.status = 'committed')
        OR (OLD.status = 'failed' AND NEW.status = 'failed')
    ) THEN
        RAISE EXCEPTION 'source_receipt status cannot move from % to %', OLD.status, NEW.status
            USING ERRCODE = '25006';
    END IF;
    IF OLD.status = 'committed' AND NEW.events_committed != OLD.events_committed THEN
        RAISE EXCEPTION 'source_receipt is terminal'
            USING ERRCODE = '25006';
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_source_receipt_no_delete ON source_receipt;
CREATE TRIGGER trg_source_receipt_no_delete
    BEFORE DELETE ON source_receipt
    FOR EACH ROW EXECUTE FUNCTION ledger_reject_write();

DROP TRIGGER IF EXISTS trg_source_receipt_update_guard ON source_receipt;
CREATE TRIGGER trg_source_receipt_update_guard
    BEFORE UPDATE ON source_receipt
    FOR EACH ROW EXECUTE FUNCTION source_receipt_update_guard();

-- Ownership rows, GitHub observations, and redaction proofs are append-only
-- evidence.
DROP TRIGGER IF EXISTS trg_source_execution_no_write ON source_execution;
CREATE TRIGGER trg_source_execution_no_write
    BEFORE UPDATE OR DELETE ON source_execution
    FOR EACH ROW EXECUTE FUNCTION ledger_reject_write();

DROP TRIGGER IF EXISTS trg_source_observation_no_write ON source_github_observation;
CREATE TRIGGER trg_source_observation_no_write
    BEFORE UPDATE OR DELETE ON source_github_observation
    FOR EACH ROW EXECUTE FUNCTION ledger_reject_write();

DROP TRIGGER IF EXISTS trg_source_redaction_proof_no_write ON source_redaction_proof;
CREATE TRIGGER trg_source_redaction_proof_no_write
    BEFORE UPDATE OR DELETE ON source_redaction_proof
    FOR EACH ROW EXECUTE FUNCTION ledger_reject_write();

-- Reader isolation. The deployment's default privileges grant the reader
-- role SELECT on every table created in this schema; the source tables hold
-- unredacted producer data (raw manifests, GitHub snapshots, redaction
-- evidence), so every non-owner SELECT grant on them is revoked here. The
-- ledger tables keep their grants: the reader serves those, nothing else.
-- Three cases are covered:
--   * table-level grants, revoked by grantee (PUBLIC is emitted as the
--     special unquoted keyword, never as a quoted role name);
--   * column-level SELECT grants, which live in pg_attribute.attacl and are
--     invisible to the table-grant view, revoked column by column;
--   * inherited access, which never creates its own ACL entry: revoking
--     from the direct grantee removes what members inherited from it.
DO $$
DECLARE holder text;
DECLARE column_name text;
DECLARE table_name text;
BEGIN
    FOR holder IN
        SELECT DISTINCT grants.grantee FROM information_schema.role_table_grants AS grants
        WHERE grants.table_schema = current_schema()
          AND grants.table_name IN ('source_receipt', 'source_execution',
                                    'source_github_observation', 'source_redaction_proof')
          AND grants.privilege_type = 'SELECT'
          AND grants.grantee <> current_user
    LOOP
        IF holder = 'PUBLIC' THEN
            EXECUTE 'REVOKE SELECT ON TABLE source_receipt, source_execution,'
                    ' source_github_observation, source_redaction_proof FROM PUBLIC';
        ELSE
            EXECUTE format('REVOKE SELECT ON TABLE source_receipt, source_execution,'
                           ' source_github_observation, source_redaction_proof FROM %I', holder);
        END IF;
    END LOOP;
    FOR table_name, column_name, holder IN
        SELECT DISTINCT grants.table_name, grants.column_name, grants.grantee
        FROM information_schema.role_column_grants AS grants
        WHERE grants.table_schema = current_schema()
          AND grants.table_name IN ('source_receipt', 'source_execution',
                                    'source_github_observation', 'source_redaction_proof')
          AND grants.privilege_type = 'SELECT'
    LOOP
        IF holder = 'PUBLIC' THEN
            EXECUTE format('REVOKE SELECT (%I) ON TABLE %I FROM PUBLIC',
                           column_name, table_name);
        ELSE
            EXECUTE format('REVOKE SELECT (%I) ON TABLE %I FROM %I',
                           column_name, table_name, holder);
        END IF;
    END LOOP;
END;
$$;
