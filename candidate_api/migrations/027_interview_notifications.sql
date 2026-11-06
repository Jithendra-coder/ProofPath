CREATE TABLE user_notifications (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    interview_id UUID NOT NULL REFERENCES application_interviews(id) ON DELETE RESTRICT,
    notification_type TEXT NOT NULL CHECK (
        notification_type IN ('interview_scheduled', 'interview_rescheduled', 'interview_cancelled', 'interview_response')
    ),
    source_version BIGINT NOT NULL CHECK (source_version > 0),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
    message TEXT NOT NULL CHECK (length(message) BETWEEN 1 AND 500),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at TIMESTAMPTZ,
    UNIQUE (user_id, interview_id, notification_type, source_version)
);

CREATE INDEX user_notifications_user_created_idx
    ON user_notifications (user_id, created_at DESC, id);
CREATE INDEX user_notifications_unread_idx
    ON user_notifications (user_id, created_at DESC)
    WHERE read_at IS NULL;
