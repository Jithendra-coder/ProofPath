CREATE TABLE competencies (
    code TEXT PRIMARY KEY CHECK (code ~ '^[a-z0-9][a-z0-9_.-]{2,79}$'),
    track TEXT NOT NULL CHECK (track = 'python-fastapi-backend'),
    name TEXT NOT NULL CHECK (length(name) BETWEEN 2 AND 120),
    description TEXT NOT NULL CHECK (length(description) BETWEEN 10 AND 500),
    sort_order SMALLINT NOT NULL UNIQUE CHECK (sort_order > 0),
    active BOOLEAN NOT NULL DEFAULT TRUE
);

INSERT INTO competencies (code, track, name, description, sort_order) VALUES
    ('python.language_fundamentals', 'python-fastapi-backend', 'Python language fundamentals', 'Data structures, control flow, exceptions, and clear Python code.', 1),
    ('python.types_and_data_model', 'python-fastapi-backend', 'Types and data model', 'Type annotations, validation models, and predictable data boundaries.', 2),
    ('fastapi.api_design', 'python-fastapi-backend', 'FastAPI API design', 'Resource-oriented routes, explicit HTTP semantics, and typed contracts.', 3),
    ('fastapi.input_validation', 'python-fastapi-backend', 'Request validation', 'Validation of untrusted request data and explicit response schemas.', 4),
    ('postgres.transactions', 'python-fastapi-backend', 'PostgreSQL transactions', 'Consistent multi-step writes, constraints, and concurrency behavior.', 5),
    ('api.authorization', 'python-fastapi-backend', 'Authorization and tenant isolation', 'Enforcement of identity, role, resource ownership, and organization scope.', 6),
    ('security.web_api', 'python-fastapi-backend', 'Web API security', 'Protection of sessions, state-changing requests, and sensitive data.', 7),
    ('testing.integration', 'python-fastapi-backend', 'Integration testing', 'Reproducible tests for persistence, failures, and authorization boundaries.', 8);

ALTER TABLE candidate_projects
    ADD CONSTRAINT candidate_projects_user_id_id_key UNIQUE (user_id, id);

ALTER TABLE assessment_attempts
    ADD CONSTRAINT assessment_attempts_candidate_user_id_id_key UNIQUE (candidate_user_id, id);

CREATE TABLE candidate_competency_evidence (
    id UUID PRIMARY KEY,
    candidate_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    competency_code TEXT NOT NULL REFERENCES competencies(code) ON DELETE RESTRICT,
    source_type TEXT NOT NULL CHECK (source_type IN ('project_claim', 'assessment_result')),
    project_id UUID,
    assessment_attempt_id UUID,
    candidate_statement TEXT CHECK (candidate_statement IS NULL OR length(candidate_statement) BETWEEN 20 AND 1000),
    observed_outcome TEXT CHECK (observed_outcome IS NULL OR length(observed_outcome) <= 4000),
    verification_method TEXT NOT NULL CHECK (verification_method IN ('self_reported', 'deterministic_test', 'human_review')),
    review_status TEXT NOT NULL DEFAULT 'unreviewed' CHECK (review_status IN ('unreviewed', 'accepted', 'rejected')),
    reviewed_by_user_id UUID REFERENCES users(id) ON DELETE RESTRICT,
    reviewed_at TIMESTAMPTZ,
    evidence_limitations TEXT NOT NULL CHECK (length(evidence_limitations) BETWEEN 10 AND 1000),
    sharing_scope TEXT NOT NULL DEFAULT 'candidate_only' CHECK (sharing_scope = 'candidate_only'),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    withdrawn_at TIMESTAMPTZ,
    FOREIGN KEY (candidate_user_id, project_id)
        REFERENCES candidate_projects (user_id, id) ON DELETE CASCADE,
    FOREIGN KEY (candidate_user_id, assessment_attempt_id)
        REFERENCES assessment_attempts (candidate_user_id, id) ON DELETE RESTRICT,
    CHECK (
        (source_type = 'project_claim' AND project_id IS NOT NULL AND assessment_attempt_id IS NULL
            AND candidate_statement IS NOT NULL AND observed_outcome IS NULL
            AND verification_method = 'self_reported' AND review_status = 'unreviewed'
            AND reviewed_by_user_id IS NULL AND reviewed_at IS NULL)
        OR
        (source_type = 'assessment_result' AND project_id IS NULL AND assessment_attempt_id IS NOT NULL
            AND candidate_statement IS NULL AND observed_outcome IS NOT NULL)
    ),
    CHECK (
        (review_status = 'unreviewed' AND reviewed_by_user_id IS NULL AND reviewed_at IS NULL)
        OR (review_status IN ('accepted', 'rejected') AND reviewed_by_user_id IS NOT NULL AND reviewed_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX candidate_project_competency_claim_active_idx
    ON candidate_competency_evidence (candidate_user_id, project_id, competency_code)
    WHERE source_type = 'project_claim' AND withdrawn_at IS NULL;

CREATE INDEX candidate_competency_evidence_candidate_idx
    ON candidate_competency_evidence (candidate_user_id, competency_code, recorded_at DESC);

CREATE INDEX candidate_competency_evidence_attempt_idx
    ON candidate_competency_evidence (assessment_attempt_id, competency_code)
    WHERE source_type = 'assessment_result';
