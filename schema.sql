-- =============================================================================
-- Voice UPI Simulator — Supabase PostgreSQL Database Schema
-- Run this script in the Supabase Dashboard -> SQL Editor (or via CLI / psql)
-- Project: https://sanozvdqvezownhvhpdq.supabase.co
-- =============================================================================

-- 1. Payer Accounts (Payer Bank - @mockbank)
CREATE TABLE IF NOT EXISTS public.payer_accounts (
    vpa TEXT PRIMARY KEY,
    holder_name TEXT NOT NULL,
    balance NUMERIC(12, 2) NOT NULL DEFAULT 50000.00,
    pin TEXT NOT NULL DEFAULT '1234',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. Payee / Merchant Accounts (Payee Bank - @mockbank2)
CREATE TABLE IF NOT EXISTS public.payee_accounts (
    vpa TEXT PRIMARY KEY,
    merchant_name TEXT NOT NULL,
    balance NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    category TEXT DEFAULT 'general',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 3. Transactions (Full End-to-End Distributed Ledger)
CREATE TABLE IF NOT EXISTS public.transactions (
    id BIGSERIAL PRIMARY KEY,
    txn_id TEXT UNIQUE NOT NULL,
    payer_vpa TEXT NOT NULL REFERENCES public.payer_accounts(vpa) ON DELETE CASCADE,
    payee_vpa TEXT NOT NULL REFERENCES public.payee_accounts(vpa) ON DELETE CASCADE,
    amount NUMERIC(12, 2) NOT NULL,
    currency TEXT NOT NULL DEFAULT 'INR',
    status TEXT NOT NULL, -- initiated, debiting, debited, crediting, credited, success, failed, settled, partial_failure, timeout
    hops JSONB DEFAULT '{}'::jsonb,
    hop_log JSONB DEFAULT '[]'::jsonb,
    error TEXT,
    error_stage TEXT,
    settlement_batch_id TEXT,
    payer_balance_after NUMERIC(12, 2),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

-- 4. Settlement Batches (NPCI Multilateral Net Settlement)
CREATE TABLE IF NOT EXISTS public.settlement_batches (
    id BIGSERIAL PRIMARY KEY,
    cycle_id TEXT UNIQUE NOT NULL,
    cycle_number INT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    debit_count INT NOT NULL DEFAULT 0,
    credit_count INT NOT NULL DEFAULT 0,
    total_debit_settled NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    total_credit_settled NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    net_obligation NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    from_bank TEXT NOT NULL,
    to_bank TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'SETTLED'
);

-- 5. NPCI Switch Routing Logs
CREATE TABLE IF NOT EXISTS public.route_logs (
    id BIGSERIAL PRIMARY KEY,
    txn_id TEXT NOT NULL,
    payer_vpa TEXT NOT NULL,
    payee_vpa TEXT NOT NULL,
    payer_bank_url TEXT,
    payee_bank_url TEXT,
    status TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Performance Indexes
CREATE INDEX IF NOT EXISTS idx_transactions_txn_id ON public.transactions(txn_id);
CREATE INDEX IF NOT EXISTS idx_transactions_payer ON public.transactions(payer_vpa);
CREATE INDEX IF NOT EXISTS idx_transactions_payee ON public.transactions(payee_vpa);
CREATE INDEX IF NOT EXISTS idx_transactions_status ON public.transactions(status);
CREATE INDEX IF NOT EXISTS idx_transactions_created_at ON public.transactions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_settlement_batches_cycle_id ON public.settlement_batches(cycle_id);

-- Enable RLS and permissive policies for backend microservices & frontend
ALTER TABLE public.payer_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.payee_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.settlement_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.route_logs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Allow all on payer_accounts" ON public.payer_accounts;
CREATE POLICY "Allow all on payer_accounts" ON public.payer_accounts FOR ALL USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS "Allow all on payee_accounts" ON public.payee_accounts;
CREATE POLICY "Allow all on payee_accounts" ON public.payee_accounts FOR ALL USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS "Allow all on transactions" ON public.transactions;
CREATE POLICY "Allow all on transactions" ON public.transactions FOR ALL USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS "Allow all on settlement_batches" ON public.settlement_batches;
CREATE POLICY "Allow all on settlement_batches" ON public.settlement_batches FOR ALL USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS "Allow all on route_logs" ON public.route_logs;
CREATE POLICY "Allow all on route_logs" ON public.route_logs FOR ALL USING (true) WITH CHECK (true);

-- Seed Initial Payer Accounts
INSERT INTO public.payer_accounts (vpa, holder_name, balance, pin)
VALUES
    ('you@mockbank',   'Keyur Arbhelonde', 50000.00, '1234'),
    ('priya@mockbank', 'Priya Sharma',      35000.00, '1234'),
    ('rahul@mockbank', 'Rahul Mehta',        22000.00, '1234')
ON CONFLICT (vpa) DO NOTHING;

-- Seed Initial Payee / Merchant Accounts
INSERT INTO public.payee_accounts (vpa, merchant_name, balance, category)
VALUES
    ('sharmastore@mockbank2', 'Sharma Store',   0.00, 'grocery'),
    ('blinkit@mockbank2',     'Blinkit',        0.00, 'delivery'),
    ('swiggy@mockbank2',      'Swiggy',         0.00, 'food'),
    ('zomato@mockbank2',      'Zomato',         0.00, 'food'),
    ('amazon@mockbank2',      'Amazon',         0.00, 'ecommerce'),
    ('petrolpump@mockbank2',  'HP Petrol Pump', 0.00, 'fuel')
ON CONFLICT (vpa) DO NOTHING;
