"""
Trygg Ares — Triage Agent
=========================

Scores untriaged signals from ares.signals using Claude Haiku 4.5.

Discipline applied per Source-Authority Spec v1:
- source_tier classified into 1-4 (primary regulatory → social/AI)
- authority_tier set after corroboration logic
- is_recency_critical flagged for adverse events
- Tier 4 cap: Class A claims from Tier 4 alone capped at relevance 4

Discipline applied per Ares Universe Doc v5:
- relevance_score for name-specific impact (1-10)
- narrative_score for cross-position read-through (1-10)
- Awareness of SpaceX-IPO and quantum-correlated clusters

Reads the system prompt from triage_prompt.md (sibling file) so the prompt
can be edited independently of the agent code.

Schedule via scheduler.py — typically 30 minutes after feed ingestion runs.
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

import anthropic
import asyncpg
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

TRIAGE_MODEL = "claude-haiku-4-5-20251001"
BATCH_SIZE = 20            # Signals scored per run
MAX_BODY_CHARS = 8000      # Truncate signal body to keep prompts efficient
MAX_OUTPUT_TOKENS = 512    # Triage output is small JSON; cap to control cost

# Load system prompt from external markdown file
PROMPT_PATH = Path(__file__).parent / "triage_prompt.md"
if not PROMPT_PATH.exists():
    raise FileNotFoundError(
        f"Triage prompt not found at {PROMPT_PATH}. "
        "The prompt is a required dependency."
    )
SYSTEM_PROMPT = PROMPT_PATH.read_text(encoding="utf-8")

REQUIRED_FIELDS = {
    "source_tier", "authority_tier", "is_recency_critical",
    "relevance_score", "sentiment", "implication",
    "narrative_score", "narrative_notes",
}


# -----------------------------------------------------------------------------
# Triage a single signal
# -----------------------------------------------------------------------------

async def triage_one_signal(
    client: anthropic.AsyncAnthropic,
    signal: dict,
) -> Optional[dict]:
    """Score one signal. Returns dict of fields to update, or None on failure."""

    body_excerpt = (signal.get("body") or "")[:MAX_BODY_CHARS]
    user_message = (
        f"Entity: {signal['ticker']} ({signal['entity_name']})\n"
        f"Source: {signal['source']}\n"
        f"URL: {signal.get('url') or 'n/a'}\n"
        f"Published: {signal.get('published_at') or 'n/a'}\n"
        f"Headline: {signal['headline']}\n\n"
        f"Body excerpt:\n{body_excerpt}"
    )

    try:
        response = await client.messages.create(
            model=TRIAGE_MODEL,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_message}],
        )
    except anthropic.APIError as e:
        logger.error("API error for signal %s: %s", signal["id"], e)
        return None

    raw = response.content[0].text.strip()

    # Strip markdown fences if the model includes them despite instructions
    if raw.startswith("```"):
        parts = raw.split("```")
        if len(parts) >= 2:
            raw = parts[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(
            "JSON parse failed for signal %s: %s\nRaw output: %s",
            signal["id"], e, raw[:300]
        )
        return None

    missing = REQUIRED_FIELDS - set(parsed.keys())
    if missing:
        logger.error(
            "Signal %s missing required fields: %s",
            signal["id"], missing
        )
        return None

    # Apply Tier 4 cap defensively (the prompt asks for this, but enforce here too)
    if parsed["source_tier"] == 4 and parsed["relevance_score"] > 4:
        logger.info(
            "Applied Tier 4 cap to signal %s (was %d → 4)",
            signal["id"], parsed["relevance_score"]
        )
        parsed["relevance_score"] = 4

    return parsed


# -----------------------------------------------------------------------------
# Main triage loop
# -----------------------------------------------------------------------------

async def run_triage():
    """Pull untriaged signals, score with Haiku, write results back."""

    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set in .env — aborting")
        return
    if not DATABASE_URL:
        logger.error("DATABASE_URL not set in .env — aborting")
        return

    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)

    # Open a job_run row for audit logging
    # NOTE: assumes platform.job_runs has (job_name, started_at, completed_at,
    # status, records_processed) columns. Verify against your existing schema.
    try:
        async with pool.acquire() as conn:
            job_run_id = await conn.fetchval("""
                INSERT INTO platform.job_runs (job_name, job_module, started_at, status)
VALUES ('ares_triage', 'ares.agents.triage', NOW(), 'running')
                RETURNING id
            """)
    except asyncpg.PostgresError as e:
        logger.warning(
            "Could not insert job_run row (platform.job_runs schema may differ): %s. "
            "Continuing without audit row.",
            e
        )
        job_run_id = None

    # Pull untriaged signals (those with NULL relevance_score)
    async with pool.acquire() as conn:
        signals = await conn.fetch("""
            SELECT
                s.id, s.headline, s.url, s.source, s.body,
                s.published_at, s.entity_id,
                e.ticker, e.name AS entity_name
            FROM ares.signals s
            JOIN ares.entities e ON e.id = s.entity_id
            WHERE s.relevance_score IS NULL
            ORDER BY s.fetched_at ASC
            LIMIT $1
        """, BATCH_SIZE)

    if not signals:
        logger.info("No untriaged signals — nothing to do")
        if job_run_id is not None:
            async with pool.acquire() as conn:
                await conn.execute("""
                    UPDATE platform.job_runs
                    SET completed_at = NOW(),
                        status = 'completed',
                        records_processed = 0
                    WHERE id = $1
                """, job_run_id)
        await pool.close()
        await client.close()
        return

    logger.info("Triaging %d signals...", len(signals))

    scored = 0
    failed = 0
    recency_critical_count = 0

    for signal in signals:
        result = await triage_one_signal(client, dict(signal))

        if result is None:
            failed += 1
            continue

        # Write back to database
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE ares.signals SET
                    source_tier         = $1,
                    authority_tier      = $2,
                    is_recency_critical = $3,
                    relevance_score     = $4,
                    sentiment           = $5,
                    implication         = $6,
                    narrative_score     = $7,
                    narrative_notes     = $8,
                    triage_model        = $9,
                    triage_run_id       = $10
                WHERE id = $11
            """,
                result["source_tier"],
                result["authority_tier"],
                result["is_recency_critical"],
                result["relevance_score"],
                result["sentiment"],
                result["implication"],
                result["narrative_score"],
                result["narrative_notes"],
                TRIAGE_MODEL,
                job_run_id,
                signal["id"],
            )

        scored += 1
        if result["is_recency_critical"]:
            recency_critical_count += 1
            logger.warning(
                "RECENCY-CRITICAL: signal %d (%s) — %s",
                signal["id"], signal["ticker"], signal["headline"]
            )

    # Close the job_run audit row
    if job_run_id is not None:
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE platform.job_runs
                SET completed_at = NOW(),
                    status = $1,
                    records_processed = $2
                WHERE id = $3
            """,
                "completed" if failed == 0 else "completed_with_errors",
                scored,
                job_run_id,
            )

    await pool.close()
    await client.close()

    logger.info(
        "Triage complete — %d scored, %d failed, %d recency-critical flagged",
        scored, failed, recency_critical_count
    )


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(run_triage())
