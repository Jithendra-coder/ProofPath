ALTER TABLE organization_jobs
    ADD CONSTRAINT organization_jobs_org_id_id_key UNIQUE (organization_id, id);

CREATE TABLE job_applications (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL,
    job_id UUID NOT NULL,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    request_key UUID NOT NULL,
    request_payload_hash BYTEA NOT NULL,
    status TEXT NOT NULL DEFAULT 'submitted' CHECK (
        status IN ('submitted', 'in_review', 'assessment', 'interview', 'offer', 'hired', 'rejected', 'withdrawn')
    ),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    portfolio_consent_version TEXT NOT NULL CHECK (portfolio_consent_version = 'job-application-public-portfolio-v1'),
    portfolio_consented_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    portfolio_snapshot JSONB NOT NULL CHECK (jsonb_typeof(portfolio_snapshot) = 'object'),
    job_snapshot JSONB NOT NULL CHECK (jsonb_typeof(job_snapshot) = 'object'),
    cover_note TEXT NOT NULL DEFAULT '' CHECK (length(cover_note) <= 2000),
    decision_reason TEXT,
    decided_by_user_id UUID REFERENCES users(id) ON DELETE RESTRICT,
    decided_at TIMESTAMPTZ,
    withdrawn_at TIMESTAMPTZ,
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, request_key),
    FOREIGN KEY (organization_id, job_id)
        REFERENCES organization_jobs (organization_id, id) ON DELETE RESTRICT,
    CHECK (
        (status IN ('hired', 'rejected') AND decided_by_user_id IS NOT NULL
            AND decided_at IS NOT NULL AND decision_reason IS NOT NULL)
        OR (status NOT IN ('hired', 'rejected') AND decided_by_user_id IS NULL
            AND decided_at IS NULL AND decision_reason IS NULL)
    ),
    CHECK ((status = 'withdrawn') = (withdrawn_at IS NOT NULL))
);

CREATE UNIQUE INDEX job_applications_one_active_per_job_idx
    ON job_applications (user_id, job_id) WHERE status <> 'withdrawn';
CREATE INDEX job_applications_org_job_status_idx
    ON job_applications (organization_id, job_id, submitted_at DESC)
    WHERE status <> 'withdrawn';
CREATE INDEX job_applications_user_submitted_idx
    ON job_applications (user_id, submitted_at DESC, id);

CREATE TABLE application_events (
    event_number BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    id UUID NOT NULL UNIQUE,
    application_id UUID NOT NULL REFERENCES job_applications(id) ON DELETE RESTRICT,
    actor_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('candidate', 'organization')),
    action TEXT NOT NULL CHECK (action IN ('submitted', 'stage_changed', 'decision_recorded', 'withdrawn')),
    from_status TEXT,
    to_status TEXT NOT NULL CHECK (
        to_status IN ('submitted', 'in_review', 'assessment', 'interview', 'offer', 'hired', 'rejected', 'withdrawn')
    ),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (application_id, event_number)
);

CREATE INDEX application_events_history_idx ON application_events (application_id, event_number);
