"""
Trygg Ares — Digest Agent
=========================

Synthesises triaged signals into a daily briefing using Claude Sonnet 4.6.

Discipline applied per Source-Authority Spec v1:
- Inline provenance: every numeric claim cites source + tier
- Tier 4 sources excluded unless flagged is_recency_critical
- Tier breakdown stored in ares.digests for audit
- Highest-tier source preferred when signals corroborate same fact

Inclusion threshold (OR conditions — any one triggers inclusion):
- relevance_score >= 6
- is_recency_critical = TRUE
- narrative_score >= 7

Telegram delivery:
- Direct HTTP via aiohttp (avoids the python-telegram-bot async/sync issue
  that caused seven days of Mímir silence)
- Chunked at 3500 chars to stay below Telegram's 4096 limit
- Markdown parse mode for inline formatting

CLI smoke-test mode:
- Set DIGEST_SKIP_SEND=true to generate digest without Telegram delivery

Schedule via scheduler.py — runs daily at 07:15 UTC (after Ares triage).
"""

import asyncio
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import aiohttp
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
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SKIP_SEND = os.getenv("DIGEST_SKIP_SEND", "").lower() in ("true", "1", "yes")

DIGEST_MODEL = "claude-sonnet-4-6"

RELEVANCE_THRESHOLD = 6
NARRATIVE_THRESHOLD = 7
LOOKBACK_HOURS = 24

MAX_OUTPUT_TOKENS = 4096
TELEGRAM_MAX_CHARS = 3500
MAX_BODY_CHARS_PER_SIGNAL = 800

PROMPT_PATH = Path(__file__).parent / "digest_prompt.md"
if not PROMPT_PATH.exists():
    raise FileNotFoundError(
        f"Digest prompt not found at {PROMPT_PATH}. "
        "The prompt is a required dependency."
    )
SYSTEM_PROMPT = PROMPT_PATH.read_text(encoding="utf-8")


# -----------------------------------------------------------------------------
# Signal formatting
# -----------------------------------------------------------------------------

def format_signal_for_prompt(signal: dict) -> str:
    """Format one signal as a structured block for the digest prompt."""
    body_excerpt = (signal.get("body") or "")[:MAX_BODY_CHARS_PER_SIGNAL]
    return (
        f"---\n"
        f"signal_id: {signal['id']}\n"
        f"Entity: {signal['ticker']} ({signal['entity_name']})\n"
        f"Universe tier: {signal['universe_tier']}\n"
        f"Headline: {signal['headline']}\n"
        f"Source: {signal['source']} (Tier {signal['source_tier']})\n"
        f"URL: {signal.get('url') or 'n/a'}\n"
        f"Published: {signal.get('published_at') or 'n/a'}\n"
        f"Relevance: {signal['relevance_score']}/10\n"
        f"Narrative: {signal['narrative_score']}/10\n"
        f"Sentiment: {signal['sentiment']}\n"
        f"Recency-critical: {signal['is_recency_critical']}\n"
        f"Implication: {signal['implication']}\n"
        f"Narrative notes: {signal.get('narrative_notes') or ''}\n"
        f"Body excerpt: {body_excerpt}\n"
    )


# -----------------------------------------------------------------------------
# Telegram delivery
# -----------------------------------------------------------------------------

def chunk_for_telegram(text: str, max_chars: int = TELEGRAM_MAX_CHARS) -> list:
    """Split text on paragraph boundaries to fit Telegram's 4096-char limit."""
    chunks: list = []
    remaining = text
    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n\n", 0, max_chars)
        if split_at == -1:
            split_at = remaining.rfind("\n", 0, max_chars)
        if split_at == -1:
            split_at = max_chars
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return chunks


