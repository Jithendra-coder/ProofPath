CREATE TABLE organization_jobs (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    created_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    request_key UUID NOT NULL,
    creation_payload_hash BYTEA NOT NULL,
    title TEXT NOT NULL CHECK (length(title) BETWEEN 3 AND 160),
    description TEXT NOT NULL CHECK (length(description) BETWEEN 20 AND 10000),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published', 'closed')),
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    published_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, request_key),
    CHECK ((status = 'draft' AND published_at IS NULL AND closed_at IS NULL)
        OR (status = 'published' AND published_at IS NOT NULL AND closed_at IS NULL)
        OR (status = 'closed' AND published_at IS NOT NULL AND closed_at IS NOT NULL))
);

CREATE INDEX organization_jobs_org_updated_idx ON organization_jobs (organization_id, updated_at DESC, id);
CREATE INDEX organization_jobs_public_idx ON organization_jobs (published_at DESC, id) WHERE status = 'published';

CREATE TABLE saved_jobs (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id UUID NOT NULL REFERENCES organization_jobs(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, job_id)
);

CREATE INDEX saved_jobs_user_created_idx ON saved_jobs (user_id, created_at DESC, job_id);
