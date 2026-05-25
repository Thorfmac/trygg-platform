"""
Trygg Ares — Trade Proposal Engine
Generates trade proposals from today's signals at 13:45 UK (45 mins before US open).
Sends structured Telegram approval requests.
Executes approved trades at market open via order_manager.py.
Manages the 15-minute execution window.

Schedule: 13:45 UTC (14:45 UK summer) daily — proposal generation
          14:25 UTC — deadline for approval (5 mins before open)
          14:30 UTC — US market open, execute approved trades
          14:45 UTC — close execution window
"""

import asyncio
import json
import logging
import os
from datetime import date, datetime, timezone, timedelta
from typing import Optional

import aiohttp
import asyncpg
from dotenv import load_dotenv

from ares.execution.position_sizer import calculate_quantity, validate_position
from ares.execution.order_manager import OrderExecutor

load_dotenv()

logger = logging.getLogger(__name__)

DATABASE_URL       = os.getenv("DATABASE_URL")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_OWNER_CHAT_ID")
ACCOUNT_ID         = os.getenv("IBKR_ACCOUNT_ID", "")

# GBP/USD rate — updated at runtime from snapshot data
# Fallback if FX data unavailable
FALLBACK_GBP_USD = 1.27

# Minimum scores to qualify for trade proposal
MIN_NARRATIVE_SCORE  = 7
MIN_RELEVANCE_SCORE  = 7
MIN_VALUATION_FLAGS  = {"undervalued", "fairly_valued"}

# Execution window
EXECUTION_WINDOW_MINUTES = 15

# Pending approvals — stored in DB between proposal and execution
PROPOSALS_TABLE = "ares.trade_proposals"


async def ensure_proposals_table(pool: asyncpg.Pool):
    """Create trade proposals table if it doesn't exist."""
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ares.trade_proposals (
                id              SERIAL PRIMARY KEY,
                proposal_date   DATE NOT NULL DEFAULT CURRENT_DATE,
                ticker          TEXT NOT NULL,
                action          TEXT NOT NULL DEFAULT 'BUY',
                quantity        INTEGER NOT NULL,
                price_usd       NUMERIC(12,4),
                position_gbp    NUMERIC(10,2),
                narrative_score INTEGER,
                relevance_score INTEGER,
                valuation_flag  TEXT,
                rationale       TEXT,
                status          TEXT NOT NULL DEFAULT 'pending',
                approved_at     TIMESTAMPTZ,
                rejected_at     TIMESTAMPTZ,
                executed_at     TIMESTAMPTZ,
                order_id        INTEGER,
                fill_price      NUMERIC(12,4),
                fill_quantity   INTEGER,
                created_at      TIMESTAMPTZ DEFAULT NOW()
            )
        """)


async def get_top_signals(pool: asyncpg.Pool) -> list[dict]:
    """
    Fetch today's top signals qualifying for trade proposal.
    One signal per equity — highest narrative score wins.
    """
    async with pool.acquire() as conn:
        signals = await conn.fetch("""
            SELECT DISTINCT ON (e.ticker)
                e.ticker,
                e.name AS equity_name,
                sig.narrative_score,
                sig.relevance_score,
                sig.sentiment,
                sig.valuation_flag,
                sig.implication,
                snap.price AS price_usd,
                snap.ps_ratio,
                snap.revenue_ttm,
                s.order_level,
                s.name AS sector_name
            FROM ares.signals sig
            JOIN ares.equities e   ON e.id  = sig.equity_id
            JOIN ares.sectors  s   ON s.id  = e.sector_id
            LEFT JOIN ares.equity_snapshots snap
                ON snap.equity_id    = sig.equity_id
                AND snap.snapshot_date = CURRENT_DATE
            WHERE sig.narrative_score  >= $1
              AND sig.relevance_score  >= $2
              AND sig.valuation_flag    = ANY($3::text[])
              AND sig.created_at::date  = CURRENT_DATE
              AND e.is_active           = TRUE
              AND snap.price            IS NOT NULL
              AND e.ticker             != 'MYNA'  -- exclude micro-cap
              AND e.ticker             != 'SPIR'  -- exclude thin liquidity
            ORDER BY e.ticker, sig.narrative_score DESC
        """,
            MIN_NARRATIVE_SCORE,
            MIN_RELEVANCE_SCORE,
            list(MIN_VALUATION_FLAGS),
        )

    return [dict(s) for s in signals]


def build_proposal_message(proposals: list[dict]) -> str:
    """Build the Telegram approval request message."""
    now_uk = datetime.now(timezone.utc) + timedelta(hours=1)
    lines = [
        "TRYGG ARES — TRADE PROPOSALS",
        f"{now_uk.strftime('%d %b %Y %H:%M')} UK | US Open in ~45 minutes",
        "",
        "Reply /ares_approve <ID> to approve a trade.",
        "Reply /ares_approve_all to approve all.",
        "Reply /ares_reject <ID> to reject.",
        "No reply = no trades executed.",
        "─────────────────────────────",
        "",
    ]

    for p in proposals:
        lines += [
            f"PROPOSAL #{p['id']} — {p['ticker']}",
            f"Action: BUY {p['quantity']} shares @ ~${float(p['price_usd']):.2f}",
            f"Position value: £{float(p['position_gbp']):.2f}",
            f"Narrative: {p['narrative_score']}/10 | Relevance: {p['relevance_score']}/10",
            f"Valuation: {p['valuation_flag']} | Sentiment: {p.get('sentiment', 'n/a')}",
            f"Rationale: {p['rationale']}",
            "",
        ]

    lines += [
        "─────────────────────────────",
        f"Deadline: {(now_uk + timedelta(minutes=40)).strftime('%H:%M')} UK",
        "Unapproved proposals will not execute.",
    ]

    return "\n".join(lines)


async def send_telegram(text: str) -> bool:
    """Send Telegram message."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]

    async with aiohttp.ClientSession() as session:
        for chunk in chunks:
            try:
                async with session.post(url, json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text":    chunk,
                }, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(f"Telegram error {resp.status}: {body[:200]}")
                        return False
            except Exception as e:
                logger.error(f"Telegram send error: {e}")
                return False
    return True


