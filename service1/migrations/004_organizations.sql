CREATE TABLE organizations (
    id UUID PRIMARY KEY,
    created_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    creation_key UUID NOT NULL,
    creation_payload_hash BYTEA NOT NULL,
    name TEXT NOT NULL CHECK (length(name) BETWEEN 2 AND 160),
    onboarding_completed_at TIMESTAMPTZ,
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (created_by_user_id, creation_key)
);

CREATE TABLE organization_memberships (
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'recruiter', 'member')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, user_id)
);

CREATE INDEX organization_memberships_user_idx ON organization_memberships (user_id, organization_id);
