"""
Trygg Ares — Deep Analysis Agent
Weekly thesis synthesis using Claude Opus 4.7.
Runs Sundays at 06:00 UTC, or on-demand via /ares_thesis Telegram command.
Also fires automatically when any signal scores narrative_score = 10.

Stores results in ares.thesis_analyses.
Delivers via Telegram.
"""

import asyncio
import logging
import os
from datetime import date, timedelta

import anthropic
import aiohttp
import asyncpg
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DATABASE_URL       = os.getenv("DATABASE_URL")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_OWNER_CHAT_ID")

DEEP_ANALYSIS_MODEL = "claude-opus-4-7"


SYSTEM_PROMPT = """You are Trygg Ares Deep Analysis, the highest-capability intelligence layer of the Trygg platform.

You produce weekly thesis reviews for a sophisticated private investor tracking the second and third order market effects of the SpaceX IPO. Your analysis must be:

- Causally precise: trace specific signal chains, not generic sector commentary
- Contrarian where warranted: flag where market consensus may be wrong
- Actionable: every section should inform a specific investment decision
- Concise: quality over length. No padding.

You have access to one week of signals, daily snapshots, and the current thesis state. Produce your analysis in exactly this structure:

TRYGG ARES — DEEP ANALYSIS
[Date] | Weekly Thesis Review | Claude Opus 4.7

1. THESIS STATUS
Has the SpaceX IPO re-rating narrative strengthened, weakened, or shifted this week?
Cite specific signals. State your confidence level (High/Medium/Low) and why.

2. SECTOR MAP
For each order level, which equities are best positioned? Which are mispriced?
Be specific about the mispricing mechanism — is it valuation, narrative lag, or execution risk?

3. UNDERVALUATION TARGETS
Which public equities show the clearest gap between current narrative score and market valuation?
Rank your top 3 with specific rationale. If none are compelling, say so directly.

4. THIRD-ORDER WATCH
Any emerging capital rotation signals? ETF flows, institutional positioning changes,
UK/EU listed space adjacency moves, sovereign wealth activity?

5. THESIS RISKS
What could invalidate the SpaceX IPO read-through thesis?
For each risk, assign: probability (Low/Medium/High) and impact (Low/Medium/High).

6. WATCHLIST CHANGES
Based on this week's signal flow, recommend any additions or removals from the active watchlist.
Be specific about why.

Use plain text only. No markdown. Be direct."""


def build_analysis_prompt(
    signals: list[dict],
    snapshots: list[dict],
    trigger_event: str,
    analysis_type: str,
) -> str:
    """Build the deep analysis prompt from this week's data."""

    # Format signals by equity
    by_equity = {}
    for sig in signals:
        ticker = sig["ticker"]
        if ticker not in by_equity:
            by_equity[ticker] = []
        by_equity[ticker].append(sig)

    signal_lines = []
    for ticker, sigs in sorted(by_equity.items()):
        top = max(sigs, key=lambda x: x["narrative_score"] or 0)
        signal_lines.append(
            f"\n{ticker} — {len(sigs)} signals this week | "
            f"Top narrative: {top['narrative_score']}/10 | "
            f"Top relevance: {top['relevance_score']}/10"
        )
        for sig in sorted(sigs, key=lambda x: -(x["narrative_score"] or 0))[:3]:
            signal_lines.append(f"  [{sig['narrative_score']}/10] {sig['title']}")
            if sig["implication"]:
                signal_lines.append(f"  → {sig['implication']}")
            if sig["valuation_flag"] == "undervalued":
                signal_lines.append(f"  *** UNDERVALUED FLAG ***")

    # Format snapshots
    snapshot_lines = ["\nCURRENT FINANCIAL SNAPSHOTS:"]
    for snap in snapshots:
        rev = f"${snap['revenue_ttm']:,}" if snap["revenue_ttm"] else "n/a"
        ps  = f"{snap['ps_ratio']:.2f}x" if snap["ps_ratio"] else "n/a"
        snapshot_lines.append(
            f"  {snap['ticker']}: price=${snap['price']} | "
            f"rev_ttm={rev} | P/S={ps}"
        )

    today = date.today().strftime("%A, %d %B %Y")
    week_start = (date.today() - timedelta(days=7)).strftime("%d %b")

    return f"""Today: {today}
Analysis type: {analysis_type}
Trigger: {trigger_event}
Signal window: {week_start} to today

SIGNALS THIS WEEK:
{"".join(signal_lines)}

{"".join(snapshot_lines)}

Produce the Trygg Ares deep analysis."""