async def generate_proposals(pool: asyncpg.Pool) -> list[dict]:
    """Generate and store trade proposals from today's signals."""
    await ensure_proposals_table(pool)

    # Check if proposals already generated today
    async with pool.acquire() as conn:
        existing = await conn.fetchval("""
            SELECT COUNT(*) FROM ares.trade_proposals
            WHERE proposal_date = CURRENT_DATE
        """)

    if existing > 0:
        logger.info(f"Proposals already generated today ({existing} existing)")
        # Return pending proposals
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM ares.trade_proposals
                WHERE proposal_date = CURRENT_DATE AND status = 'pending'
            """)
        return [dict(r) for r in rows]

    signals = await get_top_signals(pool)

    if not signals:
        logger.info("No signals qualify for trade proposals today")
        return []

    proposals = []
    for sig in signals:
        quantity, pos_usd, pos_gbp = calculate_quantity(
            price_usd=float(sig["price_usd"]),
            gbp_usd_rate=FALLBACK_GBP_USD,
        )

        if quantity <= 0:
            continue

        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO ares.trade_proposals (
                    ticker, action, quantity, price_usd, position_gbp,
                    narrative_score, relevance_score, valuation_flag,
                    rationale, status
                ) VALUES ($1,'BUY',$2,$3,$4,$5,$6,$7,$8,'pending')
                RETURNING *
            """,
                sig["ticker"],
                quantity,
                float(sig["price_usd"]),
                round(pos_gbp, 2),
                sig["narrative_score"],
                sig["relevance_score"],
                sig["valuation_flag"],
                sig["implication"] or f"Ares signal: narr={sig['narrative_score']}/10",
            )
            proposals.append(dict(row))

        logger.info(
            f"Proposal created: BUY {quantity} {sig['ticker']} "
            f"@ ~${sig['price_usd']:.2f} (£{pos_gbp:.2f})"
        )

    return proposals


