CREATE TABLE candidate_projects (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    request_key UUID NOT NULL,
    creation_payload_hash BYTEA NOT NULL,
    name TEXT NOT NULL CHECK (length(name) BETWEEN 2 AND 160),
    problem_statement TEXT NOT NULL DEFAULT '' CHECK (length(problem_statement) <= 2000),
    contribution TEXT NOT NULL DEFAULT '' CHECK (length(contribution) <= 4000),
    technologies TEXT[] NOT NULL DEFAULT '{}'::text[] CHECK (cardinality(technologies) <= 12),
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, request_key)
);

CREATE INDEX candidate_projects_owner_updated_idx ON candidate_projects (user_id, updated_at DESC, id);
