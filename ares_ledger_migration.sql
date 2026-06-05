-- =============================================================================
-- Trygg Ares — Ledger Refactor & Seed Migration
-- =============================================================================
-- Apply with:
--   docker cp ares_ledger_migration.sql trygg-db:/tmp/ledger.sql
--   docker exec -it trygg-db psql -U trygg -d trygg -f /tmp/ledger.sql
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- Drop and recreate ares.ledger
-- -----------------------------------------------------------------------------

DROP VIEW IF EXISTS ares.active_ledger;
DROP VIEW IF EXISTS ares.open_positions;
DROP TABLE IF EXISTS ares.ledger CASCADE;

CREATE TABLE ares.ledger (
    id              SERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Signal stage
    signal_date         DATE NOT NULL,
    entity_ticker       TEXT NOT NULL,
    claim               TEXT NOT NULL,
    signal_pool_ref     TEXT,
    source_max_tier     INT,
    source_summary      TEXT,

    -- Recommendation stage
    system_recommendation   TEXT NOT NULL,
    recommendation_detail   TEXT,
    proposed_action         TEXT,

    -- Decision stage
    decision_status     TEXT NOT NULL DEFAULT 'pending',
    decision_date       DATE,
    decision_rationale  TEXT,

    -- Execution stage (paper account)
    executed            BOOLEAN DEFAULT FALSE,
    execution_date      DATE,
    instrument          TEXT,
    position_size_pct   NUMERIC,
    entry_price         NUMERIC,
    exit_price          NUMERIC,
    exit_date           DATE,
    stop_level          NUMERIC,
    notes               TEXT,

    -- Resolution stage (for WITHHOLDs)
    required_primary    TEXT,
    resolved            BOOLEAN DEFAULT FALSE,
    resolution_date     DATE,
    resolution_summary  TEXT,
    resolution_verdict  TEXT,

    -- Outcome stage (30/60/90 day reflection)
    outcome_30day       TEXT,
    outcome_60day       TEXT,
    outcome_90day       TEXT,
    catalyst_outcome    TEXT,

    -- System performance audit
    system_was_correct  TEXT,
    lesson_learned      TEXT,

    CONSTRAINT valid_recommendation CHECK (
        system_recommendation IN ('action','watch','review','withhold','thesis_intact')
    ),
    CONSTRAINT valid_decision CHECK (
        decision_status IN ('pending','accepted','modified','rejected','deferred')
    )
);

CREATE INDEX idx_ledger_entity      ON ares.ledger(entity_ticker);
CREATE INDEX idx_ledger_status      ON ares.ledger(decision_status);
CREATE INDEX idx_ledger_resolved    ON ares.ledger(resolved);
CREATE INDEX idx_ledger_signal_date ON ares.ledger(signal_date DESC);

COMMENT ON TABLE ares.ledger IS 'Canonical decision/outcome ledger for paper-trading validation phase. Every system recommendation, decision, execution, resolution, and outcome lives here. Markdown human-readable version maintained alongside in ares_ledger.md.';


-- -----------------------------------------------------------------------------
-- Trigger to maintain updated_at
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION ares.touch_ledger_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ledger_updated_at ON ares.ledger;
CREATE TRIGGER ledger_updated_at
    BEFORE UPDATE ON ares.ledger
    FOR EACH ROW
    EXECUTE FUNCTION ares.touch_ledger_updated_at();


-- -----------------------------------------------------------------------------
-- View: open ledger items (unresolved WITHHOLDs + open positions)
-- -----------------------------------------------------------------------------

CREATE VIEW ares.active_ledger AS
SELECT
    id,
    signal_date,
    entity_ticker,
    claim,
    system_recommendation,
    decision_status,
    CASE
        WHEN system_recommendation = 'withhold' AND NOT resolved THEN 'PENDING VERIFICATION'
        WHEN executed AND exit_date IS NULL THEN 'OPEN POSITION'
        WHEN decision_status = 'pending' THEN 'AWAITING DECISION'
        ELSE 'OTHER'
    END AS state,
    required_primary,
    proposed_action,
    notes
FROM ares.ledger
WHERE
    (system_recommendation = 'withhold' AND NOT resolved)
    OR (executed = TRUE AND exit_date IS NULL)
    OR decision_status = 'pending'
ORDER BY signal_date DESC, id DESC;

COMMENT ON VIEW ares.active_ledger IS 'Live operational view: anything currently demanding attention. PENDING VERIFICATION items, open paper positions, undecided recommendations.';


-- =============================================================================
-- Seed: session-to-date entries
-- =============================================================================

-- Entry 1: RKLB SDA contract value (PENDING, low priority — no current position)
INSERT INTO ares.ledger (
    signal_date, entity_ticker, claim, signal_pool_ref,
    source_max_tier, source_summary,
    system_recommendation, recommendation_detail, proposed_action,
    decision_status,
    required_primary
) VALUES (
    '2026-05-31',
    'RKLB',
    'SDA Tracking Layer Tranche 3 contract cleared; value reported as $3.5B (Seeking Alpha) vs $1.3B+ (Benzinga)',
    'Ares digest 31-May (signals 18, 19)',
    3,
    'Seeking Alpha + Benzinga (both Tier 3), figures conflict',
    'withhold',
    'Conflicting Tier 3 figures on load-bearing valuation input; automatic WITHHOLD per protocol. Milestone passage itself credible colour both sources agree on.',
    'Use SDA contract value as conviction input for CSP entry sizing — WITHHELD until Tier 1 confirms',
    'deferred',
    'RKLB 8-K or press release stating contract value (SEC EDGAR, Tier 1) OR SDA award notice (Tier 1)'
);

