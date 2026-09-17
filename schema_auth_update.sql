-- =============================================================================
-- Auth upgrade: PIN hashing + WebAuthn (Face ID / Touch ID / Windows Hello)
-- Run this AFTER schema.sql, in Supabase SQL Editor
-- =============================================================================

-- 1. Move payer_accounts off plaintext PIN → hashed PIN
--    (Old `pin` column stays for now so nothing breaks mid-migration; drop it later)
ALTER TABLE public.payer_accounts
    ADD COLUMN IF NOT EXISTS pin_hash TEXT,
    ADD COLUMN IF NOT EXISTS failed_pin_attempts INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS pin_locked_until TIMESTAMPTZ;

-- 2. WebAuthn credentials — one row per enrolled authenticator
--    (a user can have more than one: phone Face ID + laptop Touch ID, etc.)
CREATE TABLE IF NOT EXISTS public.webauthn_credentials (
    id BIGSERIAL PRIMARY KEY,
    vpa TEXT NOT NULL REFERENCES public.payer_accounts(vpa) ON DELETE CASCADE,
    credential_id TEXT UNIQUE NOT NULL,       -- base64url credential ID from the authenticator
    public_key TEXT NOT NULL,                 -- base64url COSE public key
    sign_count BIGINT NOT NULL DEFAULT 0,     -- replay-protection counter
    device_label TEXT,                        -- "iPhone Face ID", "MacBook Touch ID" etc.
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_webauthn_vpa ON public.webauthn_credentials(vpa);

-- 3. Short-lived step-up auth tokens — proof that WebAuthn/PIN just succeeded,
--    consumed once by the bank's /debit endpoint instead of a raw PIN.
CREATE TABLE IF NOT EXISTS public.auth_challenges (
    id BIGSERIAL PRIMARY KEY,
    vpa TEXT NOT NULL,
    challenge TEXT NOT NULL,          -- base64url random challenge sent to the browser
    purpose TEXT NOT NULL,            -- 'registration' | 'authentication'
    consumed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '2 minutes')
);

CREATE TABLE IF NOT EXISTS public.step_up_tokens (
    token TEXT PRIMARY KEY,           -- opaque random token, given to the browser after auth succeeds
    vpa TEXT NOT NULL,
    method TEXT NOT NULL,             -- 'webauthn' | 'pin'
    used BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '90 seconds')
);

ALTER TABLE public.webauthn_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.auth_challenges ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.step_up_tokens ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Allow all on webauthn_credentials" ON public.webauthn_credentials;
CREATE POLICY "Allow all on webauthn_credentials" ON public.webauthn_credentials FOR ALL USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS "Allow all on auth_challenges" ON public.auth_challenges;
CREATE POLICY "Allow all on auth_challenges" ON public.auth_challenges FOR ALL USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS "Allow all on step_up_tokens" ON public.step_up_tokens;
CREATE POLICY "Allow all on step_up_tokens" ON public.step_up_tokens FOR ALL USING (true) WITH CHECK (true);

-- 4. Seed a hashed PIN for the demo user (this hash = "1234", generated with bcrypt)
--    Generate your own with: python -c "import bcrypt; print(bcrypt.hashpw(b'1234', bcrypt.gensalt()).decode())"
-- UPDATE public.payer_accounts SET pin_hash = '<paste bcrypt hash here>' WHERE vpa = 'you@mockbank';
