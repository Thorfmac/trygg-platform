-- ============================================================
-- Trygg Ares — Database Schema
-- Apply with:
-- docker exec -i trygg-db psql -U trygg -d trygg < ares_schema.sql
-- ============================================================

CREATE SCHEMA IF NOT EXISTS ares AUTHORIZATION trygg;

-- ------------------------------------------------------------
-- Sector taxonomy
-- ------------------------------------------------------------
CREATE TABLE ares.sectors (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    order_level     INTEGER NOT NULL CHECK (order_level IN (1, 2, 3)),
    thesis_note     TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ------------------------------------------------------------
-- Equity watchlist
-- ------------------------------------------------------------
CREATE TABLE ares.equities (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    ticker          TEXT,
    exchange        TEXT,
    sector_id       INTEGER REFERENCES ares.sectors(id),
    is_public       BOOLEAN NOT NULL DEFAULT TRUE,
    country         TEXT,
    thesis_note     TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    added_at        TIMESTAMPTZ DEFAULT NOW()
);

-- ------------------------------------------------------------
-- Daily financial snapshots (from FMP)
-- ------------------------------------------------------------
CREATE TABLE ares.equity_snapshots (
    id               SERIAL PRIMARY KEY,
    equity_id        INTEGER REFERENCES ares.equities(id),
    snapshot_date    DATE NOT NULL,
    price            NUMERIC(12,4),
    market_cap       BIGINT,
    ps_ratio         NUMERIC(8,2),
    pe_ratio         NUMERIC(8,2),
    sector_ps_median NUMERIC(8,2),
    revenue_ttm      BIGINT,
    yoy_growth_pct   NUMERIC(6,2),
    raw_data         JSONB,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (equity_id, snapshot_date)
);

-- ------------------------------------------------------------
-- News signals
-- ------------------------------------------------------------
CREATE TABLE ares.signals (
    id               SERIAL PRIMARY KEY,
    equity_id        INTEGER REFERENCES ares.equities(id),
    title            TEXT NOT NULL,
    url              TEXT NOT NULL,
    source           TEXT,
    published_at     TIMESTAMPTZ,
    content_hash     TEXT UNIQUE NOT NULL,
    relevance_score  INTEGER CHECK (relevance_score BETWEEN 1 AND 10),
    narrative_score  INTEGER CHECK (narrative_score BETWEEN 1 AND 10),
    sentiment        TEXT,
    valuation_flag   TEXT CHECK (valuation_flag IN (
                         'undervalued', 'fairly_valued', 'overvalued', 'insufficient_data'
                     )),
    implication      TEXT,
    raw_response     JSONB,
    triaged_at       TIMESTAMPTZ,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ------------------------------------------------------------
-- Dynamic candidate flags (agent-surfaced, not yet on watchlist)
-- ------------------------------------------------------------
CREATE TABLE ares.candidates (
    id               SERIAL PRIMARY KEY,
    name             TEXT NOT NULL,
    ticker           TEXT,
    mentioned_in     TEXT,
    mention_context  TEXT,
    confidence       INTEGER CHECK (confidence BETWEEN 1 AND 10),
    reviewed         BOOLEAN DEFAULT FALSE,
    added_to_watchlist BOOLEAN DEFAULT FALSE,
    flagged_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ------------------------------------------------------------
-- Opus 4.7 deep analysis runs
-- ------------------------------------------------------------
CREATE TABLE ares.thesis_analyses (
    id               SERIAL PRIMARY KEY,
    analysis_type    TEXT NOT NULL,
    trigger_event    TEXT,
    model            TEXT NOT NULL,
    content          TEXT NOT NULL,
    sectors_covered  TEXT[],
    equities_covered INTEGER[],
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ------------------------------------------------------------
-- Daily digests
-- ------------------------------------------------------------
CREATE TABLE ares.digests (
    id                  SERIAL PRIMARY KEY,
    digest_date         DATE NOT NULL,
    content             TEXT NOT NULL,
    signal_count        INTEGER,
    top_score           INTEGER,
    sent_via_telegram   BOOLEAN DEFAULT FALSE,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- Seed data
-- ============================================================

INSERT INTO ares.sectors (name, order_level, thesis_note) VALUES
('Launch Services',                1, 'Direct SpaceX competitors — valuation ceiling lifted by public benchmark'),
('Earth Observation',              1, 'SpaceX customer/partner ecosystem — institutional rerating'),
('Satellite Broadband / LEO',      1, 'LEO infrastructure — narrative gravity from SpaceX Starlink'),
('Space Data & Analytics',         1, 'Satellite data adjacency — benefits from constellation scale'),
('Space-Grade Semiconductors',     2, 'Supply chain beneficiary — satellite production scale-up'),
('Defence Prime Adjacency',        2, 'Dual-use launch/payload overlap — secondary rerating'),
('Ground Station Infrastructure',  2, 'Demand for ground segment as constellation density grows'),
('Capital Rotation / ETFs',        3, 'Macro flows into frontier tech as SpaceX IPO reprices the narrative');

INSERT INTO ares.equities (name, ticker, exchange, sector_id, country, thesis_note) VALUES
('Rocket Lab USA',     'RKLB', 'NASDAQ', 1, 'US', 'Most direct public comparator — launch services, valuation ceiling lifted by SpaceX IPO'),
('Planet Labs',        'PL',   'NYSE',   2, 'US', 'Earth observation — SpaceX customer/partner, institutional rerating candidate'),
('Spire Global',       'SPIR', 'NYSE',   4, 'US', 'Space data analytics — satellite infrastructure adjacency'),
('AST SpaceMobile',    'ASTS', 'NASDAQ', 3, 'US', 'LEO broadband — narrative gravity, direct Starlink comparison frame'),
('Intuitive Machines', 'LUNR', 'NASDAQ', 1, 'US', 'Deep space/lunar — NASA contract adjacency, small-cap optionality'),
('Mynaric AG',         'MYNA', 'NASDAQ', 5, 'DE', 'Laser comms — Starlink laser link supply chain play'),
('Kratos Defense',     'KTOS', 'NASDAQ', 6, 'US', 'Dual-use satellite/defence — space infrastructure second-order'),
('Viasat',             'VSAT', 'NASDAQ', 7, 'US', 'Ground station and satellite comms — demand beneficiary');

-- ============================================================
-- Confirm
-- ============================================================
SELECT 'ares schema applied successfully' AS status;
SELECT name, order_level FROM ares.sectors ORDER BY order_level, name;
SELECT name, ticker FROM ares.equities ORDER BY sector_id;
