CREATE TABLE candidate_onboardings (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    target_roles TEXT[] NOT NULL DEFAULT '{}'::text[] CHECK (cardinality(target_roles) <= 5),
    skills TEXT[] NOT NULL DEFAULT '{}'::text[] CHECK (cardinality(skills) <= 20),
    completed_at TIMESTAMPTZ,
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
