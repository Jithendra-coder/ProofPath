ALTER TABLE candidate_competency_evidence
    ADD COLUMN proficiency_level TEXT CHECK (proficiency_level IS NULL OR proficiency_level IN ('junior', 'mid', 'senior')),
    ADD CONSTRAINT candidate_competency_evidence_candidate_id_key UNIQUE (candidate_user_id, id);

CREATE TABLE candidate_application_evidence_share_events (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL,
    application_id UUID NOT NULL,
    candidate_user_id UUID NOT NULL,
    version BIGINT NOT NULL CHECK (version > 0),
    action TEXT NOT NULL CHECK (action IN ('shared', 'revoked')),
    consent_version TEXT NOT NULL CHECK (consent_version = 'candidate-application-evidence-v1'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (application_id, version),
    UNIQUE (id, application_id, candidate_user_id),
    FOREIGN KEY (organization_id, application_id, candidate_user_id)
        REFERENCES job_applications (organization_id, id, user_id) ON DELETE RESTRICT
);

CREATE INDEX candidate_application_evidence_share_latest_idx
    ON candidate_application_evidence_share_events (application_id, version DESC);

CREATE TABLE candidate_application_evidence_share_items (
    share_event_id UUID NOT NULL,
    application_id UUID NOT NULL,
    candidate_user_id UUID NOT NULL,
    evidence_id UUID NOT NULL,
    PRIMARY KEY (share_event_id, evidence_id),
    FOREIGN KEY (share_event_id, application_id, candidate_user_id)
        REFERENCES candidate_application_evidence_share_events (id, application_id, candidate_user_id) ON DELETE RESTRICT,
    FOREIGN KEY (candidate_user_id, evidence_id)
        REFERENCES candidate_competency_evidence (candidate_user_id, id) ON DELETE RESTRICT
);

CREATE INDEX candidate_application_evidence_share_items_evidence_idx
    ON candidate_application_evidence_share_items (evidence_id, application_id);
