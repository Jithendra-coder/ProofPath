CREATE TABLE candidate_project_source_links (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES candidate_projects(id) ON DELETE CASCADE,
    url TEXT NOT NULL CHECK (length(url) <= 300),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, url)
);

CREATE INDEX candidate_project_source_links_project_idx ON candidate_project_source_links (project_id, created_at, id);
