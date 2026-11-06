CREATE TABLE organization_assessments (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE RESTRICT,
    created_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    request_key UUID NOT NULL,
    creation_payload_hash BYTEA NOT NULL,
    definition JSONB NOT NULL CHECK (jsonb_typeof(definition) = 'object'),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published', 'archived')),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    published_at TIMESTAMPTZ,
    archived_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, request_key),
    UNIQUE (organization_id, id),
    CHECK (
        (status = 'draft' AND published_at IS NULL AND archived_at IS NULL)
        OR (status = 'published' AND published_at IS NOT NULL AND archived_at IS NULL)
        OR (status = 'archived' AND published_at IS NOT NULL AND archived_at IS NOT NULL)
    )
);

CREATE INDEX organization_assessments_org_updated_idx
    ON organization_assessments (organization_id, updated_at DESC, id);

CREATE TABLE organization_assessment_versions (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL,
    assessment_id UUID NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    definition JSONB NOT NULL CHECK (jsonb_typeof(definition) = 'object'),
    published_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (assessment_id, version_number),
    FOREIGN KEY (organization_id, assessment_id)
        REFERENCES organization_assessments (organization_id, id) ON DELETE RESTRICT
);

CREATE INDEX organization_assessment_versions_latest_idx
    ON organization_assessment_versions (assessment_id, version_number DESC);