-- Entry 2: IONQ Q1 revenue (PENDING)
INSERT INTO ares.ledger (
    signal_date, entity_ticker, claim, signal_pool_ref,
    source_max_tier, source_summary,
    system_recommendation, recommendation_detail, proposed_action,
    decision_status,
    required_primary,
    notes
) VALUES (
    '2026-05-31',
    'IONQ',
    'Q1 2026 revenue $64.7M (+755% YoY), RPO $470M (+554%)',
    'Ares digest 31-May (signal 8)',
    3,
    'Seeking Alpha contributor (Tier 3)',
    'withhold',
    'Tier 3 source for load-bearing financial figures; cannot adjust CSP strike or sizing without SEC confirmation. Q2 earnings 12 August 2026 is the next Tier 1 fundamental gate.',
    'Adjust CSP entry strike or sizing based on Q1 figures',
    'pending',
    'IONQ Q1 2026 10-Q or 8-K on SEC EDGAR (Tier 1)',
    'Opus 4.8 verification noted figures appear correct on Tier 1 sources; system was conservative direction (benign). CSP framework $55-60 unchanged either way.'
);

-- Entry 3: ASTS Falcon 9 schedule (PENDING)
INSERT INTO ares.ledger (
    signal_date, entity_ticker, claim, signal_pool_ref,
    source_max_tier, source_summary,
    system_recommendation, recommendation_detail, proposed_action,
    decision_status,
    required_primary,
    notes
) VALUES (
    '2026-06-01',
    'ASTS',
    'Blue Origin static-fire explosion 29-May materially delays ASTS BlueBird 8/9/10 mid-June Falcon 9 launch',
    'Ares digest 01-Jun (signals 22, 23, 26)',
    3,
    'Barchart (Tier 3) for causal inference; MarketScreener (Tier 2) confirms explosion only, not Falcon 9 schedule impact',
    'withhold',
    'Gate caught Tier 3 causal inference resting on Tier 2 source that confirmed only the explosion. ASTS BlueBird launches on Falcon 9 (SpaceX), not New Glenn (Blue Origin) — schedule impact is not mechanically determined.',
    'Suspend ASTS re-entry watch based on launch delay',
    'pending',
    'SpaceX manifest update OR ASTS IR statement OR FAA launch licence status (Tier 1)',
    'Critical distinction: BlueBird on Falcon 9, not New Glenn. Barchart conflated the two. System refused to inherit the inference — gate operating exactly as designed.'
);

-- Entry 4: LUNR LTV exclusion (RESOLVED 01-Jun)
INSERT INTO ares.ledger (
    signal_date, entity_ticker, claim, signal_pool_ref,
    source_max_tier, source_summary,
    system_recommendation, recommendation_detail, proposed_action,
    decision_status,
    required_primary,
    resolved, resolution_date, resolution_summary, resolution_verdict,
    system_was_correct, lesson_learned,
    notes
) VALUES (
    '2026-05-31',
    'LUNR',
    'NASA excluded LUNR from Lunar Terrain Vehicle program contract awards',
    'Ares digest 31-May & 01-Jun (signals 2, 4, 29)',
    3,
    'Benzinga + Barchart (both Tier 3); CBS News (Tier 3) on adjacent Artemis moon-base awards',
    'withhold',
    'Tier 3 sources alone insufficient for sizing/conviction adjustment. Briefing initially framed as past-tense (excluded, thesis damaged) on Tier 3 only — gate caught this in subsequent run.',
    'Review LUNR sizing and conviction based on LTV exclusion',
    'pending',
    'NASA award announcement (nasa.gov, Tier 1) OR LUNR 8-K (SEC EDGAR, Tier 1)',
    TRUE,
    '2026-06-01',
    'NASA Phase 1 LTV task orders awarded: Lunar Outpost ($220M) and Astrolab ($219M) for rovers; Blue Origin ($188M, options to $280M) for delivery. LUNR not selected for either rover or delivery. LTV Services framework continues through 2039 with phased on-ramps — future task orders possible. LUNR retains dominant CLPS prime contractor position (IM-3 Trinity on manifest). LUNR also won $20M reconnaissance contracts (LRO, ShadowCam) confirming continued NASA engagement.',
    'confirmed',
    'yes',
    'WITHHOLD discipline worked exactly as designed: system refused to assert load-bearing conclusion on Tier 3, named specific Tier 1 primary, primary fetched within hours, decision made with full context. This is the loop the source-authority gate was built for.',
    'Sizing decision pending. Options: (a) hold v5 sizing as-is (5-7%/10%, LTV was bonus); (b) trim upper range (4-5%/8%, calibrate to weaker thesis); (c) wait for Q2 earnings (early Aug) before sizing up. Q2 print is first hard read on organic revenue without LTV. Thesis component on surface infrastructure weakened; CLPS-centric component intact.'
);

-- -----------------------------------------------------------------------------
-- Verification queries
-- -----------------------------------------------------------------------------

\echo
\echo '=== Ledger seeded ==='
SELECT id, signal_date, entity_ticker, system_recommendation, decision_status,
       CASE WHEN resolved THEN 'RESOLVED' ELSE 'OPEN' END AS resolution_state
FROM ares.ledger
ORDER BY id;

\echo
\echo '=== Active ledger (needs attention) ==='
SELECT entity_ticker, state, claim FROM ares.active_ledger;

COMMIT;
