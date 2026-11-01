CREATE TABLE rate_limit_buckets (
    key_hash BYTEA PRIMARY KEY CHECK (octet_length(key_hash) = 32),
    window_seconds INTEGER NOT NULL CHECK (window_seconds > 0),
    event_times TIMESTAMPTZ[] NOT NULL CHECK (cardinality(event_times) <= 1000),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX rate_limit_buckets_expiry_idx ON rate_limit_buckets (expires_at);
