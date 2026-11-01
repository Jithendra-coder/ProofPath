BEGIN;

DO $rollback$
BEGIN
    IF EXISTS (SELECT 1 FROM application_reviewer_assignments)
       OR EXISTS (
           SELECT 1 FROM audit_events
           WHERE resource_type = 'application_reviewer_assignment'
       ) THEN
        RAISE EXCEPTION 'Reviewer assignments or audit history exist; export them before rolling back this schema.';
    END IF;
END;
$rollback$;

DROP TABLE application_reviewer_assignments;
ALTER TABLE job_applications DROP CONSTRAINT job_applications_organization_application_key;

COMMIT;
