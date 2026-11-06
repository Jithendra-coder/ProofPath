CREATE TABLE candidate_resumes (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    filename TEXT NOT NULL CHECK (length(filename) BETWEEN 1 AND 180),
    media_type TEXT NOT NULL CHECK (media_type IN (
        'application/pdf',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'text/plain'
    )),
    sha256 CHAR(64) NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size BETWEEN 1 AND 5242880),
    content BYTEA NOT NULL,
    extracted_text TEXT NOT NULL CHECK (length(extracted_text) BETWEEN 40 AND 100000),
    extracted_data JSONB NOT NULL CHECK (jsonb_typeof(extracted_data) = 'object'),
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (byte_size = octet_length(content))
);
