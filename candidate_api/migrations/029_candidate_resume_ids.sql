ALTER TABLE candidate_resumes
    ADD COLUMN id UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE;
