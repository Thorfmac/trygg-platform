-- =============================================================
-- TRYGG PLATFORM — PostgreSQL Schema v1.0
-- =============================================================
-- Three namespaces:
--   platform.*  — shared across all modules (clients, config, jobs)
--   mimir.*     — investment intelligence module
--   lex.*       — regulatory intelligence module
--
-- Design principles:
--   - PostgreSQL-native types throughout (JSONB, TIMESTAMPTZ, ENUM)
--   - Soft deletes via is_active / deleted_at — nothing is hard-deleted
--   - All timestamps in UTC (TIMESTAMPTZ)
--   - Foreign keys enforced — referential integrity is non-negotiable
--   - JSONB for flexible metadata that varies per record type
--   - Schema-namespaced so modules are isolated but share one DB
-- =============================================================


-- =============================================================
-- EXTENSIONS
-- =============================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";   -- uuid_generate_v4()
CREATE EXTENSION IF NOT EXISTS "pg_trgm";     -- fuzzy text search on company names


-- =============================================================
-- SCHEMAS
-- =============================================================

CREATE SCHEMA IF NOT EXISTS platform;
CREATE SCHEMA IF NOT EXISTS mimir;
CREATE SCHEMA IF NOT EXISTS lex;


-- =============================================================
-- ENUMS
-- =============================================================

-- Client tier controls which Claude model tier they receive
CREATE TYPE platform.client_tier AS ENUM (
    'standard',     -- local model only, full privacy
    'premium'       -- Claude API synthesis, client has consented
);

-- Job status for the scheduler
CREATE TYPE platform.job_status AS ENUM (
    'pending',
    'running',
    'completed',
    'failed',
    'skipped'
);

-- Company development stage
CREATE TYPE mimir.company_stage AS ENUM (
    'private_early',    -- seed / Series A
    'private_growth',   -- Series B+
    'pre_ipo',          -- actively filing / roadshowing
    'public',           -- listed
    'acquired'          -- no longer independent
);

-- Signal types for Mímir
CREATE TYPE mimir.signal_type AS ENUM (
    'news',
    'funding_round',
    'regulatory_filing',
    'patent',
    'leadership_change',
    'partnership',
    'product_launch',
    'ipo_signal',       -- specific pre-IPO indicator
    'other'
);

-- IPO signal strength — used in the IPO proximity tracker
CREATE TYPE mimir.ipo_signal_strength AS ENUM (
    'weak',
    'moderate',
    'strong',
    'confirmed'
);

-- Regulatory body source for Lex
CREATE TYPE lex.regulator AS ENUM (
    'fca',
    'pra',
    'esma',
    'fatf',
    'ico',
    'dora',
    'bis',
    'hmrc',
    'parliament',
    'other'
);

-- Regulatory item type
CREATE TYPE lex.item_type AS ENUM (
    'policy_statement',
    'consultation_paper',
    'guidance',
    'technical_standard',
    'enforcement_action',
    'speech',
    'legislation',
    'other'
);


-- =============================================================
-- PLATFORM SCHEMA — shared foundation
-- =============================================================

