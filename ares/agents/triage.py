"""
Trygg Ares — Triage Agent
Scores untriaged signals using Claude Haiku 4.5.
Three-dimension scoring: relevance, narrative (SpaceX read-through), valuation flag.

Schedule: 06:30 UTC daily (after feed ingestion)
"""

import asyncio
import json
import logging
import os
from typing import Optional

import anthropic
import asyncpg
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DATABASE_URL      = os.getenv("DATABASE_URL")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Model
TRIAGE_MODEL = "claude-haiku-4-5-20251001"

# Only score signals above this relevance threshold in digest
MIN_DIGEST_SCORE = 5

# Cached system prompt — reduces cost ~90% on repeated calls
SYSTEM_PROMPT = """You are Trygg Ares, an equity intelligence agent specialising in the second and third order market effects of the SpaceX IPO.

Your task is to score news signals for their relevance to the SpaceX IPO read-through thesis.

Score each signal on exactly these four dimensions:

relevance_score (integer 1-10):
  Does this signal genuinely concern this specific company?
  10 = directly about this company's core business
  1  = tangential mention or unrelated

narrative_score (integer 1-10):
  How strongly does this signal relate to the SpaceX IPO re-rating thesis?
  10 = direct SpaceX comparison, sector re-rating language, IPO benchmark discussion
  7-9 = clear adjacency, institutional capital flows, sector positioning
  4-6 = indirect connection, supply chain, macro space tailwind
  1-3 = tangential or no connection to SpaceX thesis

sentiment (string):
  One of: Strongly Positive, Positive, Neutral, Negative, Strongly Negative

valuation_flag (string):
  Given the P/S ratio vs sector context provided, classify as one of:
  undervalued    = P/S significantly below peers AND narrative_score >= 6
  fairly_valued  = P/S in line with peers
  overvalued     = P/S significantly above peers
  insufficient_data = no financial data available to assess

implication (string):
  One sentence only. What does this signal mean for the SpaceX IPO thesis and this equity's positioning?
  Be specific. Avoid generic statements.

Return ONLY valid JSON with these exact keys. No markdown fences. No preamble."""


def build_user_prompt(signal: dict, equity: dict, snapshot: Optional[dict]) -> str:
    """Build the triage prompt for a single signal."""

    # Valuation context
    if snapshot:
        ps_ratio    = snapshot.get("ps_ratio")
        revenue_ttm = snapshot.get("revenue_ttm")
        price       = snapshot.get("price")
        val_context = (
            f"Current price: ${price}\n"
            f"Revenue TTM: ${revenue_ttm:,}\n"
            f"P/S ratio: {ps_ratio if ps_ratio else 'not available'}"
        ) if revenue_ttm else "Financial data: not available"
    else:
        val_context = "Financial data: not available"

    return f"""Company: {equity['name']} ({equity['ticker']})
Sector: {equity['sector_name']} (Order {equity['order_level']} effect)
Thesis: {equity['thesis_note']}

{val_context}

Signal title: {signal['title']}
Signal source: {signal['source'] or 'unknown'}

Score this signal for the Trygg Ares SpaceX IPO thesis."""


def parse_triage_response(response_text: str) -> Optional[dict]:
    """Parse Claude's JSON response, stripping any markdown fences."""
    text = response_text.strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e} — response was: {text[:200]}")
        return None


async def triage_signal(
    client: anthropic.Anthropic,
    signal: dict,
    equity: dict,
    snapshot: Optional[dict],
) -> Optional[dict]:
    """Score a single signal with Claude Haiku. Returns parsed scores or None."""
    try:
        response = client.messages.create(
            model=TRIAGE_MODEL,
            max_tokens=300,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},  # Prompt caching
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": build_user_prompt(signal, equity, snapshot),
                }
            ],
        )
        return parse_triage_response(response.content[0].text)
    except Exception as e:
        logger.error(f"Triage API error for signal {signal['id']}: {e}")
        return None


