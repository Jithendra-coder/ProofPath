ALTER TABLE job_applications
    ADD CONSTRAINT job_applications_organization_application_key UNIQUE (organization_id, id);

CREATE TABLE application_reviewer_assignments (
    organization_id UUID NOT NULL,
    application_id UUID PRIMARY KEY,
    reviewer_user_id UUID NOT NULL,
    assigned_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    request_key UUID NOT NULL,
    request_payload_hash BYTEA NOT NULL,
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (organization_id, application_id)
        REFERENCES job_applications (organization_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, reviewer_user_id)
        REFERENCES organization_memberships (organization_id, user_id) ON DELETE CASCADE
);

CREATE INDEX application_reviewer_assignments_reviewer_idx
    ON application_reviewer_assignments (organization_id, reviewer_user_id, application_id);
