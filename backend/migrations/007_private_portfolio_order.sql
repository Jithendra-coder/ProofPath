ALTER TABLE candidate_projects ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 1 CHECK (sort_order >= 1);

WITH ordered AS (
    SELECT id, row_number() OVER (PARTITION BY user_id ORDER BY created_at, id)::integer AS position
    FROM candidate_projects
)
UPDATE candidate_projects AS project SET sort_order = ordered.position
FROM ordered WHERE ordered.id = project.id;

CREATE INDEX candidate_projects_owner_order_idx ON candidate_projects (user_id, sort_order, id);

CREATE TABLE candidate_portfolios (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
