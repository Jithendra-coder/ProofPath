BEGIN;

DO $rollback$
BEGIN
    IF EXISTS (SELECT 1 FROM assessment_attempts)
       OR EXISTS (
           SELECT 1 FROM audit_events
           WHERE resource_type = 'assessment_attempt'
       ) THEN
        RAISE EXCEPTION 'Assessment attempts or audit history exist; export them before rolling back this schema.';
    END IF;
END;
$rollback$;

DROP TABLE assessment_attempts;
ALTER TABLE assessment_invitations DROP CONSTRAINT assessment_invitations_attempt_scope_key;

COMMIT;
