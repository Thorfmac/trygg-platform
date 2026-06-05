"""
Trygg Ares — Digest Agent (multi-briefing-type)
================================================

Synthesises triaged signals into a briefing via Claude Sonnet 4.6. Supports
three briefing types selected via BRIEFING_TYPE env var or function parameter:

  - morning   (07:00 UTC, 24h lookback, comprehensive review)
      Read in UK morning before US markets open.
  - midday    (09:00 America/New_York, 6h lookback, pre-open heads-up)
      Read 30 min before NYSE open. Short, focused on what's new since morning.
  - postclose (16:30 America/New_York, 11h lookback, post-close brief)
      Read after NYSE close. What moved today and what filed after-hours.

Each briefing type uses its own system prompt and lookback window. Everything
else (Telegram delivery, ledger storage, tier_breakdown audit) is shared.

Usage:
  python -m ares.agents.digest                          # default: morning
  BRIEFING_TYPE=midday python -m ares.agents.digest
  BRIEFING_TYPE=postclose python -m ares.agents.digest
  DIGEST_SKIP_SEND=true python -m ares.agents.digest    # preview to stdout

When called from scheduler.py:
  await run_digest(briefing_type="postclose")
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
# Briefing type configuration
# -----------------------------------------------------------------------------

BRIEFING_TYPES = {
    "morning": {
        "lookback_hours": 24,
        "prompt_file": "digest_prompt.md",
        "job_name": "ares_digest_morning",
        "label": "Morning Briefing",
        "description": "Comprehensive 24h review. Read in UK morning before US markets open.",
    },
    "midday": {
        "lookback_hours": 6,
        "prompt_file": "digest_midday_prompt.md",
        "job_name": "ares_digest_midday",
        "label": "Pre-Open Heads-Up",
        "description": "Light brief covering 6h since morning. Read 30 min before NYSE open.",
    },
    "postclose": {
        "lookback_hours": 11,
        "prompt_file": "digest_postclose_prompt.md",
        "job_name": "ares_digest_postclose",
        "label": "Post-Close Brief",
        "description": "Day's flow and after-hours filings. Read after NYSE close.",
    },
}


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
MAX_OUTPUT_TOKENS = 4096
TELEGRAM_MAX_CHARS = 3500
MAX_BODY_CHARS_PER_SIGNAL = 800


def get_briefing_config(briefing_type: str) -> dict:
    """Resolve briefing config dict from the type string."""
    bt = (briefing_type or "morning").lower()
    if bt not in BRIEFING_TYPES:
        logger.warning("Unknown briefing_type %r; defaulting to morning", bt)
        bt = "morning"
    return {"key": bt, **BRIEFING_TYPES[bt]}


def load_system_prompt(prompt_file: str) -> str:
    """Load a prompt file from the agent directory."""
    path = Path(__file__).parent / prompt_file
    if not path.exists():
        raise FileNotFoundError(
            f"Digest prompt not found at {path}. Required dependency."
        )
    return path.read_text(encoding="utf-8")


# -----------------------------------------------------------------------------
# Signal formatting
# -----------------------------------------------------------------------------

def format_signal_for_prompt(signal: dict) -> str:
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
    chunks = []
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

async def run_digest(briefing_type: str = None):
    """
    Pull recent triaged signals, synthesise via Sonnet, deliver to Telegram.

    Args:
        briefing_type: 'morning' | 'midday' | 'postclose'.
                       Falls back to BRIEFING_TYPE env var, then 'morning'.
    """
    if briefing_type is None:
        briefing_type = os.getenv("BRIEFING_TYPE", "morning")
    config = get_briefing_config(briefing_type)

    logger.info(
        "Starting %s digest: %s (lookback=%dh, prompt=%s)",
        config["key"], config["label"],
        config["lookback_hours"], config["prompt_file"],
    )

    if not DATABASE_URL:
        logger.error("DATABASE_URL not set — aborting")
        return
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set — aborting")
        return
    if not TELEGRAM_BOT_TOKEN and not SKIP_SEND:
        logger.warning(
            "TELEGRAM_BOT_TOKEN not set — digest will be generated but not delivered"
        )
    if SKIP_SEND:
        logger.info("DIGEST_SKIP_SEND=true — digest will print to stdout, not Telegram")

    system_prompt = load_system_prompt(config["prompt_file"])

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

    try:
        async with pool.acquire() as conn:
            job_run_id = await conn.fetchval("""
                INSERT INTO platform.job_runs (job_name, job_module, started_at, status)
                VALUES ($1, 'ares.agents.digest', NOW(), 'running')
                RETURNING id
            """, config["job_name"])
    except asyncpg.PostgresError as e:
        logger.warning("Could not insert job_run row: %s", e)
        job_run_id = None

    lookback_hours = config["lookback_hours"]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
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
            "No signals meet threshold for %s digest (lookback=%dh) — skipping",
            config["key"], lookback_hours,
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

    logger.info("Synthesising %s digest from %d signals", config["key"], len(signals))

    tier_breakdown = {f"tier_{i}": 0 for i in range(1, 5)}
    for s in signals:
        t = s["source_tier"]
        if 1 <= t <= 4:
            tier_breakdown[f"tier_{t}"] += 1
    logger.info("Tier breakdown of input signals: %s", tier_breakdown)

    today = date.today()
    signal_blocks = [format_signal_for_prompt(dict(s)) for s in signals]
    user_message = (
        f"Briefing type: {config['label']}\n"
        f"Today's date: {today.strftime('%A, %d %B %Y')}\n"
        f"Lookback window: Last {lookback_hours} hours\n"
        f"Signals to synthesise: {len(signals)}\n"
        f"Tier breakdown: {tier_breakdown}\n\n"
        f"=== SIGNALS ===\n\n"
        + "\n".join(signal_blocks)
        + "\n\nSynthesise into a Trygg Ares Briefing per the structure in your system prompt."
    )

    try:
        response = await client.messages.create(
            model=DIGEST_MODEL,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
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

    if digest_content == "NO_SIGNIFICANT_SIGNALS":
        logger.info(
            "Sonnet judged no significant signals warrant a %s briefing — no delivery",
            config["key"]
        )
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

    signal_ids = [s["id"] for s in signals]
    async with pool.acquire() as conn:
        digest_id = await conn.fetchval("""
            INSERT INTO ares.digests (
                digest_date, digest_type, content, signal_ids_included,
                signal_count, relevance_threshold, has_inline_provenance,
                tier_breakdown, digest_model, generation_run_id
            )
            VALUES ($1, $2, $3, $4, $5, $6, TRUE, $7, $8, $9)
            RETURNING id
        """,
            today,
            config["key"],
            digest_content,
            signal_ids,
            len(signals),
            RELEVANCE_THRESHOLD,
            json.dumps(tier_breakdown),
            DIGEST_MODEL,
            job_run_id,
        )

    logger.info(
        "%s digest %d stored — %d signals, tier breakdown: %s",
        config["label"], digest_id, len(signals), tier_breakdown,
    )

    if SKIP_SEND:
        print("\n" + "=" * 70)
        print(f"{config['label'].upper()} PREVIEW (SKIP_SEND mode — not delivered to Telegram)")
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
                logger.info("Sending %s digest to %s (chat %d)",
                            config["key"], c["name"], chat_id_int)
                ok = await send_telegram_message(session, chat_id_int, digest_content)
                if ok:
                    sent_to.append(c["id"])

        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE ares.digests
                SET sent_to_clients = $1, sent_at = NOW()
                WHERE id = $2
            """, sent_to, digest_id)

        logger.info("%s digest delivered to %d/%d clients",
                    config["label"], len(sent_to), len(clients))

    if job_run_id is not None:
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE platform.job_runs
                SET completed_at = NOW(), status = 'completed', records_processed = $1
                WHERE id = $2
            """, len(signals), job_run_id)

    await pool.close()
    await client.close()
    logger.info("%s digest run complete", config["label"])


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(run_digest())
