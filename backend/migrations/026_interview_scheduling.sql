CREATE TABLE application_interviews (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL,
    application_id UUID NOT NULL,
    candidate_user_id UUID NOT NULL,
    interviewer_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    request_key UUID NOT NULL,
    request_payload_hash BYTEA NOT NULL,
    scheduled_at TIMESTAMPTZ NOT NULL,
    duration_minutes INTEGER NOT NULL CHECK (duration_minutes BETWEEN 15 AND 180),
    display_timezone TEXT NOT NULL CHECK (length(display_timezone) BETWEEN 1 AND 64),
    meeting_url TEXT CHECK (meeting_url IS NULL OR length(meeting_url) <= 2048),
    candidate_note TEXT NOT NULL DEFAULT '' CHECK (length(candidate_note) <= 1000),
    status TEXT NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'cancelled')),
    candidate_response TEXT NOT NULL DEFAULT 'pending'
        CHECK (candidate_response IN ('pending', 'accepted', 'declined', 'reschedule_requested')),
    response_message TEXT CHECK (response_message IS NULL OR length(response_message) <= 1000),
    responded_at TIMESTAMPTZ,
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    cancelled_by_user_id UUID REFERENCES users(id) ON DELETE RESTRICT,
    cancelled_at TIMESTAMPTZ,
    cancellation_reason TEXT CHECK (cancellation_reason IS NULL OR length(cancellation_reason) BETWEEN 10 AND 500),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, request_key),
    FOREIGN KEY (organization_id, application_id, candidate_user_id)
        REFERENCES job_applications (organization_id, id, user_id) ON DELETE RESTRICT,
    CHECK (
        (candidate_response = 'pending' AND responded_at IS NULL AND response_message IS NULL)
        OR (candidate_response <> 'pending' AND responded_at IS NOT NULL)
    ),
    CHECK (
        (status = 'scheduled' AND cancelled_by_user_id IS NULL AND cancelled_at IS NULL AND cancellation_reason IS NULL)
        OR (status = 'cancelled' AND cancelled_by_user_id IS NOT NULL AND cancelled_at IS NOT NULL AND cancellation_reason IS NOT NULL)
    )
);

CREATE INDEX application_interviews_candidate_time_idx
    ON application_interviews (candidate_user_id, scheduled_at DESC, id);
CREATE INDEX application_interviews_org_time_idx
    ON application_interviews (organization_id, scheduled_at DESC, id);
CREATE INDEX application_interviews_interviewer_time_idx
    ON application_interviews (interviewer_user_id, scheduled_at)
    WHERE status = 'scheduled' AND candidate_response NOT IN ('declined', 'reschedule_requested');