-- Clients
-- "Not sure yet" on differentiation is handled by the tier field
-- and the flexible metadata JSONB. Per-client watchlists vs shared
-- is resolved by the mimir.watchlist_membership join table below.
CREATE TABLE platform.clients (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name                TEXT NOT NULL,
    contact_email       TEXT NOT NULL,
    telegram_chat_id    TEXT,                   -- NULL until they connect Telegram
    tier                platform.client_tier NOT NULL DEFAULT 'standard',
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    metadata            JSONB NOT NULL DEFAULT '{}',
    -- metadata examples:
    --   {"firm_type": "ifa", "aum_band": "10m-50m", "fca_ref": "123456"}
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_clients_telegram ON platform.clients (telegram_chat_id)
    WHERE telegram_chat_id IS NOT NULL;
CREATE INDEX idx_clients_active ON platform.clients (is_active);


-- Scheduler job log
-- Every agent run is recorded here — provides full audit trail
-- and the basis for alerting on failed jobs
CREATE TABLE platform.job_runs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_name        TEXT NOT NULL,          -- e.g. 'mimir.feed_scan', 'lex.fca_ingest'
    job_module      TEXT NOT NULL,          -- 'mimir' | 'lex' | 'platform'
    status          platform.job_status NOT NULL DEFAULT 'pending',
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    error_message   TEXT,
    records_processed INTEGER,
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_job_runs_name_status ON platform.job_runs (job_name, status);
CREATE INDEX idx_job_runs_created ON platform.job_runs (created_at DESC);


-- Notification log
-- Every Telegram message sent, regardless of module
CREATE TABLE platform.notifications (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    client_id       UUID NOT NULL REFERENCES platform.clients (id),
    module          TEXT NOT NULL,          -- 'mimir' | 'lex'
    notification_type TEXT NOT NULL,        -- 'alert' | 'digest' | 'report'
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,
    sent_at         TIMESTAMPTZ,
    delivery_status TEXT NOT NULL DEFAULT 'pending',
    metadata        JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_notifications_client ON platform.notifications (client_id, sent_at DESC);


-- =============================================================
-- MÍMIR SCHEMA — investment intelligence
-- =============================================================

-- Companies on the watchlist
CREATE TABLE mimir.companies (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            TEXT NOT NULL,
    slug            TEXT NOT NULL UNIQUE,   -- url-safe identifier e.g. 'pqshield'
    ticker          TEXT,                   -- NULL if private
    sector          TEXT NOT NULL,          -- e.g. 'post-quantum cryptography'
    subsector       TEXT,
    country         TEXT NOT NULL,          -- ISO 3166-1 alpha-2 e.g. 'GB'
    stage           mimir.company_stage NOT NULL DEFAULT 'private_early',
    website         TEXT,
    description     TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    metadata        JSONB NOT NULL DEFAULT '{}',
    -- metadata examples:
    --   {"founded": 2018, "employees_band": "50-100",
    --    "investors": ["Oxnead", "Addition"], "patents": 30,
    --    "ncsc_pilot": true}
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_companies_slug ON mimir.companies (slug);
CREATE INDEX idx_companies_sector ON mimir.companies (sector);
CREATE INDEX idx_companies_stage ON mimir.companies (stage);
CREATE INDEX idx_companies_name_trgm ON mimir.companies
    USING GIN (name gin_trgm_ops);  -- fuzzy name search


-- Key people associated with companies
CREATE TABLE mimir.people (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    company_id      UUID NOT NULL REFERENCES mimir.companies (id),
    name            TEXT NOT NULL,
    role            TEXT NOT NULL,          -- e.g. 'CEO', 'CTO', 'Founder'
    linkedin_url    TEXT,
    is_current      BOOLEAN NOT NULL DEFAULT TRUE,
    joined_date     DATE,
    left_date       DATE,
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_people_company ON mimir.people (company_id, is_current);


-- Funding rounds
CREATE TABLE mimir.funding_rounds (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    company_id      UUID NOT NULL REFERENCES mimir.companies (id),
    round_type      TEXT NOT NULL,          -- 'Seed', 'Series A', 'Series B', etc.
    amount_usd      BIGINT,                 -- NULL if undisclosed
    announced_date  DATE NOT NULL,
    investors       TEXT[],                 -- array of investor names
    lead_investor   TEXT,
    source_url      TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_funding_company ON mimir.funding_rounds (company_id, announced_date DESC);


-- Watchlists
-- A watchlist is a named collection of companies.
-- At launch you'll have one: 'PQC Core Watchlist'
-- Per-client watchlists are created as additional rows.
CREATE TABLE mimir.watchlists (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            TEXT NOT NULL,
    description     TEXT,
    is_global       BOOLEAN NOT NULL DEFAULT FALSE,
    -- is_global = TRUE means all clients see this watchlist
    -- is_global = FALSE means it's private to a specific client
    owner_client_id UUID REFERENCES platform.clients (id),
    -- NULL when is_global = TRUE
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT watchlist_ownership_check CHECK (
        (is_global = TRUE AND owner_client_id IS NULL) OR
        (is_global = FALSE AND owner_client_id IS NOT NULL)
    )
);


-- Watchlist membership — companies on a watchlist
CREATE TABLE mimir.watchlist_companies (
    watchlist_id    UUID NOT NULL REFERENCES mimir.watchlists (id),
    company_id      UUID NOT NULL REFERENCES mimir.companies (id),
    added_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    added_reason    TEXT,
    PRIMARY KEY (watchlist_id, company_id)
);


-- Client watchlist subscriptions
-- Resolves "not sure yet" — a client can follow any combination
-- of global and private watchlists
CREATE TABLE mimir.client_watchlist_subscriptions (
    client_id       UUID NOT NULL REFERENCES platform.clients (id),
    watchlist_id    UUID NOT NULL REFERENCES mimir.watchlists (id),
    subscribed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (client_id, watchlist_id)
);


-- Signals — the core intelligence feed
-- Every piece of information about a company is a signal.
-- The AI scores each one and classifies its type.
CREATE TABLE mimir.signals (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    company_id      UUID NOT NULL REFERENCES mimir.companies (id),
    signal_type     mimir.signal_type NOT NULL,
    headline        TEXT NOT NULL,
    summary         TEXT,                   -- AI-generated summary
    source_url      TEXT,
    source_name     TEXT,                   -- e.g. 'TechCrunch', 'FCA Register'
    published_at    TIMESTAMPTZ,            -- original publication time
    discovered_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- AI scoring
    relevance_score SMALLINT CHECK (relevance_score BETWEEN 1 AND 10),
    sentiment       TEXT CHECK (sentiment IN ('positive', 'negative', 'neutral')),
    investment_implication TEXT,            -- AI one-line: strengthens/weakens thesis
    model_used      TEXT,                   -- 'haiku-4-5' | 'sonnet-4-6'
    scored_at       TIMESTAMPTZ,

    -- Deduplication
    content_hash    TEXT,                   -- hash of headline+source to prevent duplicates
    is_duplicate    BOOLEAN NOT NULL DEFAULT FALSE,

    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_signals_company ON mimir.signals (company_id, discovered_at DESC);
CREATE INDEX idx_signals_type ON mimir.signals (signal_type, discovered_at DESC);
CREATE INDEX idx_signals_score ON mimir.signals (relevance_score DESC)
    WHERE relevance_score >= 7;             -- partial index for high-value signals only
CREATE INDEX idx_signals_hash ON mimir.signals (content_hash)
    WHERE content_hash IS NOT NULL;


-- IPO readiness scores — tracked over time per company
-- The scheduler updates this after each feed scan
-- Historical series enables trend detection
CREATE TABLE mimir.ipo_scores (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    company_id      UUID NOT NULL REFERENCES mimir.companies (id),
    score           SMALLINT NOT NULL CHECK (score BETWEEN 0 AND 100),
    strength        mimir.ipo_signal_strength NOT NULL,
    rationale       TEXT,                   -- AI-generated explanation
    signals_counted INTEGER NOT NULL DEFAULT 0,
    model_used      TEXT,
    scored_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    job_run_id      UUID REFERENCES platform.job_runs (id)
);

CREATE INDEX idx_ipo_scores_company ON mimir.ipo_scores (company_id, scored_at DESC);


-- IPO signal indicators — the specific pre-listing events detected
-- These are the discrete signals that feed into the ipo_score
CREATE TABLE mimir.ipo_indicators (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    company_id      UUID NOT NULL REFERENCES mimir.companies (id),
    signal_id       UUID REFERENCES mimir.signals (id),
    indicator_type  TEXT NOT NULL,
    -- examples:
    --   'prospectus_filing'     -- regulatory filing detected
    --   'ir_hire'               -- investor relations role posted
    --   'conference_surge'      -- spike in speaking engagements
    --   'patent_consolidation'  -- IP portfolio activity
    --   'exec_comp_restructure' -- compensation changes typical pre-IPO
    --   'underwriter_mandate'   -- investment bank appointment
    detected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confidence      SMALLINT CHECK (confidence BETWEEN 1 AND 10),
    notes           TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_ipo_indicators_company ON mimir.ipo_indicators (company_id, detected_at DESC);


-- Digests — the assembled daily/weekly reports
-- Stored so they can be resent, reviewed, or audited
CREATE TABLE mimir.digests (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    client_id       UUID NOT NULL REFERENCES platform.clients (id),
    watchlist_id    UUID NOT NULL REFERENCES mimir.watchlists (id),
    digest_type     TEXT NOT NULL CHECK (digest_type IN ('daily', 'weekly', 'alert')),
    period_start    TIMESTAMPTZ NOT NULL,
    period_end      TIMESTAMPTZ NOT NULL,
    content         TEXT NOT NULL,          -- full rendered text of the digest
    signal_count    INTEGER NOT NULL DEFAULT 0,
    company_count   INTEGER NOT NULL DEFAULT 0,
    model_used      TEXT,
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delivered_at    TIMESTAMPTZ,
    job_run_id      UUID REFERENCES platform.job_runs (id)
);

CREATE INDEX idx_digests_client ON mimir.digests (client_id, generated_at DESC);


-- =============================================================
-- LEX SCHEMA — regulatory intelligence
-- =============================================================

-- Regulatory items — the raw feed
-- "Start simple" means one table with a relevance score.
-- Per-firm profile matching is added later as lex.firm_profiles
-- and lex.item_firm_scores without touching this table.
CREATE TABLE lex.regulatory_items (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    regulator       lex.regulator NOT NULL,
    item_type       lex.item_type NOT NULL,
    title           TEXT NOT NULL,
    summary         TEXT,                   -- AI-generated
    source_url      TEXT,
    reference_number TEXT,                  -- e.g. 'PS24/1', 'CP23/20'
    published_at    TIMESTAMPTZ,
    effective_date  DATE,                   -- when it comes into force
    consultation_closes DATE,              -- if it's a consultation paper
    discovered_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- AI scoring (general relevance, not per-firm yet)
    relevance_score SMALLINT CHECK (relevance_score BETWEEN 1 AND 10),
    impact_areas    TEXT[],
    -- examples: ['consumer_duty', 'ai_governance', 'data_protection',
    --            'cryptography', 'aml', 'mifid']
    action_required BOOLEAN,               -- AI assessment: does this require a response?
    urgency         TEXT CHECK (urgency IN ('low', 'medium', 'high', 'critical')),
    ai_commentary   TEXT,                  -- AI one-paragraph impact assessment
    model_used      TEXT,
    scored_at       TIMESTAMPTZ,

    -- Deduplication
    content_hash    TEXT,
    is_duplicate    BOOLEAN NOT NULL DEFAULT FALSE,

    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_lex_items_regulator ON lex.regulatory_items (regulator, published_at DESC);
CREATE INDEX idx_lex_items_urgency ON lex.regulatory_items (urgency, discovered_at DESC)
    WHERE urgency IN ('high', 'critical');
CREATE INDEX idx_lex_items_hash ON lex.regulatory_items (content_hash)
    WHERE content_hash IS NOT NULL;
CREATE INDEX idx_lex_items_areas ON lex.regulatory_items USING GIN (impact_areas);


-- Lex digests — assembled regulatory briefings per client
-- Mirrors mimir.digests in structure for consistency
CREATE TABLE lex.digests (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    client_id       UUID NOT NULL REFERENCES platform.clients (id),
    digest_type     TEXT NOT NULL CHECK (digest_type IN ('daily', 'weekly', 'alert')),
    period_start    TIMESTAMPTZ NOT NULL,
    period_end      TIMESTAMPTZ NOT NULL,
    content         TEXT NOT NULL,
    item_count      INTEGER NOT NULL DEFAULT 0,
    high_urgency_count INTEGER NOT NULL DEFAULT 0,
    model_used      TEXT,
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delivered_at    TIMESTAMPTZ,
    job_run_id      UUID REFERENCES platform.job_runs (id)
);

CREATE INDEX idx_lex_digests_client ON lex.digests (client_id, generated_at DESC);


-- =============================================================
-- UTILITY — updated_at trigger
-- Applied to all tables with an updated_at column
-- =============================================================

CREATE OR REPLACE FUNCTION platform.set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER set_updated_at BEFORE UPDATE ON platform.clients
    FOR EACH ROW EXECUTE FUNCTION platform.set_updated_at();

CREATE TRIGGER set_updated_at BEFORE UPDATE ON mimir.companies
    FOR EACH ROW EXECUTE FUNCTION platform.set_updated_at();

CREATE TRIGGER set_updated_at BEFORE UPDATE ON mimir.people
    FOR EACH ROW EXECUTE FUNCTION platform.set_updated_at();

CREATE TRIGGER set_updated_at BEFORE UPDATE ON mimir.watchlists
    FOR EACH ROW EXECUTE FUNCTION platform.set_updated_at();


-- =============================================================
-- SEED DATA — initial watchlist and PQC companies
-- =============================================================

-- The global PQC watchlist
INSERT INTO mimir.watchlists (id, name, description, is_global)
VALUES (
    uuid_generate_v4(),
    'PQC Core Watchlist',
    'Post-quantum cryptography companies with near-term IPO or acquisition potential',
    TRUE
);

-- Seed companies (the under-the-radar PQC watchlist from the research)
-- These can be fleshed out further but the slugs are the canonical identifiers
INSERT INTO mimir.companies (name, slug, sector, country, stage, description) VALUES
    ('PQShield',        'pqshield',         'post-quantum cryptography', 'GB', 'private_growth',
     'Oxford spinout. Silicon IP layer — ML-KEM and ML-DSA on hardware. 30 patent families. NCSC Pilot provider.'),
    ('ISARA Corporation','isara',            'post-quantum cryptography', 'CA', 'private_growth',
     'BlackBerry spinout. Crypto-agility toolkits and PKI migration. Carahsoft federal distribution since Jan 2026.'),
    ('Crypto4A',        'crypto4a',         'post-quantum cryptography', 'CA', 'private_early',
     'Quantum-safe HSMs (QxHSM). Nokia Blueprint 7 trial partner. Canadian sovereignty angle.'),
    ('evolutionQ',      'evolutionq',       'post-quantum cryptography', 'CA', 'private_growth',
     'Basejump platform. Bank of Canada contract. Michele Mosca co-founder. GRI quantum threat timeline.'),
    ('Crypto Quantique', 'crypto-quantique', 'post-quantum cryptography', 'GB', 'private_early',
     'Lightweight PQC for IoT and edge devices. UK-based.'),
    ('Xiphera',         'xiphera',          'post-quantum cryptography', 'FI', 'private_early',
     'PQC IP cores for FPGA and ASIC. Finnish.'),
    ('SEALSQ',          'sealsq',           'post-quantum cryptography', 'CH', 'public',
     'NASDAQ: LAES. Quantum-safe semiconductors and hardware roots-of-trust. WISeKey subsidiary.'),
    ('Quantinuum',      'quantinuum',       'quantum computing',         'GB', 'private_growth',
     'Trapped-ion quantum computing. Honeywell/Cambridge Quantum merger. IPO rumoured.');

-- Add all seed companies to the global watchlist
INSERT INTO mimir.watchlist_companies (watchlist_id, company_id, added_reason)
SELECT
    w.id,
    c.id,
    'Initial PQC thesis — identified in founding research'
FROM mimir.watchlists w
CROSS JOIN mimir.companies c
WHERE w.is_global = TRUE;


-- =============================================================
-- END OF SCHEMA
-- =============================================================