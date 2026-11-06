BEGIN;

DO $rollback$
BEGIN
    IF EXISTS (SELECT 1 FROM assessment_invitations)
       OR EXISTS (
           SELECT 1 FROM audit_events
           WHERE resource_type = 'assessment_invitation'
       ) THEN
        RAISE EXCEPTION 'Assessment invitations or audit history exist; export them before rolling back this schema.';
    END IF;
END;
$rollback$;

DROP TABLE assessment_invitations;
ALTER TABLE organization_assessment_versions DROP CONSTRAINT organization_assessment_versions_tenant_key;
ALTER TABLE job_applications DROP CONSTRAINT job_applications_org_id_user_key;

COMMIT;
