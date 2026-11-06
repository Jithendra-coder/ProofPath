CREATE TABLE assessment_attempt_workspaces (
    attempt_id UUID PRIMARY KEY,
    candidate_user_id UUID NOT NULL,
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    source_code TEXT NOT NULL CHECK (length(source_code) <= 50000),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (candidate_user_id, attempt_id)
        REFERENCES assessment_attempts (candidate_user_id, id) ON DELETE RESTRICT
);

CREATE TABLE assessment_test_runs (
    id UUID PRIMARY KEY,
    attempt_id UUID NOT NULL,
    candidate_user_id UUID NOT NULL,
    workspace_version BIGINT NOT NULL CHECK (workspace_version > 0),
    request_key UUID NOT NULL,
    request_payload_hash BYTEA NOT NULL,
    source_sha256 BYTEA NOT NULL CHECK (octet_length(source_sha256) = 32),
    source_snapshot TEXT NOT NULL CHECK (length(source_snapshot) <= 50000),
    status TEXT NOT NULL CHECK (status IN ('running', 'passed', 'failed', 'timed_out', 'infrastructure_failed', 'cancelled')),
    competency_results JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(competency_results) = 'array'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    UNIQUE (attempt_id, request_key),
    UNIQUE (id, candidate_user_id),
    FOREIGN KEY (candidate_user_id, attempt_id)
        REFERENCES assessment_attempts (candidate_user_id, id) ON DELETE RESTRICT,
    CHECK ((status = 'running' AND completed_at IS NULL) OR (status <> 'running' AND completed_at IS NOT NULL))
);

CREATE INDEX assessment_test_runs_attempt_created_idx
    ON assessment_test_runs (attempt_id, created_at DESC, id);

ALTER TABLE candidate_competency_evidence
    ADD COLUMN source_test_run_id UUID,
    ADD CONSTRAINT candidate_competency_evidence_test_run_key
        FOREIGN KEY (source_test_run_id, candidate_user_id)
        REFERENCES assessment_test_runs (id, candidate_user_id) ON DELETE RESTRICT,
    ADD CONSTRAINT candidate_competency_evidence_test_run_source_check
        CHECK (source_test_run_id IS NULL OR source_type = 'assessment_result');

CREATE INDEX candidate_competency_evidence_test_run_idx
    ON candidate_competency_evidence (source_test_run_id, competency_code)
    WHERE source_test_run_id IS NOT NULL;
