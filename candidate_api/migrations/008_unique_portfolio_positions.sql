DROP INDEX candidate_projects_owner_order_idx;

ALTER TABLE candidate_projects ADD CONSTRAINT candidate_projects_user_sort_order_unique
    UNIQUE (user_id, sort_order) DEFERRABLE INITIALLY DEFERRED;
