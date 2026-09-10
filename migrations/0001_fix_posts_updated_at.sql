-- 0001: fix posts.updated_at so INSERT works.
-- Root cause: updated_at was NOT NULL with no default (onupdate only fires
-- on UPDATE), so POST /api/posts failed with NotNullViolation on the live DB.
-- Applied: 2026-09-10 (pending live deploy approval)

ALTER TABLE public.posts ALTER COLUMN updated_at SET DEFAULT now();

-- Backfill any rows is not needed; existing rows already have values.
-- Verify:
--   INSERT INTO posts (title, slug, body_md) VALUES ('t','t-slug','b') RETURNING id;
--   DELETE FROM posts WHERE slug = 't-slug';
