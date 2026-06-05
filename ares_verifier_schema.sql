-- =============================================================================
-- Trygg Ares — Verifier schema additions
-- =============================================================================
-- Adds columns to ares.ledger to capture verifier output that doesn't fit the
-- existing resolution_* fields (which are for human-verified resolutions).
--
-- Apply with:
--   docker cp ares_verifier_schema.sql trygg-db:/tmp/verifier.sql
--   docker exec -it trygg-db psql -U trygg -d trygg -f /tmp/verifier.sql
-- =============================================================================

BEGIN;

-- Add columns for verifier output
ALTER TABLE ares.ledger
    ADD COLUMN IF NOT EXISTS verifier_attempted_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS verifier_proposal      TEXT,
    ADD COLUMN IF NOT EXISTS verifier_confidence    TEXT,
    ADD COLUMN IF NOT EXISTS verifier_source_tier   INT,
    ADD COLUMN IF NOT EXISTS verifier_sources_used  TEXT,
    ADD COLUMN IF NOT EXISTS verifier_evidence      TEXT,
    ADD COLUMN IF NOT EXISTS verifier_auto_resolved BOOLEAN DEFAULT FALSE;

-- Constraint on confidence values (allow NULL for unprocessed rows)
ALTER TABLE ares.ledger DROP CONSTRAINT IF EXISTS valid_verifier_confidence;
ALTER TABLE ares.ledger ADD CONSTRAINT valid_verifier_confidence CHECK (
    verifier_confidence IS NULL OR
    verifier_confidence IN ('high', 'medium', 'low', 'failed')
);

-- Refresh the active_ledger view to surface verifier state
DROP VIEW IF EXISTS ares.active_ledger;
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
    notes,
    -- Verifier columns now surfaced in the active view
    verifier_attempted_at,
    verifier_confidence,
    verifier_source_tier,
    verifier_proposal,
    verifier_auto_resolved
FROM ares.ledger
WHERE
    (system_recommendation = 'withhold' AND NOT resolved)
    OR (executed = TRUE AND exit_date IS NULL)
    OR decision_status = 'pending'
ORDER BY signal_date DESC, id DESC;

COMMENT ON VIEW ares.active_ledger IS 'Live operational view: anything currently demanding attention. PENDING VERIFICATION items, open paper positions, undecided recommendations. Includes verifier output for unresolved WITHHOLDs.';

-- Verifier-specific operational view: items the verifier has touched
CREATE OR REPLACE VIEW ares.verifier_audit AS
SELECT
    id,
    signal_date,
    entity_ticker,
    claim,
    verifier_attempted_at,
    verifier_confidence,
    verifier_source_tier,
    verifier_auto_resolved,
    CASE
        WHEN verifier_auto_resolved THEN 'AUTO-RESOLVED'
        WHEN verifier_attempted_at IS NOT NULL AND NOT resolved THEN 'PROPOSAL PENDING REVIEW'
        WHEN verifier_attempted_at IS NULL THEN 'NOT YET ATTEMPTED'
        WHEN resolved THEN 'HUMAN RESOLVED'
        ELSE 'OTHER'
    END AS verifier_state,
    verifier_proposal,
    verifier_sources_used
FROM ares.ledger
WHERE system_recommendation = 'withhold'
ORDER BY verifier_attempted_at DESC NULLS LAST, id DESC;

COMMENT ON VIEW ares.verifier_audit IS 'Verifier performance log: shows what the verifier has attempted, with confidence and source tier. Used for spot-checking auto-resolutions.';

\echo
\echo '=== ares.ledger now has verifier columns ==='
SELECT column_name, data_type FROM information_schema.columns
WHERE table_schema='ares' AND table_name='ledger'
  AND column_name LIKE 'verifier_%'
ORDER BY ordinal_position;

\echo
\echo '=== Views refreshed ==='
\dv ares.*

COMMIT;
