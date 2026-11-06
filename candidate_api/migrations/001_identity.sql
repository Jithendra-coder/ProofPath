CREATE TABLE users (
    id UUID PRIMARY KEY,
    full_name TEXT NOT NULL CHECK (length(full_name) BETWEEN 1 AND 120),
    email TEXT NOT NULL UNIQUE CHECK (email = lower(email)),
    password_hash TEXT NOT NULL,
    email_verified_at TIMESTAMPTZ,
    initial_purpose TEXT NOT NULL CHECK (initial_purpose IN ('candidate', 'employer')),
    terms_version TEXT NOT NULL,
    terms_accepted_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE email_verification_tokens (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash BYTEA NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX email_verification_tokens_user_expiry_idx ON email_verification_tokens (user_id, expires_at);

CREATE TABLE auth_sessions (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_token_hash BYTEA NOT NULL UNIQUE,
    csrf_token_hash BYTEA NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX auth_sessions_user_active_idx ON auth_sessions (user_id, expires_at) WHERE revoked_at IS NULL;

CREATE TABLE audit_events (
    id UUID PRIMARY KEY,
    actor_id UUID REFERENCES users(id) ON DELETE SET NULL,
    actor_type TEXT NOT NULL,
    tenant_id UUID,
    resource_type TEXT NOT NULL,
    resource_id UUID,
    action TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    correlation_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('success', 'denied', 'failure')),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX audit_events_resource_time_idx ON audit_events (resource_type, resource_id, occurred_at DESC);
