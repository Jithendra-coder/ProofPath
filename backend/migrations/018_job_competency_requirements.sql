CREATE TABLE organization_job_competency_requirements (
    organization_id UUID NOT NULL,
    job_id UUID NOT NULL,
    competency_code TEXT NOT NULL REFERENCES competencies(code) ON DELETE RESTRICT,
    target_level TEXT NOT NULL CHECK (target_level IN ('junior', 'mid', 'senior')),
    accepted_verification_methods TEXT[] NOT NULL CHECK (
        cardinality(accepted_verification_methods) BETWEEN 1 AND 2
        AND accepted_verification_methods <@ ARRAY['deterministic_test', 'human_review']::TEXT[]
    ),
    max_age_days INTEGER NOT NULL CHECK (max_age_days BETWEEN 1 AND 3650),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, competency_code),
    FOREIGN KEY (organization_id, job_id)
        REFERENCES organization_jobs (organization_id, id) ON DELETE CASCADE
);

CREATE INDEX organization_job_competency_requirement_catalog_idx
    ON organization_job_competency_requirements (competency_code, job_id);
