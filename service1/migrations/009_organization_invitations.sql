CREATE TABLE organization_invitations (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    invited_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    email TEXT NOT NULL CHECK (email = lower(email)),
    role TEXT NOT NULL CHECK (role IN ('admin', 'recruiter', 'member')),
    token_hash BYTEA NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    accepted_at TIMESTAMPTZ,
    accepted_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    cancelled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (accepted_at IS NULL OR cancelled_at IS NULL)
);

CREATE UNIQUE INDEX organization_invitations_pending_email_idx
    ON organization_invitations (organization_id, email)
    WHERE accepted_at IS NULL AND cancelled_at IS NULL;
CREATE INDEX organization_invitations_org_created_idx
    ON organization_invitations (organization_id, created_at DESC, id);
