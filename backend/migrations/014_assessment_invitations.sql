ALTER TABLE job_applications
    ADD CONSTRAINT job_applications_org_id_user_key UNIQUE (organization_id, id, user_id);

ALTER TABLE organization_assessment_versions
    ADD CONSTRAINT organization_assessment_versions_tenant_key
    UNIQUE (organization_id, assessment_id, version_number);

CREATE TABLE assessment_invitations (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL,
    application_id UUID NOT NULL,
    candidate_user_id UUID NOT NULL,
    assessment_id UUID NOT NULL,
    assessment_version INTEGER NOT NULL CHECK (assessment_version > 0),
    invited_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    request_key UUID NOT NULL,
    request_payload_hash BYTEA NOT NULL,
    status TEXT NOT NULL DEFAULT 'issued' CHECK (
        status IN ('issued', 'accepted', 'declined', 'cancelled', 'expired')
    ),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    expires_at TIMESTAMPTZ NOT NULL,
    response_from_version BIGINT,
    responded_at TIMESTAMPTZ,
    consent_version TEXT,
    consented_at TIMESTAMPTZ,
    cancelled_by_user_id UUID REFERENCES users(id) ON DELETE RESTRICT,
    cancelled_at TIMESTAMPTZ,
    cancellation_reason TEXT CHECK (cancellation_reason IS NULL OR length(cancellation_reason) BETWEEN 10 AND 500),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, request_key),
    UNIQUE (application_id, assessment_id, assessment_version),
    FOREIGN KEY (organization_id, application_id, candidate_user_id)
        REFERENCES job_applications (organization_id, id, user_id) ON DELETE RESTRICT,
    FOREIGN KEY (organization_id, assessment_id, assessment_version)
        REFERENCES organization_assessment_versions (organization_id, assessment_id, version_number) ON DELETE RESTRICT,
    CHECK (
        (status = 'issued' AND response_from_version IS NULL AND responded_at IS NULL
            AND consent_version IS NULL AND consented_at IS NULL AND cancelled_by_user_id IS NULL
            AND cancelled_at IS NULL AND cancellation_reason IS NULL)
        OR (status = 'accepted' AND response_from_version IS NOT NULL AND responded_at IS NOT NULL
            AND consent_version = 'assessment-invitation-v1' AND consented_at IS NOT NULL
            AND cancelled_by_user_id IS NULL AND cancelled_at IS NULL AND cancellation_reason IS NULL)
        OR (status = 'declined' AND response_from_version IS NOT NULL AND responded_at IS NOT NULL
            AND consent_version IS NULL AND consented_at IS NULL AND cancelled_by_user_id IS NULL
            AND cancelled_at IS NULL AND cancellation_reason IS NULL)
        OR (status = 'cancelled' AND response_from_version IS NULL AND responded_at IS NULL
            AND consent_version IS NULL AND consented_at IS NULL AND cancelled_by_user_id IS NOT NULL
            AND cancelled_at IS NOT NULL AND cancellation_reason IS NOT NULL)
        OR (status = 'expired' AND response_from_version IS NULL AND responded_at IS NULL
            AND consent_version IS NULL AND consented_at IS NULL AND cancelled_by_user_id IS NULL
            AND cancelled_at IS NULL AND cancellation_reason IS NULL)
    )
);

CREATE INDEX assessment_invitations_org_created_idx
    ON assessment_invitations (organization_id, created_at DESC, id);
CREATE INDEX assessment_invitations_candidate_created_idx
    ON assessment_invitations (candidate_user_id, created_at DESC, id);