async def send_telegram_message(
    session: aiohttp.ClientSession,
    chat_id: int,
    text: str,
) -> bool:
    """Send a Telegram message via direct HTTP. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set — cannot send")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = chunk_for_telegram(text)

    for i, chunk in enumerate(chunks):
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            async with session.post(
                url, json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    logger.error(
                        "Telegram send failed (chunk %d/%d): status=%d body=%s",
                        i + 1, len(chunks), resp.status, err[:200]
                    )
                    return False
        except Exception as e:
            logger.error("Telegram send error: %s", e)
            return False

        if i < len(chunks) - 1:
            await asyncio.sleep(0.5)

    return True


# -----------------------------------------------------------------------------
# Main digest run
# -----------------------------------------------------------------------------

async def run_digest():
    """Pull recent triaged signals, synthesise briefing, deliver."""

    if not DATABASE_URL:
        logger.error("DATABASE_URL not set in .env — aborting")
        return
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set in .env — aborting")
        return
    if not TELEGRAM_BOT_TOKEN and not SKIP_SEND:
        logger.warning(
            "TELEGRAM_BOT_TOKEN not set — digest will be generated but not delivered"
        )

    if SKIP_SEND:
        logger.info("DIGEST_SKIP_SEND=true — digest will print to stdout, not Telegram")

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

    # Open audit row
    try:
        async with pool.acquire() as conn:
            job_run_id = await conn.fetchval("""
                INSERT INTO platform.job_runs (job_name, job_module, started_at, status)
                VALUES ('ares_digest', 'ares.agents.digest', NOW(), 'running')
                RETURNING id
            """)
    except asyncpg.PostgresError as e:
        logger.warning("Could not insert job_run row: %s", e)
        job_run_id = None

    # Pull signals meeting threshold
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    async with pool.acquire() as conn:
        signals = await conn.fetch("""
            SELECT
                s.id, s.headline, s.url, s.source, s.body,
                s.published_at, s.source_tier, s.authority_tier,
                s.is_recency_critical, s.relevance_score, s.narrative_score,
                s.sentiment, s.implication, s.narrative_notes,
                e.ticker, e.name AS entity_name, e.sector, e.universe_tier
            FROM ares.signals s
            JOIN ares.entities e ON e.id = s.entity_id
            WHERE s.fetched_at >= $1
              AND s.relevance_score IS NOT NULL
              AND (
                  s.relevance_score >= $2
                  OR s.is_recency_critical = TRUE
                  OR s.narrative_score >= $3
              )
            ORDER BY
                s.is_recency_critical DESC,
                s.relevance_score DESC,
                s.narrative_score DESC,
                e.universe_tier ASC
        """, cutoff, RELEVANCE_THRESHOLD, NARRATIVE_THRESHOLD)

    if not signals:
        logger.info(
            "No signals meet digest threshold (relevance >= %d OR critical OR narrative >= %d) "
            "in last %d hours — skipping",
            RELEVANCE_THRESHOLD, NARRATIVE_THRESHOLD, LOOKBACK_HOURS
        )
        if job_run_id is not None:
            async with pool.acquire() as conn:
                await conn.execute("""
                    UPDATE platform.job_runs
                    SET completed_at = NOW(), status = 'completed', records_processed = 0
                    WHERE id = $1
                """, job_run_id)
        await pool.close()
        await client.close()
        return

    logger.info("Synthesising digest from %d signals", len(signals))

    # Tier breakdown audit
    tier_breakdown = {f"tier_{i}": 0 for i in range(1, 5)}
    for s in signals:
        t = s["source_tier"]
        if 1 <= t <= 4:
            tier_breakdown[f"tier_{t}"] += 1
    logger.info("Tier breakdown of input signals: %s", tier_breakdown)

    # Build user message
    today = date.today()
    signal_blocks = [format_signal_for_prompt(dict(s)) for s in signals]
    user_message = (
        f"Today's date: {today.strftime('%A, %d %B %Y')}\n"
        f"Window: Last {LOOKBACK_HOURS} hours\n"
        f"Signals to synthesise: {len(signals)}\n"
        f"Tier breakdown: {tier_breakdown}\n\n"
        f"=== SIGNALS ===\n\n"
        + "\n".join(signal_blocks)
        + "\n\nSynthesise into a Trygg Ares Briefing per the structure in your system prompt."
    )

    # Sonnet 4.6 synthesis
    try:
        response = await client.messages.create(
            model=DIGEST_MODEL,
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
        logger.error("Anthropic API error: %s", e)
        await pool.close()
        await client.close()
        return

    digest_content = response.content[0].text.strip()

    # No-signal sentinel from the model
    if digest_content == "NO_SIGNIFICANT_SIGNALS":
        logger.info("Sonnet judged no significant signals warrant a briefing — no delivery")
        if job_run_id is not None:
            async with pool.acquire() as conn:
                await conn.execute("""
                    UPDATE platform.job_runs
                    SET completed_at = NOW(), status = 'completed', records_processed = $1
                    WHERE id = $2
                """, 0, job_run_id)
        await pool.close()
        await client.close()
        return

    # Store digest
    signal_ids = [s["id"] for s in signals]
    async with pool.acquire() as conn:
        digest_id = await conn.fetchval("""
            INSERT INTO ares.digests (
                digest_date, digest_type, content, signal_ids_included,
                signal_count, relevance_threshold, has_inline_provenance,
                tier_breakdown, digest_model, generation_run_id
            )
            VALUES ($1, 'daily', $2, $3, $4, $5, TRUE, $6, $7, $8)
            RETURNING id
        """,
            today,
            digest_content,
            signal_ids,
            len(signals),
            RELEVANCE_THRESHOLD,
            json.dumps(tier_breakdown),
            DIGEST_MODEL,
            job_run_id,
        )

    logger.info(
        "Digest %d stored — %d signals, tier breakdown: %s",
        digest_id, len(signals), tier_breakdown
    )

    # Skip-send mode: print and return
    if SKIP_SEND:
        print("\n" + "=" * 70)
        print("DIGEST PREVIEW (SKIP_SEND mode — not delivered to Telegram)")
        print("=" * 70 + "\n")
        print(digest_content)
        print("\n" + "=" * 70 + "\n")
        if job_run_id is not None:
            async with pool.acquire() as conn:
                await conn.execute("""
                    UPDATE platform.job_runs
                    SET completed_at = NOW(), status = 'completed', records_processed = $1
                    WHERE id = $2
                """, len(signals), job_run_id)
        await pool.close()
        await client.close()
        return

    # Deliver to premium clients
    async with pool.acquire() as conn:
        clients = await conn.fetch("""
            SELECT id, name, telegram_chat_id
            FROM platform.clients
            WHERE tier = 'premium' AND telegram_chat_id IS NOT NULL
        """)

    if not clients:
        logger.warning("No premium clients with Telegram chat IDs — stored but not delivered")
    else:
        async with aiohttp.ClientSession() as session:
            sent_to = []
            for c in clients:
                chat_id_raw = c["telegram_chat_id"]
                try:
                    chat_id_int = int(chat_id_raw)
                except (TypeError, ValueError):
                    logger.error(
                        "Invalid Telegram chat_id for client %s: %r",
                        c["name"], chat_id_raw
                    )
                    continue
                logger.info("Sending digest to %s (chat %d)", c["name"], chat_id_int)
                ok = await send_telegram_message(session, chat_id_int, digest_content)
                if ok:
                    sent_to.append(c["id"])

        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE ares.digests
                SET sent_to_clients = $1, sent_at = NOW()
                WHERE id = $2
            """, sent_to, digest_id)

        logger.info("Digest delivered to %d/%d clients", len(sent_to), len(clients))

    if job_run_id is not None:
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE platform.job_runs
                SET completed_at = NOW(), status = 'completed', records_processed = $1
                WHERE id = $2
            """, len(signals), job_run_id)

    await pool.close()
    await client.close()
    logger.info("Digest run complete")


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(run_digest())
