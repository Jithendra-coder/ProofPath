ALTER TABLE candidate_portfolios
    ADD COLUMN public_handle TEXT CHECK (
        public_handle IS NULL OR public_handle ~ '^[a-z0-9][a-z0-9-]{1,28}[a-z0-9]$'
    ),
    ADD COLUMN published_at TIMESTAMPTZ,
    ADD COLUMN publication_consent_at TIMESTAMPTZ,
    ADD COLUMN publication_consent_version TEXT;

CREATE UNIQUE INDEX candidate_portfolios_public_handle_idx
    ON candidate_portfolios (lower(public_handle)) WHERE public_handle IS NOT NULL;

ALTER TABLE candidate_projects ADD COLUMN published_at TIMESTAMPTZ;
