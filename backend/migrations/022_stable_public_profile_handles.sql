ALTER TABLE candidate_portfolios ADD COLUMN public_handle_locked_at TIMESTAMPTZ;

-- Existing handles may already have been shared before a later unpublish.
UPDATE candidate_portfolios
SET public_handle_locked_at = COALESCE(published_at, updated_at)
WHERE public_handle IS NOT NULL;
