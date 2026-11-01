ALTER TABLE assessment_attempts
    ADD CONSTRAINT assessment_attempts_candidate_scope_key UNIQUE (id, candidate_user_id);

CREATE TABLE assessment_attempt_consent_events (
    id UUID PRIMARY KEY,
    attempt_id UUID NOT NULL,
    candidate_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    actor_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    attempt_version BIGINT NOT NULL CHECK (attempt_version > 1),
    action TEXT NOT NULL CHECK (action IN ('accepted', 'revoked')),
    consent_version TEXT NOT NULL CHECK (consent_version = 'assessment-attempt-v1'),
    recording_preferences JSONB NOT NULL CHECK (
        recording_preferences = '{"audio": false, "video": false, "screen": false, "transcript": false}'::jsonb
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (attempt_id, attempt_version),
    CHECK (actor_id = candidate_user_id),
    FOREIGN KEY (attempt_id, candidate_user_id)
        REFERENCES assessment_attempts (id, candidate_user_id) ON DELETE RESTRICT
);

CREATE INDEX assessment_attempt_consent_latest_idx
    ON assessment_attempt_consent_events (attempt_id, attempt_version DESC);
