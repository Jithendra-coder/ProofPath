ALTER TABLE candidate_portfolios
    ADD COLUMN discoverable_at TIMESTAMPTZ,
    ADD COLUMN discovery_consent_at TIMESTAMPTZ,
    ADD COLUMN discovery_consent_version TEXT,
    ADD CONSTRAINT candidate_portfolios_discovery_consent_check CHECK (
        (discoverable_at IS NULL AND discovery_consent_at IS NULL AND discovery_consent_version IS NULL)
        OR
        (discoverable_at IS NOT NULL AND discovery_consent_at IS NOT NULL
         AND discovery_consent_version = 'candidate-discovery-v1')
    );

CREATE INDEX candidate_portfolios_discoverable_idx
    ON candidate_portfolios (discoverable_at DESC, user_id)
    WHERE discoverable_at IS NOT NULL AND published_at IS NOT NULL;
