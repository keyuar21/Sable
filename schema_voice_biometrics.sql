-- =============================================================================
-- Voice biometrics: per-user speaker embeddings (optional Supabase mirror)
-- Local SQLite in mock_auth is the source of truth; this table is a cloud sync.
-- Run in Supabase SQL Editor after schema_auth_update.sql
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.voice_embeddings (
    vpa TEXT PRIMARY KEY REFERENCES public.payer_accounts(vpa) ON DELETE CASCADE,
    embedding JSONB NOT NULL,
    sample_count INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.voice_embeddings ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Allow all on voice_embeddings" ON public.voice_embeddings;
CREATE POLICY "Allow all on voice_embeddings" ON public.voice_embeddings
    FOR ALL USING (true) WITH CHECK (true);
