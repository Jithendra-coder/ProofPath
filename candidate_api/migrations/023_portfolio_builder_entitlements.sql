CREATE TABLE candidate_entitlement_grants (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    feature_key TEXT NOT NULL CHECK (feature_key = 'portfolio_customization'),
    grant_source TEXT NOT NULL CHECK (grant_source IN ('test', 'manual', 'billing')),
    starts_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (expires_at IS NULL OR expires_at > starts_at)
);

CREATE INDEX candidate_entitlement_grants_active_idx
    ON candidate_entitlement_grants (user_id, feature_key, starts_at, expires_at)
    WHERE revoked_at IS NULL;

CREATE TABLE candidate_portfolio_versions (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    version_number INTEGER NOT NULL CHECK (version_number >= 1),
    template_key TEXT NOT NULL CHECK (template_key IN ('editorial', 'engineer', 'case-study', 'compact-resume')),
    theme_key TEXT NOT NULL CHECK (theme_key IN ('coral', 'teal', 'ink')),
    sections JSONB NOT NULL CHECK (
        jsonb_typeof(sections) = 'array'
        AND jsonb_array_length(sections) BETWEEN 1 AND 3
        AND sections <@ '["roles", "skills", "projects"]'::jsonb
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ,
    UNIQUE (user_id, id),
    UNIQUE (user_id, version_number)
);

ALTER TABLE candidate_portfolios
    ADD COLUMN draft_version_id UUID,
    ADD COLUMN published_version_id UUID;