async def generate_deep_analysis(
    signals: list[dict],
    snapshots: list[dict],
    trigger_event: str,
    analysis_type: str,
) -> str | None:
    """Run Opus 4.7 deep analysis. Returns content or None on failure."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = build_analysis_prompt(signals, snapshots, trigger_event, analysis_type)

    try:
        logger.info(f"Running Opus 4.7 deep analysis ({analysis_type})...")
        response = client.messages.create(
            model=DEEP_ANALYSIS_MODEL,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    except Exception as e:
        logger.error(f"Opus deep analysis error: {e}")
        return None


async def send_telegram(text: str) -> bool:
    """Send message via Telegram, splitting at 4000 chars if needed."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Telegram credentials not set")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]

    async with aiohttp.ClientSession() as session:
        for i, chunk in enumerate(chunks):
            try:
                async with session.post(url, json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text":    chunk,
                }, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(f"Telegram error {resp.status}: {body[:200]}")
                        return False
                    logger.info(f"Telegram chunk {i+1}/{len(chunks)} sent")
            except Exception as e:
                logger.error(f"Telegram send error: {e}")
                return False
            if len(chunks) > 1:
                await asyncio.sleep(1)

    return True


async def run_deep_analysis(
    analysis_type: str = "weekly_review",
    trigger_event: str = "Scheduled weekly run",
):
    """
    Main entry point.
    analysis_type: 'weekly_review' | 'event_trigger' | 'manual'
    trigger_event: description of what triggered this run
    """
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set — aborting")
        return

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)

    # Fetch signals from last 7 days
    async with pool.acquire() as conn:
        signals = await conn.fetch("""
            SELECT
                sig.id, sig.title, sig.implication,
                sig.relevance_score, sig.narrative_score,
                sig.sentiment, sig.valuation_flag,
                e.name AS equity_name, e.ticker,
                s.name AS sector_name, s.order_level
            FROM ares.signals sig
            JOIN ares.equities e ON e.id = sig.equity_id
            JOIN ares.sectors  s ON s.id = e.sector_id
            WHERE sig.created_at >= NOW() - INTERVAL '7 days'
              AND sig.relevance_score IS NOT NULL
            ORDER BY sig.narrative_score DESC NULLS LAST
            LIMIT 100
        """)

        # Fetch latest snapshots for all active equities
        snapshots = await conn.fetch("""
            SELECT DISTINCT ON (e.ticker)
                e.ticker, snap.price, snap.ps_ratio,
                snap.revenue_ttm, snap.yoy_growth_pct,
                snap.snapshot_date
            FROM ares.equity_snapshots snap
            JOIN ares.equities e ON e.id = snap.equity_id
            WHERE e.is_active = TRUE
            ORDER BY e.ticker, snap.snapshot_date DESC
        """)

    if not signals:
        logger.warning("No signals in the last 7 days — skipping deep analysis")
        await pool.close()
        return

    signals_list   = [dict(s) for s in signals]
    snapshots_list = [dict(s) for s in snapshots]

    logger.info(
        f"Deep analysis: {len(signals_list)} signals, "
        f"{len(snapshots_list)} snapshots, trigger: {trigger_event}"
    )

    content = await generate_deep_analysis(
        signals_list, snapshots_list, trigger_event, analysis_type
    )

    if not content:
        logger.error("Deep analysis generation failed")
        await pool.close()
        return

    # Send to Telegram
    header = f"*** TRYGG ARES DEEP ANALYSIS ***\n\n"
    sent = await send_telegram(header + content)

    # Store in DB
    equities_covered = list({s["ticker"] for s in signals_list})
    sectors_covered  = list({s["sector_name"] for s in signals_list})

    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO ares.thesis_analyses (
                analysis_type, trigger_event, model,
                content, sectors_covered, equities_covered
            ) VALUES ($1,$2,$3,$4,$5,$6::text[])
        """,
            analysis_type,
            trigger_event,
            DEEP_ANALYSIS_MODEL,
            content,
            sectors_covered,
            equities_covered,
        )

    await pool.close()
    logger.info(
        f"Deep analysis complete — stored, "
        f"telegram={'sent' if sent else 'FAILED'}"
    )


async def check_and_fire_event_trigger(pool: asyncpg.Pool) -> None:
    """
    Called after each triage run.
    If any signal scores narrative_score = 10, fire an immediate Opus analysis.
    """
    async with pool.acquire() as conn:
        landmark = await conn.fetchrow("""
            SELECT sig.title, e.ticker
            FROM ares.signals sig
            JOIN ares.equities e ON e.id = sig.equity_id
            WHERE sig.narrative_score = 10
              AND sig.created_at >= NOW() - INTERVAL '1 hour'
            ORDER BY sig.created_at DESC
            LIMIT 1
        """)

    if landmark:
        trigger = f"Landmark signal: {landmark['ticker']} — {landmark['title'][:80]}"
        logger.warning(f"EVENT TRIGGER FIRED: {trigger}")
        await run_deep_analysis(
            analysis_type="event_trigger",
            trigger_event=trigger,
        )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    import sys
    # Allow manual trigger with custom message:
    # python ares/agents/deep_analysis.py "Custom trigger message"
    trigger = sys.argv[1] if len(sys.argv) > 1 else "Manual trigger"
    asyncio.run(run_deep_analysis(analysis_type="manual", trigger_event=trigger))
