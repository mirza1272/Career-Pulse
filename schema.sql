-- =====================================================================
-- Career Pulse & Radar — Supabase PostgreSQL Schema & Multi-User Migration
-- Target: Supabase Dashboard -> SQL Editor
-- Description:
--   1. Creates `users` table with encrypted SMTP credentials & Knowledge Base JSON.
--   2. Updates `applications` table with `user_id` and `ats_note` columns.
--   3. Creates `jobs` table (Radar job discovery engine).
--   4. Creates performance indexes and seeds the system administrator.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. USERS TABLE (Multi-User Authentication & Profile Management)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.users (
    id BIGSERIAL PRIMARY KEY,
    email VARCHAR(320) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    name VARCHAR(200) DEFAULT '',
    is_active BOOLEAN DEFAULT TRUE,
    
    -- Per-user SMTP credentials (encrypted app passwords)
    smtp_host VARCHAR(255) DEFAULT 'smtp.gmail.com',
    smtp_port INTEGER DEFAULT 587,
    smtp_username VARCHAR(320) DEFAULT '',
    smtp_password_encrypted TEXT DEFAULT '',
    sender_name VARCHAR(200) DEFAULT '',
    smtp_verified BOOLEAN DEFAULT FALSE,
    
    -- Dynamic Zero-Force Knowledge Base Profile (JSON)
    knowledge_base_json TEXT DEFAULT '{}',
    
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_email ON public.users(email);
CREATE INDEX IF NOT EXISTS idx_users_is_active ON public.users(is_active);


-- ---------------------------------------------------------------------
-- 2. JOBS TABLE (Radar Multi-Platform Aggregator)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.jobs (
    id BIGSERIAL PRIMARY KEY,
    dedup_key VARCHAR(120) UNIQUE NOT NULL,
    title VARCHAR(400) DEFAULT '',
    company VARCHAR(300) DEFAULT '',
    location VARCHAR(300) DEFAULT '',
    link TEXT NOT NULL,
    has_email BOOLEAN DEFAULT FALSE,
    email VARCHAR(320),
    jd_text TEXT DEFAULT '',
    deadline TIMESTAMPTZ,
    status VARCHAR(20) DEFAULT 'active',
    source VARCHAR(80) DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON public.jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_dedup_key ON public.jobs(dedup_key);


-- ---------------------------------------------------------------------
-- 3. APPLICATIONS TABLE (Approval Queue & Dispatch)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.applications (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES public.users(id) ON DELETE SET NULL,
    job_id BIGINT REFERENCES public.jobs(id) ON DELETE SET NULL,
    email VARCHAR(320) NOT NULL,
    job_title VARCHAR(400) DEFAULT '',
    company VARCHAR(300) DEFAULT '',
    link TEXT DEFAULT '',
    jd_text TEXT DEFAULT '',
    drafted_email TEXT DEFAULT '',
    subject VARCHAR(400) DEFAULT '',
    resume_path TEXT DEFAULT '',
    ats_score DOUBLE PRECISION DEFAULT 0.0,
    ats_attempts INTEGER DEFAULT 0,
    ats_note TEXT DEFAULT '',
    status VARCHAR(20) DEFAULT 'pending',
    disposition VARCHAR(40) DEFAULT '',
    sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Safely add multi-user columns if table already existed from earlier versions
ALTER TABLE public.applications 
ADD COLUMN IF NOT EXISTS user_id BIGINT REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE public.applications 
ADD COLUMN IF NOT EXISTS ats_note TEXT DEFAULT '';

-- Phase 10 (2026-09-22): manual resume-edit locks (§U.13). Stores the user's
-- edited regions as JSON ({REGION_NAME: final region HTML}) so recreating a
-- resume re-applies the user's edits instead of wiping them. Additive only.
-- SUPABASE MANUAL ACTION REQUIRED: run this once in the Supabase SQL editor.
ALTER TABLE public.applications
ADD COLUMN IF NOT EXISTS resume_locks_json TEXT NOT NULL DEFAULT '{}';

-- Performance indexes
CREATE INDEX IF NOT EXISTS idx_applications_user_id ON public.applications(user_id);
CREATE INDEX IF NOT EXISTS idx_applications_status ON public.applications(status);
CREATE INDEX IF NOT EXISTS idx_applications_job_id ON public.applications(job_id);


-- ---------------------------------------------------------------------
-- 4. SEED SYSTEM ADMINISTRATOR ACCOUNT
-- ---------------------------------------------------------------------
-- Seeds the initial administrator account (mirzahaseeb0566@gmail.com)
-- NOTE (2026-09-22): the admin password hash below is intentionally an invalid
-- placeholder. The initial admin account is bootstrapped by the application
-- (app/db.py seed_admin_user) ONLY when AUTH_PASSWORD is explicitly set in the
-- environment. There is no default password. A previously committed
-- pre-computed hash was removed as a credential-exposure risk.
INSERT INTO public.users (
    email,
    password_hash,
    name,
    is_active,
    smtp_host,
    smtp_port,
    smtp_username,
    sender_name,
    smtp_verified,
    knowledge_base_json
)
VALUES (
    'mirzahaseeb0566@gmail.com',
    '__INVALID__SET_AUTH_PASSWORD_ENV__',
    'Haseeb Ur Rahman',
    TRUE,
    'smtp.gmail.com',
    587,
    'mirzahaseeb0566@gmail.com',
    'Haseeb Ur Rahman',
    TRUE,
    '{}'
)
ON CONFLICT (email) DO NOTHING;

-- REMOVED (2026-09-22): blind backfill of NULL user_id applications to the
-- admin account. Ownership must be established by investigation first — see
-- the listing query in the migrations section below. New application rows
-- always receive a user_id server-side.
-- UPDATE public.applications
-- SET user_id = (SELECT id FROM public.users WHERE email = 'mirzahaseeb0566@gmail.com' LIMIT 1)
-- WHERE user_id IS NULL;


-- ---------------------------------------------------------------------
-- 5. ACCESS PRIVILEGES (Supabase PostgREST & Service Role Permissions)
-- ---------------------------------------------------------------------
-- 2026-09-22: anon/authenticated are intentionally NOT granted access.
-- The application's server-side sync uses the service_role key, which
-- bypasses Row Level Security. RLS is enabled (see migration A below) with
-- no permissive policies: deny-by-default for anon/authenticated.
GRANT ALL ON TABLE public.users TO postgres, service_role;
GRANT ALL ON TABLE public.applications TO postgres, service_role;
GRANT ALL ON TABLE public.jobs TO postgres, service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO postgres, service_role;


-- =====================================================================
-- 6. MIGRATIONS — 2026-09-22 CareerPulse security & workflow upgrade
-- =====================================================================
-- Each migration below is independent and idempotent. They are safe for
-- existing data: no rows are deleted; only additive changes plus the
-- explicitly approved status and privilege tightening.

-- ============================================================
-- SUPABASE MANUAL ACTION REQUIRED
-- Run the following SQL in Supabase SQL Editor.
-- ============================================================
-- MIGRATION A: Row Level Security lockdown (existing databases).
-- What: enables RLS on users/applications/jobs with NO permissive
--   policies (deny-by-default for anon/authenticated), and revokes the
--   previous GRANT ALL TO anon over-grant.
-- Why: the old schema granted the anonymous PostgREST key full read/write
--   on password hashes, encrypted SMTP credentials, and KB data.
-- Safe: the app's server-side sync uses the service_role key, which
--   bypasses RLS, so sync keeps working. No rows modified.

ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.applications ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.jobs ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.users FROM anon;
REVOKE ALL ON TABLE public.applications FROM anon;
REVOKE ALL ON TABLE public.jobs FROM anon;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon;
REVOKE ALL ON TABLE public.users FROM authenticated;
REVOKE ALL ON TABLE public.applications FROM authenticated;
REVOKE ALL ON TABLE public.jobs FROM authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM authenticated;

-- ============================================================
-- SUPABASE MANUAL ACTION REQUIRED
-- Run the following SQL in Supabase SQL Editor.
-- ============================================================
-- MIGRATION B: resume version history table.
-- What: new table resume_versions storing every generated resume version
--   per application (version number, role, KB snapshot hash, paths,
--   ATS score, iteration count, template id).
-- Safe: brand-new table; existing data untouched.

CREATE TABLE IF NOT EXISTS public.resume_versions (
    id BIGSERIAL PRIMARY KEY,
    application_id BIGINT REFERENCES public.applications(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL,
    role TEXT,
    jd_text TEXT,
    kb_snapshot_hash TEXT,
    resume_path TEXT,
    pdf_path TEXT,
    ats_score DOUBLE PRECISION,
    iterations INTEGER NOT NULL DEFAULT 1,
    score_history_json TEXT NOT NULL DEFAULT '[]',
    template_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (application_id, version_no)
);
-- NOTE (Phase 8, 2026-09-22): jd_text and score_history_json were added to this
-- pending migration before it was ever applied remotely (the table does not
-- exist in Supabase yet), so no data migration is needed — run the block once.
CREATE INDEX IF NOT EXISTS idx_resume_versions_application
    ON public.resume_versions(application_id);
ALTER TABLE public.resume_versions ENABLE ROW LEVEL SECURITY;
GRANT ALL ON TABLE public.resume_versions TO postgres, service_role;

-- ============================================================
-- SUPABASE MANUAL ACTION REQUIRED
-- Run the following SQL in Supabase SQL Editor.
-- ============================================================
-- MIGRATION C: approval-workflow statuses.
-- What: existing 'pending' rows become 'draft' so they are re-reviewed
--   under the explicit approval flow:
--   draft -> ready -> pending_approval -> approved -> sent.
--   The send endpoint rejects anything not 'approved'.
-- Safe: 'sent' rows and dispositions are untouched; only the label of
--   not-yet-sent rows changes.

UPDATE public.applications SET status = 'draft' WHERE status = 'pending';

-- ============================================================
-- SUPABASE MANUAL ACTION REQUIRED
-- Run the following SQL in Supabase SQL Editor.
-- ============================================================
-- MIGRATION D: PDF page-target preference (Phase 12, §U.8).
-- What: adds applications.pdf_page_target (1 or 2, default 1) storing the
--   user's chosen resume PDF page count.
-- Safe: additive column with a default; existing rows read as 1-page.

ALTER TABLE public.applications
ADD COLUMN IF NOT EXISTS pdf_page_target INTEGER NOT NULL DEFAULT 1;

-- ============================================================
-- SUPABASE MANUAL ACTION REQUIRED
-- Run the following SQL in Supabase SQL Editor and share the output
-- (it contains no secrets).
-- ============================================================
-- INVESTIGATION: list ownerless applications. Ownership must be
-- established before any backfill. Do NOT blindly assign these rows.
SELECT id, job_title, company, status, disposition, created_at
FROM public.applications
WHERE user_id IS NULL
ORDER BY created_at DESC;
