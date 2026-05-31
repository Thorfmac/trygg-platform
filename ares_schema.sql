-- =============================================================================
-- Trygg Ares Schema — Cycle One
-- =============================================================================
-- Adds the `ares.*` namespace alongside existing `platform.*`, `mimir.*`, `lex.*`.
-- Source-authority discipline baked in from day one per Source-Authority Spec v1.
-- Seeded with the v5 universe doc entities and catalyst calendar.
--
-- Apply order: this is ADDITIVE. Run against the existing Trygg Postgres database.
-- Does not modify platform.*, mimir.*, or lex.* schemas.
-- =============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS ares;

-- -----------------------------------------------------------------------------
-- ares.entities — companies, tickers, ETFs that Ares tracks
-- -----------------------------------------------------------------------------
CREATE TABLE ares.entities (
    id              SERIAL PRIMARY KEY,
    ticker          TEXT UNIQUE NOT NULL,
    exchange        TEXT,                                -- 'NASDAQ', 'NYSE', 'LSE', 'TSX', 'OTC'
    name            TEXT NOT NULL,
    country         TEXT,
    sector          TEXT NOT NULL,                       -- 'space', 'quantum'
    sub_sector      TEXT,                                -- 'launch', 'satellite', 'lunar', 'pqc', 'quantum_compute', 'broadband'
    status          TEXT NOT NULL,                       -- 'public', 'private', 'pre_ipo', 'recently_listed'
    is_etf          BOOLEAN DEFAULT FALSE,
    
    -- Universe doc classification
    universe_tier   INT,                                 -- 1=conviction, 2=CSP, 3=conditional, 4=catalyst-trade, 5=watchlist, 6=off-universe
    universe_status TEXT,                                -- 'active', 'deferred', 'watchlist', 'off_universe'
    
    -- Source-authority metadata
    primary_filing_url      TEXT,                        -- SEC EDGAR or equivalent
    last_filing_check_at    TIMESTAMPTZ,
    
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_ares_entities_sector ON ares.entities(sector);
CREATE INDEX idx_ares_entities_universe_tier ON ares.entities(universe_tier);

-- -----------------------------------------------------------------------------
-- ares.theses — current thesis on each entity, versioned against universe doc
-- -----------------------------------------------------------------------------
CREATE TABLE ares.theses (
    id                      SERIAL PRIMARY KEY,
    entity_id               INT NOT NULL REFERENCES ares.entities(id) ON DELETE CASCADE,
    universe_doc_version    TEXT NOT NULL,               -- 'v5', 'v6', etc.
    
    thesis                  TEXT NOT NULL,               -- The thesis prose
    invalidator             TEXT NOT NULL,               -- Exit condition regardless of price
    
    -- Sizing & instrument
    initial_size_pct        NUMERIC,                     -- % of total capital
    max_size_pct            NUMERIC,
    stop_price              NUMERIC,
    trim_price              NUMERIC,
    instrument              TEXT,                        -- 'equity', 'csp', 'long_put', 'put_spread', 'call', 'etf'
    structure_notes         TEXT,
    
    -- Lifecycle
    is_active               BOOLEAN DEFAULT TRUE,
    superseded_by           INT REFERENCES ares.theses(id),
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    superseded_at           TIMESTAMPTZ
);

CREATE INDEX idx_ares_theses_entity_active ON ares.theses(entity_id, is_active);

-- -----------------------------------------------------------------------------
-- ares.catalysts — defined events with dates that move positions
-- -----------------------------------------------------------------------------
CREATE TABLE ares.catalysts (
    id                          SERIAL PRIMARY KEY,
    entity_id                   INT REFERENCES ares.entities(id) ON DELETE CASCADE,
    
    -- Event timing
    catalyst_date               DATE,                    -- Specific date if known
    catalyst_window_start       DATE,                    -- Window if range
    catalyst_window_end         DATE,
    
    -- Event detail
    description                 TEXT NOT NULL,
    catalyst_type               TEXT NOT NULL,           -- 'earnings', 'ipo', 'lockup', 'product', 'regulatory', 'launch', 'deal_close', 'rate_decision'
    affected_entity_ids         INT[],                   -- Multi-name catalysts (e.g. SpaceX IPO affects ASTS, SATS, sector)
    
    -- Planned action
    pre_action                  TEXT,                    -- Action to take before catalyst
    post_action                 TEXT,                    -- Action to take after catalyst
    
    -- Source-authority
    is_confirmed                BOOLEAN DEFAULT FALSE,
    confirmation_source_url     TEXT,
    confirmation_source_tier    INT,                     -- 1-4 per source-authority spec
    confirmation_checked_at     TIMESTAMPTZ,
    
    created_at                  TIMESTAMPTZ DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_ares_catalysts_date ON ares.catalysts(catalyst_date);
CREATE INDEX idx_ares_catalysts_entity ON ares.catalysts(entity_id);

-- -----------------------------------------------------------------------------
-- ares.signals — scored intelligence with source-authority discipline
-- -----------------------------------------------------------------------------
CREATE TABLE ares.signals (
    id              SERIAL PRIMARY KEY,
    entity_id       INT NOT NULL REFERENCES ares.entities(id) ON DELETE CASCADE,
    
    -- Content
    headline        TEXT NOT NULL,
    url             TEXT,
    source          TEXT NOT NULL,                       -- 'sec.gov', 'reuters.com', 'bloomberg.com', etc.
    body            TEXT,
    body_hash       TEXT UNIQUE NOT NULL,                -- SHA-256 of normalised body for dedup
    
    -- Source-authority (required from day one)
    source_tier             INT NOT NULL,                -- 1=primary regulatory, 2=vetted aggregator, 3=mainstream news, 4=social/speculation
    authority_tier          INT,                         -- Final tier after corroboration logic
    is_recency_critical     BOOLEAN DEFAULT FALSE,       -- Adverse-event flag — triggers recency check
    
    -- Timing
    published_at    TIMESTAMPTZ,
    fetched_at      TIMESTAMPTZ DEFAULT NOW(),
    
    -- Triage scoring (Claude Haiku output)
    relevance_score     NUMERIC,                         -- 1-10
    sentiment           TEXT,                            -- 'positive', 'negative', 'neutral', 'mixed'
    implication         TEXT,                            -- One-line investment implication
    triage_notes        TEXT,
    
    -- Corroboration (per source-authority spec)
    corroboration_signal_ids    INT[],                   -- Other signals supporting this claim
    contradicts_signal_ids      INT[],                   -- Other signals contradicting
    
    -- Provenance
    triage_model    TEXT,                                -- 'claude-haiku-4-5-20251001'
    triage_run_id   INT,                                 -- Reference to platform.job_runs.id (if it exists)
    
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_ares_signals_entity ON ares.signals(entity_id);
CREATE INDEX idx_ares_signals_source_tier ON ares.signals(source_tier);
CREATE INDEX idx_ares_signals_relevance ON ares.signals(relevance_score DESC) WHERE relevance_score IS NOT NULL;
CREATE INDEX idx_ares_signals_published ON ares.signals(published_at DESC);
CREATE INDEX idx_ares_signals_recency_critical ON ares.signals(is_recency_critical) WHERE is_recency_critical = TRUE;

-- -----------------------------------------------------------------------------
-- ares.digests — stored briefings with inline-provenance compliance tracking
-- -----------------------------------------------------------------------------
CREATE TABLE ares.digests (
    id                      SERIAL PRIMARY KEY,
    digest_date             DATE NOT NULL,
    digest_type             TEXT NOT NULL,                -- 'daily', 'event_driven', 'weekly_review'
    
    content                 TEXT NOT NULL,                -- Full markdown body
    signal_ids_included     INT[],                        -- Which signals were synthesised
    signal_count            INT,
    relevance_threshold     NUMERIC,                      -- Score threshold used (e.g. >= 5)
    
    -- Source-authority compliance
    has_inline_provenance   BOOLEAN DEFAULT TRUE,         -- Every numeric claim cites source
    tier_breakdown          JSONB,                        -- e.g. {"tier_1": 3, "tier_2": 12, "tier_3": 2, "tier_4": 0}
    tier_4_excluded         BOOLEAN DEFAULT TRUE,         -- Tier 4 sources excluded per spec
    
    -- Generation provenance
    digest_model            TEXT,                         -- 'claude-sonnet-4-6'
    generation_run_id       INT,                          -- Reference to platform.job_runs.id
    
    sent_to_clients         INT[],                        -- Client IDs notified (references platform.clients)
    sent_at                 TIMESTAMPTZ,
    
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_ares_digests_date ON ares.digests(digest_date DESC);

-- -----------------------------------------------------------------------------
-- ares.ledger — every decision logged with full context for skill-vs-beta audit
-- -----------------------------------------------------------------------------
-- This is the decision/outcome ledger that the universe doc treats as mandatory
-- before first trade. Per v5 pre-deployment checklist.
-- -----------------------------------------------------------------------------
CREATE TABLE ares.ledger (
    id                          SERIAL PRIMARY KEY,
    decision_at                 TIMESTAMPTZ DEFAULT NOW(),
    entity_id                   INT REFERENCES ares.entities(id),
    thesis_id                   INT REFERENCES ares.theses(id),
    
    -- Decision type
    decision_type               TEXT NOT NULL,            -- 'entry', 'add', 'trim', 'exit', 'pass', 'roll', 'stop_triggered', 'thesis_review'
    
    -- Trade details (nullable for non-trade decisions like 'pass' or 'thesis_review')
    instrument                  TEXT,                     -- 'equity', 'csp', 'long_put', 'put_spread', 'call', 'etf'
    side                        TEXT,                     -- 'long', 'short_put', 'long_put', 'short_call'
    quantity                    NUMERIC,                  -- Shares or contracts
    price                       NUMERIC,                  -- Fill price (equity) or premium per contract (options)
    strike                      NUMERIC,                  -- Options only
    expiry                      DATE,                     -- Options only
    size_pct_at_entry           NUMERIC,                  -- Position size as % of total capital
    
    -- Decision context (these are the audit fields)
    entry_thesis                TEXT NOT NULL,            -- One-paragraph rationale at decision time
    catalyst_triggering         TEXT,                     -- Free-text description of what triggered this
    catalyst_id                 INT REFERENCES ares.catalysts(id),
    target_exit_condition       TEXT,                     -- What would make us exit
    stop_condition              TEXT,                     -- Hard stop level/condition
    expected_hold_window        TEXT,                     -- '6 months', '12-18 months', etc.
    
    -- Funder ratification (per cycle one governance)
    funder_ratified             BOOLEAN DEFAULT FALSE,
    funder_ratification_method  TEXT,                     -- 'slack', 'email', 'verbal', 'pre_approved_by_thesis'
    funder_ratified_at          TIMESTAMPTZ,
    
    -- Outcome (filled later — this is what enables skill-vs-beta judgement)
    outcome_recorded_at         TIMESTAMPTZ,
    realized_pnl                NUMERIC,                  -- Absolute P&L in account currency
    realized_pnl_pct            NUMERIC,                  -- % return on position
    outcome_notes               TEXT,                     -- Post-trade reflection
    invalidator_triggered       BOOLEAN,                  -- Did the stated invalidator fire?
    
    -- Account routing
    account                     TEXT NOT NULL,            -- 'US' or 'UK'
    broker_order_id             TEXT,
    
    notes                       TEXT
);

CREATE INDEX idx_ares_ledger_entity ON ares.ledger(entity_id);
CREATE INDEX idx_ares_ledger_decision_at ON ares.ledger(decision_at DESC);
CREATE INDEX idx_ares_ledger_decision_type ON ares.ledger(decision_type);
CREATE INDEX idx_ares_ledger_open ON ares.ledger(entity_id) WHERE outcome_recorded_at IS NULL AND decision_type IN ('entry', 'add');

-- -----------------------------------------------------------------------------
-- ares.position_snapshots — periodic snapshot of open positions for portfolio audit
-- -----------------------------------------------------------------------------
CREATE TABLE ares.position_snapshots (
    id                      SERIAL PRIMARY KEY,
    snapshot_at             TIMESTAMPTZ DEFAULT NOW(),
    
    -- Sleeve-level state
    total_capital           NUMERIC,
    cash_pct                NUMERIC,
    space_sleeve_pct        NUMERIC,
    quantum_sleeve_pct      NUMERIC,
    
    -- Correlation cap tracking
    spacex_correlated_pct   NUMERIC,                      -- Counts against 25% catalyst-correlation cap
    
    -- Drawdown state
    drawdown_pct_from_peak  NUMERIC,                      -- Trigger thresholds: -8%, -12%, -20%
    
    -- Benchmark tracking
    cycle_one_return_pct    NUMERIC,                      -- Cycle one performance
    ufo_return_pct          NUMERIC,                      -- Benchmark performance
    spread_vs_ufo_pct       NUMERIC,                      -- Cycle one minus UFO
    
    notes                   TEXT
);

CREATE INDEX idx_ares_snapshots_at ON ares.position_snapshots(snapshot_at DESC);

-- =============================================================================
-- Seed data — Universe Doc v5
-- =============================================================================

-- Tier 1: Conviction equity positions
INSERT INTO ares.entities (ticker, exchange, name, country, sector, sub_sector, status, is_etf, universe_tier, universe_status, primary_filing_url, notes) VALUES
('LUNR', 'NASDAQ', 'Intuitive Machines', 'US', 'space', 'lunar', 'public', FALSE, 1, 'active',
 'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001844452',
 'Anchor position. Forward P/S ~8.3x, positive Adj EBITDA, $1.1B backlog. Initial 5%, max 10%, stop $25.'),

('SATS', 'NASDAQ', 'EchoStar Corporation', 'US', 'space', 'broadband', 'public', FALSE, 1, 'active',
 'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001415404',
 'Closing-risk-bridge with SpaceX beta. 3% starter, scale to 5% on AT&T close, max 6% only if SPCX high end. Stop $95. Going-concern flag.'),

('UFO', 'NYSE', 'Procure Space ETF', 'US', 'space', NULL, 'public', TRUE, 1, 'active',
 NULL,
 'Sleeve beta + benchmark. 3% of capital. No stop; monthly review.'),

('QTUM', 'NYSE', 'Defiance Quantum ETF', 'US', 'quantum', NULL, 'public', TRUE, 1, 'active',
 NULL,
 'Quantum sleeve beta. 2% of capital. Build over June ahead of QNT listing.');

-- Tier 2: CSP entries
INSERT INTO ares.entities (ticker, exchange, name, country, sector, sub_sector, status, is_etf, universe_tier, universe_status, primary_filing_url, notes) VALUES
('IONQ', 'NYSE', 'IonQ', 'US', 'quantum', 'quantum_compute', 'public', FALSE, 2, 'active',
 'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001824920',
 'Fair-value CSP entry. Puts at $55-60, 1.5-2% collateral. Max 4% on assignment.');

-- Tier 3: Conditional entries
INSERT INTO ares.entities (ticker, exchange, name, country, sector, sub_sector, status, is_etf, universe_tier, universe_status, primary_filing_url, notes) VALUES
('RKLB', 'NASDAQ', 'Rocket Lab', 'US', 'space', 'launch', 'public', FALSE, 3, 'deferred',
 'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001819994',
 'Puts at $100-110 only on pullback to $115-120 or Q2 disappointment. 1.5% collateral.'),

('QNT', 'NASDAQ', 'Quantinuum', 'US', 'quantum', 'quantum_compute', 'pre_ipo', FALSE, 3, 'deferred',
 NULL,
 'Post-IPO CSPs only if listing 30%+ above IPO price. 1.5% collateral. Lockup expiry puts Nov-Dec 2026.');

-- Tier 4: Defined-risk catalyst trade
INSERT INTO ares.entities (ticker, exchange, name, country, sector, sub_sector, status, is_etf, universe_tier, universe_status, primary_filing_url, notes) VALUES
('SPCX', 'NYSE', 'SpaceX', 'US', 'space', 'launch', 'pre_ipo', FALSE, 4, 'deferred',
 NULL,
 'Lockup expiry long puts. Enter late Oct/Nov 2026. ~10-15% OTM. 30-45d expiry covering 15-27 Dec. 1% premium-at-risk.');

-- Tier 5: Watchlist
INSERT INTO ares.entities (ticker, exchange, name, country, sector, sub_sector, status, is_etf, universe_tier, universe_status, notes) VALUES
('ASTS', 'NASDAQ', 'AST SpaceMobile', 'US', 'space', 'satellite', 'public', FALSE, 5, 'watchlist',
 'Re-entry trigger: pullback to <=$90 AND confirmed BlueBird 8/9/10 launch. CSPs at $75-80 then. Max 4%.'),

('RDW', 'NYSE', 'Redwire', 'US', 'space', 'satellite', 'public', FALSE, 5, 'watchlist',
 'Re-entry: pullback to $15-18 OR two clean EBITDA-positive quarters. CSPs at $13-15. Max 3%.'),

('FLY', 'NASDAQ', 'Firefly Aerospace', 'US', 'space', 'launch', 'recently_listed', FALSE, 5, 'watchlist',
 'Re-entry: pullback to $30-35 (200-day MA). Equity starter or puts at $30. Max 2%.'),

('SATL', 'NASDAQ', 'Satellogic', 'US', 'space', 'satellite', 'public', FALSE, 5, 'watchlist',
 'Needs thesis hour. Earth observation. Max TBD.'),

('BKSY', 'NYSE', 'BlackSky Technology', 'US', 'space', 'satellite', 'public', FALSE, 5, 'watchlist',
 'Needs thesis hour. Earth observation + defence. Max TBD.'),

('MDA.TO', 'TSX', 'MDA Space', 'CA', 'space', 'satellite', 'public', FALSE, 5, 'watchlist',
 'Needs thesis hour. Canadian-listed; consider UK account workflow.'),

('FTC.L', 'LSE', 'Filtronic', 'GB', 'space', 'satellite', 'public', FALSE, 5, 'watchlist',
 'Needs thesis hour. UK-listed; natural UK account candidate. Max 1.5%.');

-- Tier 6: Off-universe (documented for the record, not actively monitored)
INSERT INTO ares.entities (ticker, exchange, name, country, sector, sub_sector, status, is_etf, universe_tier, universe_status, notes) VALUES
('IRDM', 'NASDAQ', 'Iridium Communications', 'US', 'space', 'broadband', 'public', FALSE, 6, 'off_universe',
 'Short thesis correct over 3-5y but offsetting catalysts make it poor cycle-one expression.'),

('VSAT', 'NASDAQ', 'Viasat', 'US', 'space', 'broadband', 'public', FALSE, 6, 'off_universe',
 'Active defence contract wins prevent clean short.'),

('DXYZ', 'NYSE', 'Destiny Tech100', 'US', 'space', NULL, 'public', TRUE, 6, 'off_universe',
 'Premium-to-NAV trade only; skip.'),

('HON', 'NASDAQ', 'Honeywell', 'US', 'quantum', 'quantum_compute', 'public', FALSE, 6, 'off_universe',
 'Not a usable Quantinuum proxy — too large.');

-- =============================================================================
-- Seed catalyst calendar — Universe Doc v5
-- =============================================================================

INSERT INTO ares.catalysts (entity_id, catalyst_date, catalyst_window_start, catalyst_window_end, description, catalyst_type, pre_action, post_action, is_confirmed, confirmation_source_tier)
SELECT id, NULL, '2026-06-08', '2026-06-14', 'SpaceX IPO roadshow begins; IV will spike across complex', 'ipo', 'Watch only; do not chase IPO names', 'Watch SPCX opening print, intraday range, IV settlement', FALSE, 2
FROM ares.entities WHERE ticker = 'SPCX';

INSERT INTO ares.catalysts (entity_id, catalyst_date, description, catalyst_type, pre_action, post_action, is_confirmed, confirmation_source_tier)
SELECT id, '2026-06-12', 'SpaceX IPO listing day; SATS marks to market; no direct IPO positions', 'ipo', 'No positions added on IPO names', 'Mark SATS; document baseline IPO observations', FALSE, 2
FROM ares.entities WHERE ticker = 'SPCX';

INSERT INTO ares.catalysts (entity_id, catalyst_date, catalyst_window_start, catalyst_window_end, description, catalyst_type, pre_action, post_action, is_confirmed, confirmation_source_tier)
SELECT id, NULL, '2026-06-01', '2026-06-07', 'Quantinuum IPO listing (QNT) first week of June', 'ipo', 'QTUM ETF position already built', 'Watch listing day; day-30 close determines CSP eligibility', FALSE, 2
FROM ares.entities WHERE ticker = 'QNT';

INSERT INTO ares.catalysts (entity_id, catalyst_date, catalyst_window_start, catalyst_window_end, description, catalyst_type, pre_action, post_action, is_confirmed, confirmation_source_tier)
SELECT id, NULL, '2026-06-15', '2026-06-30', 'ASTS BlueBird 8/9/10 Falcon 9 launch', 'launch', NULL, 'Watch for re-entry trigger if pullback materialises', FALSE, 2
FROM ares.entities WHERE ticker = 'ASTS';

INSERT INTO ares.catalysts (entity_id, catalyst_date, catalyst_window_start, catalyst_window_end, description, catalyst_type, pre_action, post_action, is_confirmed, confirmation_source_tier)
SELECT id, NULL, '2026-06-15', '2026-06-30', 'LUNR Goonhilly Earth Station acquisition close (mid-2026)', 'deal_close', 'Held position', 'Watch for integration commentary in Q2 earnings', FALSE, 2
FROM ares.entities WHERE ticker = 'LUNR';

INSERT INTO ares.catalysts (entity_id, catalyst_date, catalyst_window_start, catalyst_window_end, description, catalyst_type, pre_action, post_action, is_confirmed, confirmation_source_tier)
SELECT id, NULL, '2026-06-01', '2026-06-30', 'SATS AT&T transaction close (H1 2026) — going-concern flag lifts', 'deal_close', 'Hold at 3% starter', 'Scale 3%->5% on confirmation. NO BREAK-UP FEE if blocked.', FALSE, 1
FROM ares.entities WHERE ticker = 'SATS';

INSERT INTO ares.catalysts (entity_id, catalyst_date, catalyst_window_start, catalyst_window_end, description, catalyst_type, pre_action, post_action, is_confirmed, confirmation_source_tier)
SELECT id, NULL, '2026-08-01', '2026-08-15', 'LUNR Q2 2026 earnings — first organic-growth test post-Lanteris', 'earnings', NULL, 'Scale to upper end of size range on confirmed organic growth', FALSE, 2
FROM ares.entities WHERE ticker = 'LUNR';

INSERT INTO ares.catalysts (entity_id, catalyst_date, description, catalyst_type, pre_action, post_action, is_confirmed, confirmation_source_tier)
SELECT id, '2026-08-12', 'IONQ Q2 2026 earnings', 'earnings', 'Held puts in place', 'Manage roll vs assignment based on price action', TRUE, 2
FROM ares.entities WHERE ticker = 'IONQ';

INSERT INTO ares.catalysts (entity_id, catalyst_date, catalyst_window_start, catalyst_window_end, description, catalyst_type, pre_action, post_action, is_confirmed, confirmation_source_tier)
SELECT id, NULL, '2026-08-01', '2026-08-15', 'RKLB Q2 2026 earnings', 'earnings', 'Watch for pullback trigger', 'If pullback to $115-120, sell CSPs at $100-110', FALSE, 2
FROM ares.entities WHERE ticker = 'RKLB';

INSERT INTO ares.catalysts (entity_id, catalyst_date, catalyst_window_start, catalyst_window_end, description, catalyst_type, pre_action, post_action, is_confirmed, confirmation_source_tier)
SELECT id, NULL, '2026-09-01', '2026-10-31', 'Neutron readiness updates from RKLB', 'product', NULL, 'Watch for entry condition (confirmed launch within 90 days)', FALSE, 2
FROM ares.entities WHERE ticker = 'RKLB';

INSERT INTO ares.catalysts (entity_id, catalyst_date, catalyst_window_start, catalyst_window_end, description, catalyst_type, pre_action, post_action, is_confirmed, confirmation_source_tier)
SELECT NULL, NULL, '2026-10-01', '2026-12-31', 'NIST PQC standard finalisation expected late 2026', 'regulatory', 'Held quantum positions benefit', 'Watch for sector-wide repricing', FALSE, 1;

INSERT INTO ares.catalysts (entity_id, catalyst_date, catalyst_window_start, catalyst_window_end, description, catalyst_type, pre_action, post_action, is_confirmed, confirmation_source_tier)
SELECT id, NULL, '2026-10-01', '2026-12-31', 'Neutron first launch (NET Q4 2026)', 'launch', NULL, 'Held puts or equity if triggered earlier', FALSE, 2
FROM ares.entities WHERE ticker = 'RKLB';

INSERT INTO ares.catalysts (entity_id, catalyst_date, catalyst_window_start, catalyst_window_end, description, catalyst_type, pre_action, post_action, is_confirmed, confirmation_source_tier)
SELECT id, NULL, '2026-11-01', '2026-11-15', 'LUNR Q3 2026 earnings', 'earnings', 'Held position', 'Reassess sizing toward upper range if trajectory intact', FALSE, 2
FROM ares.entities WHERE ticker = 'LUNR';

INSERT INTO ares.catalysts (entity_id, catalyst_date, catalyst_window_start, catalyst_window_end, description, catalyst_type, pre_action, post_action, is_confirmed, confirmation_source_tier)
SELECT id, NULL, '2026-11-01', '2026-11-15', 'RKLB Q3 2026 earnings', 'earnings', NULL, 'Catalyst event for any held position', FALSE, 2
FROM ares.entities WHERE ticker = 'RKLB';

INSERT INTO ares.catalysts (entity_id, catalyst_date, catalyst_window_start, catalyst_window_end, description, catalyst_type, pre_action, post_action, is_confirmed, confirmation_source_tier)
SELECT id, NULL, '2026-10-25', '2026-11-15', 'Enter SPCX lockup expiry puts — ACTIVE TRADE EXECUTION', 'lockup', 'Watch SPCX behaviour Oct; enter Oct/early Nov', '10-15% OTM, 30-45d expiry, 1% premium-at-risk', FALSE, 2
FROM ares.entities WHERE ticker = 'SPCX';

INSERT INTO ares.catalysts (entity_id, catalyst_date, catalyst_window_start, catalyst_window_end, description, catalyst_type, pre_action, post_action, is_confirmed, confirmation_source_tier)
SELECT id, NULL, '2026-12-15', '2026-12-27', 'SPCX 180-day lockup expiry — primary cycle one P&L event', 'lockup', 'Held puts in place from Oct/Nov', 'Manage exit: take profits at 100% gain or hold to 5d before expiry', FALSE, 2
FROM ares.entities WHERE ticker = 'SPCX';

INSERT INTO ares.catalysts (entity_id, catalyst_date, catalyst_window_start, catalyst_window_end, description, catalyst_type, pre_action, post_action, is_confirmed, confirmation_source_tier)
SELECT id, NULL, '2026-11-01', '2026-12-31', 'QNT lockup expiry (estimated ~6 months post-IPO)', 'lockup', 'Pre-position via puts 4-6 weeks before', '1% capital, 30-45d expiry', FALSE, 2
FROM ares.entities WHERE ticker = 'QNT';

INSERT INTO ares.catalysts (entity_id, catalyst_date, description, catalyst_type, pre_action, post_action, is_confirmed, confirmation_source_tier)
SELECT id, '2027-11-30', 'SATS SpaceX Spectrum Acquisition Closing — equity receipt date (beyond cycle one window)', 'deal_close', 'Beyond cycle one window', 'Informs hold/sell decision for SATS post-cycle one', TRUE, 1
FROM ares.entities WHERE ticker = 'SATS';

-- =============================================================================
-- Convenience views for ongoing operations
-- =============================================================================

-- All active theses with their entity context
CREATE OR REPLACE VIEW ares.active_theses AS
SELECT
    t.id AS thesis_id,
    e.ticker,
    e.name,
    e.sector,
    e.universe_tier,
    e.universe_status,
    t.universe_doc_version,
    t.initial_size_pct,
    t.max_size_pct,
    t.stop_price,
    t.instrument,
    t.thesis,
    t.invalidator,
    t.created_at
FROM ares.theses t
JOIN ares.entities e ON e.id = t.entity_id
WHERE t.is_active = TRUE;

-- Upcoming catalysts in the next 90 days
CREATE OR REPLACE VIEW ares.upcoming_catalysts AS
SELECT
    c.id AS catalyst_id,
    e.ticker,
    e.name,
    c.catalyst_date,
    c.catalyst_window_start,
    c.catalyst_window_end,
    c.description,
    c.catalyst_type,
    c.pre_action,
    c.is_confirmed,
    c.confirmation_source_tier
FROM ares.catalysts c
LEFT JOIN ares.entities e ON e.id = c.entity_id
WHERE
    COALESCE(c.catalyst_date, c.catalyst_window_end) >= CURRENT_DATE
    AND COALESCE(c.catalyst_date, c.catalyst_window_start) <= CURRENT_DATE + INTERVAL '90 days'
ORDER BY COALESCE(c.catalyst_date, c.catalyst_window_start);

-- Open ledger positions (entry without exit recorded)
CREATE OR REPLACE VIEW ares.open_positions AS
SELECT
    l.id AS ledger_id,
    e.ticker,
    l.decision_at,
    l.instrument,
    l.side,
    l.quantity,
    l.price,
    l.strike,
    l.expiry,
    l.size_pct_at_entry,
    l.entry_thesis,
    l.target_exit_condition,
    l.stop_condition,
    l.account
FROM ares.ledger l
JOIN ares.entities e ON e.id = l.entity_id
WHERE
    l.outcome_recorded_at IS NULL
    AND l.decision_type IN ('entry', 'add')
ORDER BY l.decision_at DESC;

-- =============================================================================
-- Helper functions
-- =============================================================================

-- Compute current sleeve allocation from open positions
CREATE OR REPLACE FUNCTION ares.current_sleeve_pct(p_sector TEXT)
RETURNS NUMERIC AS $$
    SELECT COALESCE(SUM(l.size_pct_at_entry), 0)
    FROM ares.ledger l
    JOIN ares.entities e ON e.id = l.entity_id
    WHERE
        l.outcome_recorded_at IS NULL
        AND l.decision_type IN ('entry', 'add')
        AND e.sector = p_sector;
$$ LANGUAGE SQL STABLE;

-- Compute SpaceX-correlated exposure for catalyst-correlation cap
CREATE OR REPLACE FUNCTION ares.spacex_correlated_pct()
RETURNS NUMERIC AS $$
    SELECT COALESCE(SUM(l.size_pct_at_entry), 0)
    FROM ares.ledger l
    JOIN ares.entities e ON e.id = l.entity_id
    WHERE
        l.outcome_recorded_at IS NULL
        AND l.decision_type IN ('entry', 'add')
        AND e.ticker IN ('ASTS', 'RKLB', 'SATS', 'SPCX', 'DXYZ', 'UFO');
$$ LANGUAGE SQL STABLE;

COMMIT;

-- =============================================================================
-- Post-deployment verification queries
-- =============================================================================
-- Run these after applying the migration to confirm seed data is correct:
--
-- SELECT ticker, name, universe_tier, universe_status FROM ares.entities ORDER BY universe_tier, ticker;
-- SELECT * FROM ares.upcoming_catalysts;
-- SELECT ares.current_sleeve_pct('space');
-- SELECT ares.spacex_correlated_pct();
-- =============================================================================
