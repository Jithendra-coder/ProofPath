ALTER TABLE assessment_invitations
    ADD CONSTRAINT assessment_invitations_attempt_scope_key
    UNIQUE (organization_id, id, candidate_user_id, assessment_id, assessment_version);

CREATE TABLE assessment_attempts (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL,
    invitation_id UUID NOT NULL,
    candidate_user_id UUID NOT NULL,
    assessment_id UUID NOT NULL,
    assessment_version INTEGER NOT NULL CHECK (assessment_version > 0),
    request_key UUID NOT NULL,
    request_payload_hash BYTEA NOT NULL,
    status TEXT NOT NULL DEFAULT 'created' CHECK (
        status IN ('created', 'consented', 'ready', 'active', 'paused', 'submitted',
                   'processing', 'completed', 'review_required', 'processing_failed', 'cancelled')
    ),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (candidate_user_id, request_key),
    FOREIGN KEY (organization_id, invitation_id, candidate_user_id, assessment_id, assessment_version)
        REFERENCES assessment_invitations
            (organization_id, id, candidate_user_id, assessment_id, assessment_version)
        ON DELETE RESTRICT
);

CREATE UNIQUE INDEX assessment_attempt_one_active_invitation_idx
    ON assessment_attempts (invitation_id)
    WHERE status IN ('created', 'consented', 'ready', 'active', 'paused', 'submitted', 'processing');
CREATE INDEX assessment_attempt_candidate_created_idx
    ON assessment_attempts (candidate_user_id, created_at DESC, id);