async def execute_approved_trades(pool: asyncpg.Pool) -> list[dict]:
    """
    Execute all approved proposals via TWS.
    Called at 14:30 UTC (US market open).
    """
    async with pool.acquire() as conn:
        approved = await conn.fetch("""
            SELECT * FROM ares.trade_proposals
            WHERE proposal_date = CURRENT_DATE
              AND status = 'approved'
        """)

    if not approved:
        logger.info("No approved proposals to execute")
        return []

    logger.info(f"Executing {len(approved)} approved trade(s)...")

    executor = OrderExecutor()
    if not executor.connect():
        logger.error("Cannot connect to TWS — aborting execution")
        await send_telegram(
            "ARES EXECUTION FAILED\n\n"
            "Cannot connect to TWS. Check TWS is running on the Geekom."
        )
        return []

    results = []
    execution_start = datetime.now(timezone.utc)

    try:
        for proposal in approved:
            prop = dict(proposal)
            ticker   = prop["ticker"]
            quantity = prop["quantity"]

            logger.info(f"Executing: BUY {quantity} {ticker}")

            order_id = executor.submit_order(
                ticker=ticker,
                action="BUY",
                quantity=quantity,
                order_type="MKT",
            )

            if not order_id:
                logger.error(f"Order submission failed for {ticker}")
                async with pool.acquire() as conn:
                    await conn.execute("""
                        UPDATE ares.trade_proposals
                        SET status = 'failed'
                        WHERE id = $1
                    """, prop["id"])
                continue

            # Wait for fill (up to 30 seconds per order)
            fill_status = executor.wait_for_fill(order_id, timeout=30)

            fill_price    = fill_status.get("avg_fill_price", 0)
            fill_quantity = fill_status.get("filled", 0)
            status        = "executed" if fill_quantity > 0 else "submitted"

            async with pool.acquire() as conn:
                await conn.execute("""
                    UPDATE ares.trade_proposals SET
                        status        = $1,
                        executed_at   = NOW(),
                        order_id      = $2,
                        fill_price    = $3,
                        fill_quantity = $4
                    WHERE id = $5
                """,
                    status,
                    order_id,
                    fill_price or None,
                    fill_quantity or None,
                    prop["id"],
                )

            result = {
                "ticker":         ticker,
                "quantity":       quantity,
                "order_id":       order_id,
                "fill_price":     fill_price,
                "fill_quantity":  fill_quantity,
                "status":         status,
            }
            results.append(result)

            logger.info(
                f"  {ticker}: order_id={order_id}, "
                f"filled={fill_quantity}@${fill_price}"
            )

            # Small delay between orders
            await asyncio.sleep(2)

    finally:
        executor.disconnect()

    # Send execution summary to Telegram
    if results:
        summary_lines = ["ARES EXECUTION SUMMARY", ""]
        for r in results:
            fill_str = (
                f"{r['fill_quantity']} shares @ ${r['fill_price']:.2f}"
                if r["fill_price"] else "pending fill"
            )
            summary_lines.append(
                f"{r['ticker']}: BUY {r['quantity']} — {fill_str} "
                f"[order #{r['order_id']}]"
            )
        summary_lines += [
            "",
            f"Execution window: {EXECUTION_WINDOW_MINUTES} minutes from open",
            "Use /ares_positions to monitor.",
        ]
        await send_telegram("\n".join(summary_lines))

    return results


async def run_trade_proposal():
    """
    Main entry point — generate proposals and send to Telegram.
    Called at 13:45 UTC daily.
    """
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)

    proposals = await generate_proposals(pool)

    if not proposals:
        await send_telegram(
            "ARES TRADE PROPOSALS\n\n"
            "No qualifying signals for trade today.\n"
            "Minimum: narrative ≥7, relevance ≥7, valuation = undervalued or fairly_valued."
        )
        await pool.close()
        return

    message = build_proposal_message(proposals)
    await send_telegram(message)

    await pool.close()
    logger.info(f"Trade proposals sent — {len(proposals)} proposals pending approval")


async def run_execution():
    """
    Main entry point — execute approved trades.
    Called at 14:30 UTC (US market open).
    """
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)
    results = await execute_approved_trades(pool)
    await pool.close()
    logger.info(f"Execution complete — {len(results)} orders submitted")


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    mode = sys.argv[1] if len(sys.argv) > 1 else "propose"
    if mode == "execute":
        asyncio.run(run_execution())
    else:
        asyncio.run(run_trade_proposal())
