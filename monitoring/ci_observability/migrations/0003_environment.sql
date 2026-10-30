-- Public metadata uses the ingest owner's view privileges. Source tables stay
-- private, and the barrier keeps reader predicates outside the publication gate.
CREATE OR REPLACE VIEW ledger_environment WITH (security_barrier = true) AS
SELECT
    receipt.receipt_id,
    execution.execution_id,
    receipt.manifest_sha256,
    execution.repository_id,
    execution.run_id,
    execution.run_attempt,
    execution.github_job_id,
    receipt.manifest ->> 'created_at' AS source_created_at,
    receipt.first_seen_at,
    receipt.manifest #>> '{execution,workflow,path}' AS claimed_workflow_path,
    receipt.manifest #>> '{execution,workflow,revision}' AS claimed_workflow_revision,
    receipt.manifest #>> '{execution,event}' AS claimed_event,
    receipt.manifest #>> '{execution,cohort}' AS claimed_cohort,
    json_build_object(
        'framework', receipt.manifest #> '{suite,framework}',
        'tier', receipt.manifest #> '{suite,tier}',
        'component', receipt.manifest #> '{suite,component}',
        'variant', receipt.manifest #> '{suite,variant}',
        'selection', CASE WHEN receipt.manifest #> '{suite,selection}' IS NULL THEN NULL::json
            ELSE json_build_object(
                'paths', receipt.manifest #> '{suite,selection,paths}',
                'markers', receipt.manifest #> '{suite,selection,markers}',
                'keyword', receipt.manifest #> '{suite,selection,keyword}',
                'worker_count', receipt.manifest #> '{suite,selection,worker_count}'
            ) END,
        'campaign', CASE WHEN receipt.manifest #> '{suite,campaign}' IS NULL THEN NULL::json
            ELSE json_build_object(
                'campaign_id', receipt.manifest #> '{suite,campaign,campaign_id}',
                'group', receipt.manifest #> '{suite,campaign,group}',
                'repetition', receipt.manifest #> '{suite,campaign,repetition}',
                'repetitions_total', receipt.manifest #> '{suite,campaign,repetitions_total}'
            ) END
    ) AS claimed_suite,
    (
        SELECT json_agg(json_build_object(
            'role', source.value -> 'role',
            'repository', source.value -> 'repository',
            'revision', source.value -> 'revision'
        ) ORDER BY source.ordinality)
        FROM json_array_elements(receipt.manifest #> '{environment,sources}')
            WITH ORDINALITY AS source(value, ordinality)
        WHERE source.value ->> 'role' IN (
            'tested', 'workflow', 'test-suite', 'installer', 'infrastructure', 'orchestration'
        )
    ) AS claimed_sources,
    receipt.manifest #> '{environment,image_digests}' AS claimed_image_digests,
    receipt.manifest #>> '{environment,cluster_snapshot}' AS claimed_cluster_snapshot,
    receipt.manifest #>> '{environment,config_hash}' AS claimed_config_hash,
    (receipt.manifest #>> '{completeness,complete}')::boolean AS claimed_complete,
    receipt.manifest #>> '{completeness,end_reason}' AS claimed_end_reason,
    -- The contract's nonnegative integer has no bigint bound. Keep its JSON
    -- number instead of rejecting otherwise valid large counts in a cast.
    receipt.manifest #> '{completeness,dropped_records}' AS claimed_dropped_records,
    observation.tested_sha,
    observation.revision_verdict,
    observation.revision_reason,
    observation.revision_provenance
FROM source_receipt AS receipt
JOIN source_execution AS owner ON owner.execution_id = receipt.execution_id
JOIN ledger_execution AS execution
    ON execution.execution_id = owner.execution_id
    AND execution.github_job_id = owner.verified_job_id
JOIN LATERAL (
    SELECT evidence.tested_sha, evidence.revision_verdict,
        evidence.revision_reason, evidence.revision_provenance
    FROM source_github_observation AS evidence
    WHERE evidence.receipt_id = receipt.receipt_id
        AND evidence.job_verdict = 'verified'
        AND evidence.publication_authorized
        AND evidence.numeric_job_id = owner.verified_job_id
    ORDER BY evidence.observation_id DESC
    LIMIT 1
) AS observation ON true
WHERE receipt.status = 'committed'
    AND EXISTS (
        SELECT 1 FROM source_redaction_proof AS proof
        WHERE proof.receipt_id = receipt.receipt_id AND proof.covered_all
    );
