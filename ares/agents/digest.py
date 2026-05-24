"""
Trygg Ares — Digest Agent
Synthesises daily briefing from triaged signals using Claude Sonnet 4.6.
Delivers via Telegram and stores in ares.digests.

Schedule: 07:15 UTC daily
"""

import asyncio
import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Optional

import anthropic
import aiohttp
import asyncpg
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DATABASE_URL        = os.getenv("DATABASE_URL")
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_OWNER_CHAT_ID")

DIGEST_MODEL        = "claude-sonnet-4-6"
MIN_RELEVANCE_SCORE = 5
MIN_NARRATIVE_SCORE = 5


SYSTEM_PROMPT = """You are Trygg Ares, an investment intelligence system specialising in the second and third order market effects of the SpaceX IPO.

You produce concise, analyst-quality daily briefings for a sophisticated private investor. Your tone is direct, precise, and actionable. No fluff. No generic statements.

Format your response exactly as follows — use these exact headers:

TRYGG ARES BRIEFING
[Date] — SpaceX IPO Market Effects Monitor
For: Thorfinn

HEADLINE
[One sentence. The single most significant signal today and what it means for the SpaceX thesis.]

SIGNALS
[Group by equity. For each equity with signals, write:]
[COMPANY] ([TICKER]) — [ORDER] effect — Score: [highest narrative score]/10 | [valuation_flag]
• [Signal 1 implication]
• [Signal 2 implication if exists]

UNDERVALUATION RADAR
[List any equities flagged 'undervalued'. If none, write: No undervalued flags today.]

THESIS PULSE
[2-3 sentences. Overall read on SpaceX IPO narrative strength today. Constructive/Cautious/Neutral and why.]

Keep the entire briefing under 600 words. Use plain text only — no markdown bold, no asterisks."""


def format_signals_for_prompt(signals: list[dict]) -> str:
    """Format signals grouped by equity for the digest prompt."""
    # Group by equity
    by_equity = {}
    for sig in signals:
        ticker = sig["ticker"]
        if ticker not in by_equity:
            by_equity[ticker] = {
                "name":         sig["equity_name"],
                "ticker":       ticker,
                "sector":       sig["sector_name"],
                "order_level":  sig["order_level"],
                "valuation_flag": sig["valuation_flag"],
                "signals":      [],
            }
        by_equity[ticker]["signals"].append({
            "title":          sig["title"],
            "relevance":      sig["relevance_score"],
            "narrative":      sig["narrative_score"],
            "sentiment":      sig["sentiment"],
            "implication":    sig["implication"],
        })

    # Build prompt text
    lines = []
    for ticker, data in sorted(by_equity.items(), key=lambda x: -max(s["narrative"] for s in x[1]["signals"])):
        top_score = max(s["narrative"] for s in data["signals"])
        lines.append(
            f"\n{data['name']} ({ticker}) — Order {data['order_level']} — "
            f"Top narrative score: {top_score}/10 | Valuation: {data['valuation_flag']}"
        )
        for sig in data["signals"]:
            lines.append(f"  Signal: {sig['title']}")
            lines.append(f"  Relevance: {sig['relevance']}/10 | Narrative: {sig['narrative']}/10 | {sig['sentiment']}")
            lines.append(f"  Implication: {sig['implication']}")

    return "\n".join(lines)


async def generate_digest(signals: list[dict]) -> Optional[str]:
    """Generate digest content using Claude Sonnet."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    today_str = date.today().strftime("%A, %d %B %Y")
    signals_text = format_signals_for_prompt(signals)

    user_prompt = f"""Today is {today_str}.

Here are today's Ares signals (all scored relevance >= {MIN_RELEVANCE_SCORE} and narrative >= {MIN_NARRATIVE_SCORE}):

{signals_text}

Produce the Trygg Ares daily briefing."""

    try:
        response = client.messages.create(
            model=DIGEST_MODEL,
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return response.content[0].text
    except Exception as e:
        logger.error(f"Digest generation error: {e}")
        return None


async def send_telegram(text: str) -> bool:
    """Send message via Telegram Bot API."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Telegram credentials not set")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    # Split if over Telegram's 4096 char limit
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]

    async with aiohttp.ClientSession() as session:
        for i, chunk in enumerate(chunks):
            try:
                async with session.post(url, json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text":    chunk,
                }, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        logger.info(f"Telegram chunk {i+1}/{len(chunks)} sent")
                    else:
                        body = await resp.text()
                        logger.error(f"Telegram error {resp.status}: {body[:200]}")
                        return False
            except Exception as e:
                logger.error(f"Telegram send error: {e}")
                return False
            if len(chunks) > 1:
                await asyncio.sleep(1)

    return True


async def run_digest():
    """Main entry point — generate and deliver daily Ares digest."""
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set — aborting")
        return

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)
    today = date.today()

    # Check if digest already sent today
    async with pool.acquire() as conn:
        existing = await conn.fetchval("""
            SELECT id FROM ares.digests
            WHERE digest_date = $1 AND sent_via_telegram = TRUE
        """, today)

    if existing:
        logger.info(f"Digest already sent for {today} — skipping")
        await pool.close()
        return

    # Fetch qualifying signals from today
    async with pool.acquire() as conn:
        signals = await conn.fetch("""
            SELECT
                sig.id,
                sig.title,
                sig.url,
                sig.source,
                sig.relevance_score,
                sig.narrative_score,
                sig.sentiment,
                sig.valuation_flag,
                sig.implication,
                e.name        AS equity_name,
                e.ticker,
                s.name        AS sector_name,
                s.order_level
            FROM ares.signals sig
            JOIN ares.equities e ON e.id = sig.equity_id
            JOIN ares.sectors  s ON s.id = e.sector_id
            WHERE sig.relevance_score >= $1
              AND sig.narrative_score  >= $2
              AND sig.created_at::date  = $3
            ORDER BY sig.narrative_score DESC, sig.relevance_score DESC
        """, MIN_RELEVANCE_SCORE, MIN_NARRATIVE_SCORE, today)

    if not signals:
        logger.warning(f"No qualifying signals for {today} — no digest generated")
        await pool.close()
        return

    logger.info(f"Generating digest from {len(signals)} qualifying signals...")

    signals_list = [dict(s) for s in signals]
    content = await generate_digest(signals_list)

    if not content:
        logger.error("Digest generation failed")
        await pool.close()
        return

    logger.info("Digest generated — sending to Telegram...")
    sent = await send_telegram(content)

    # Store digest regardless of Telegram outcome
    top_score = max(s["narrative_score"] for s in signals_list)
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO ares.digests (
                digest_date, content, signal_count,
                top_score, sent_via_telegram
            ) VALUES ($1,$2,$3,$4,$5)
        """, today, content, len(signals_list), top_score, sent)

    await pool.close()
    logger.info(
        f"Digest complete — {len(signals_list)} signals, "
        f"top score {top_score}, telegram={'sent' if sent else 'FAILED'}"
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(run_digest())