async def run_triage():
    """Main entry point — triage all unscored signals."""
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set — aborting")
        return

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Fetch untriaged signals with equity and snapshot context
    async with pool.acquire() as conn:
        signals = await conn.fetch("""
            SELECT
                sig.id,
                sig.title,
                sig.url,
                sig.source,
                sig.equity_id,
                e.name        AS equity_name,
                e.ticker,
                e.thesis_note,
                s.name        AS sector_name,
                s.order_level,
                snap.price,
                snap.ps_ratio,
                snap.revenue_ttm
            FROM ares.signals sig
            JOIN ares.equities e  ON e.id  = sig.equity_id
            JOIN ares.sectors  s  ON s.id  = e.sector_id
            LEFT JOIN ares.equity_snapshots snap
                ON snap.equity_id = sig.equity_id
                AND snap.snapshot_date = CURRENT_DATE
            WHERE sig.relevance_score IS NULL
            ORDER BY sig.created_at DESC
            LIMIT 50
        """)

    if not signals:
        logger.info("No untriaged signals found")
        await pool.close()
        return

    logger.info(f"Triaging {len(signals)} signals with {TRIAGE_MODEL}...")

    scored   = 0
    failed   = 0
    high_scores = []

    for signal in signals:
        sig_dict    = dict(signal)
        equity_dict = {
            "name":        sig_dict["equity_name"],
            "ticker":      sig_dict["ticker"],
            "thesis_note": sig_dict["thesis_note"],
            "sector_name": sig_dict["sector_name"],
            "order_level": sig_dict["order_level"],
        }
        snapshot_dict = {
            "price":       sig_dict["price"],
            "ps_ratio":    sig_dict["ps_ratio"],
            "revenue_ttm": sig_dict["revenue_ttm"],
        } if sig_dict["price"] else None

        scores = await triage_signal(client, sig_dict, equity_dict, snapshot_dict)

        if scores:
            relevance_score = scores.get("relevance_score")
            narrative_score = scores.get("narrative_score")
            sentiment       = scores.get("sentiment")
            valuation_flag  = scores.get("valuation_flag")
            implication     = scores.get("implication")

            # Validate valuation_flag
            valid_flags = {"undervalued", "fairly_valued", "overvalued", "insufficient_data"}
            if valuation_flag not in valid_flags:
                valuation_flag = "insufficient_data"

            async with pool.acquire() as conn:
                await conn.execute("""
                    UPDATE ares.signals SET
                        relevance_score = $1,
                        narrative_score = $2,
                        sentiment       = $3,
                        valuation_flag  = $4,
                        implication     = $5,
                        triaged_at      = NOW()
                    WHERE id = $6
                """,
                    relevance_score,
                    narrative_score,
                    sentiment,
                    valuation_flag,
                    implication,
                    sig_dict["id"],
                )

            scored += 1

            # Track high-scoring signals for immediate alert consideration
            if narrative_score and narrative_score >= 9:
                high_scores.append({
                    "ticker":     sig_dict["ticker"],
                    "title":      sig_dict["title"],
                    "narrative":  narrative_score,
                    "relevance":  relevance_score,
                    "implication": implication,
                })

            logger.info(
                f"  {sig_dict['ticker']}: rel={relevance_score}, "
                f"narr={narrative_score}, {sentiment}, {valuation_flag}"
            )
        else:
            failed += 1

        # Small delay to avoid rate limiting
        await asyncio.sleep(0.3)

    # Log any landmark signals
    if high_scores:
        logger.warning(f"HIGH NARRATIVE SIGNALS (>=9): {len(high_scores)}")
        for hs in high_scores:
            logger.warning(
                f"  *** {hs['ticker']} narr={hs['narrative']} — {hs['title'][:80]}"
            )

    await pool.close()
    logger.info(f"Triage complete — {scored} scored, {failed} failed")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(run_triage())
