BEGIN;

DO $rollback$
BEGIN
    IF EXISTS (SELECT 1 FROM organization_assessment_versions)
       OR EXISTS (SELECT 1 FROM organization_assessments)
       OR EXISTS (
           SELECT 1 FROM audit_events
           WHERE resource_type = 'organization_assessment'
       ) THEN
        RAISE EXCEPTION 'Assessment data or audit history exists; export it before rolling back this schema.';
    END IF;
END;
$rollback$;

DROP TABLE organization_assessment_versions;
DROP TABLE organization_assessments;

COMMIT;
