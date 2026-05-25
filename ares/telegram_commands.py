"""
Trygg Ares — Telegram Command Handlers
Handles /ares_* commands via Saffie (@thorfinn_hermes_bot).
Plugs into the existing mimir/telegram_handler.py dispatch loop.
"""

import asyncio
import logging
import os
from datetime import date, timedelta

import asyncpg
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")


async def get_db_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)


# ------------------------------------------------------------
# /ares — Today's digest on demand
# ------------------------------------------------------------
async def cmd_ares(pool: asyncpg.Pool) -> str:
    async with pool.acquire() as conn:
        digest = await conn.fetchrow("""
            SELECT content, signal_count, top_score, created_at
            FROM ares.digests
            WHERE digest_date = $1
            ORDER BY created_at DESC
            LIMIT 1
        """, date.today())

    if not digest:
        # Check yesterday
        async with pool.acquire() as conn:
            digest = await conn.fetchrow("""
                SELECT content, signal_count, top_score, created_at, digest_date
                FROM ares.digests
                ORDER BY digest_date DESC
                LIMIT 1
            """)
        if not digest:
            return "No Ares digest available yet. Run /ares_status to check signal pipeline."
        return f"[Most recent digest — {digest['digest_date']}]\n\n{digest['content']}"

    return digest["content"]


# ------------------------------------------------------------
# /ares_status <ticker> — Latest signals + snapshot for one equity
# ------------------------------------------------------------
async def cmd_ares_status(pool: asyncpg.Pool, ticker: str) -> str:
    ticker = ticker.upper().strip()

    async with pool.acquire() as conn:
        equity = await conn.fetchrow("""
            SELECT e.id, e.name, e.ticker, e.thesis_note,
                   s.name AS sector_name, s.order_level
            FROM ares.equities e
            JOIN ares.sectors s ON s.id = e.sector_id
            WHERE UPPER(e.ticker) = $1 AND e.is_active = TRUE
        """, ticker)

    if not equity:
        return f"Ticker {ticker} not found in Ares watchlist. Use /ares_watchlist to see all tracked equities."

    async with pool.acquire() as conn:
        snapshot = await conn.fetchrow("""
            SELECT price, market_cap, ps_ratio, revenue_ttm,
                   yoy_growth_pct, snapshot_date
            FROM ares.equity_snapshots
            WHERE equity_id = $1
            ORDER BY snapshot_date DESC
            LIMIT 1
        """, equity["id"])

        signals = await conn.fetch("""
            SELECT title, relevance_score, narrative_score,
                   sentiment, valuation_flag, implication, created_at
            FROM ares.signals
            WHERE equity_id = $1
              AND relevance_score IS NOT NULL
            ORDER BY narrative_score DESC, created_at DESC
            LIMIT 5
        """, equity["id"])

    lines = [
        f"ARES STATUS — {equity['name']} ({ticker})",
        f"Sector: {equity['sector_name']} | Order {equity['order_level']} effect",
        f"Thesis: {equity['thesis_note']}",
        "",
    ]

    if snapshot:
        rev = f"${snapshot['revenue_ttm']:,}" if snapshot["revenue_ttm"] else "n/a"
        ps  = f"{snapshot['ps_ratio']:.2f}x" if snapshot["ps_ratio"] else "n/a"
        lines += [
            f"Price: ${snapshot['price']} | Rev TTM: {rev} | P/S: {ps}",
            f"Snapshot: {snapshot['snapshot_date']}",
            "",
        ]
    else:
        lines += ["Financial data: not yet available", ""]

    if signals:
        lines.append(f"TOP SIGNALS ({len(signals)}):")
        for sig in signals:
            lines.append(
                f"• [{sig['narrative_score']}/10 narr | {sig['sentiment']}] "
                f"{sig['title'][:70]}"
            )
            if sig["implication"]:
                lines.append(f"  → {sig['implication']}")
    else:
        lines.append("No triaged signals yet.")

    return "\n".join(lines)


# ------------------------------------------------------------
# /ares_radar — Undervaluation radar
# ------------------------------------------------------------
async def cmd_ares_radar(pool: asyncpg.Pool) -> str:
    async with pool.acquire() as conn:
        undervalued = await conn.fetch("""
            SELECT DISTINCT ON (e.ticker)
                e.name, e.ticker, s.name AS sector_name, s.order_level,
                sig.narrative_score, sig.implication,
                snap.price, snap.ps_ratio, snap.revenue_ttm
            FROM ares.signals sig
            JOIN ares.equities e ON e.id = sig.equity_id
            JOIN ares.sectors  s ON s.id = e.sector_id
            LEFT JOIN ares.equity_snapshots snap
                ON snap.equity_id = sig.equity_id
                AND snap.snapshot_date = CURRENT_DATE
            WHERE sig.valuation_flag = 'undervalued'
              AND sig.narrative_score >= 6
            ORDER BY e.ticker, sig.narrative_score DESC
        """)

    if not undervalued:
        return (
            "ARES UNDERVALUATION RADAR\n\n"
            "No equities currently flagged as undervalued.\n"
            "Flags appear when P/S is below peers AND narrative score >= 6."
        )

    lines = ["ARES UNDERVALUATION RADAR", ""]
    for eq in undervalued:
        rev = f"${eq['revenue_ttm']:,}" if eq["revenue_ttm"] else "n/a"
        ps  = f"{eq['ps_ratio']:.2f}x" if eq["ps_ratio"] else "n/a"
        lines += [
            f"{eq['name']} ({eq['ticker']}) — Order {eq['order_level']}",
            f"Price: ${eq['price']} | Rev TTM: {rev} | P/S: {ps}",
            f"Narrative score: {eq['narrative_score']}/10",
            f"→ {eq['implication']}",
            "",
        ]

    return "\n".join(lines)


# ------------------------------------------------------------
# /ares_watchlist — Full active watchlist with current flags
# ------------------------------------------------------------
async def cmd_ares_watchlist(pool: asyncpg.Pool) -> str:
    async with pool.acquire() as conn:
        equities = await conn.fetch("""
            SELECT e.name, e.ticker, e.is_public,
                   s.name AS sector_name, s.order_level,
                   snap.price, snap.ps_ratio,
                   (
                       SELECT valuation_flag FROM ares.signals
                       WHERE equity_id = e.id AND valuation_flag IS NOT NULL
                       ORDER BY created_at DESC LIMIT 1
                   ) AS latest_flag
            FROM ares.equities e
            JOIN ares.sectors s ON s.id = e.sector_id
            LEFT JOIN ares.equity_snapshots snap
                ON snap.equity_id = e.id
                AND snap.snapshot_date = CURRENT_DATE
            WHERE e.is_active = TRUE
            ORDER BY s.order_level, s.name, e.name
        """)

    lines = ["ARES WATCHLIST", f"Updated: {date.today()}", ""]
    current_order = None

    for eq in equities:
        if eq["order_level"] != current_order:
            current_order = eq["order_level"]
            order_label = {1: "FIRST ORDER", 2: "SECOND ORDER", 3: "THIRD ORDER"}.get(current_order, "")
            lines += [f"── {order_label} ──", ""]

        price = f"${eq['price']}" if eq["price"] else "n/a"
        flag  = eq["latest_flag"] or "unscored"
        pub   = "(public)" if eq["is_public"] else "(private)"
        lines.append(f"{eq['name']} ({eq['ticker']}) {pub} — {price} — {flag}")

    return "\n".join(lines)


# ------------------------------------------------------------
# /ares_candidates — Unreviewed dynamic candidates
# ------------------------------------------------------------
async def cmd_ares_candidates(pool: asyncpg.Pool) -> str:
    async with pool.acquire() as conn:
        candidates = await conn.fetch("""
            SELECT name, ticker, mention_context, confidence, flagged_at
            FROM ares.candidates
            WHERE reviewed = FALSE
            ORDER BY confidence DESC, flagged_at DESC
            LIMIT 10
        """)

    if not candidates:
        return "ARES CANDIDATES\n\nNo unreviewed candidates at this time."

    lines = [f"ARES CANDIDATES ({len(candidates)} unreviewed)", ""]
    for c in candidates:
        ticker_str = f" ({c['ticker']})" if c["ticker"] else ""
        lines += [
            f"{c['name']}{ticker_str} — confidence {c['confidence']}/10",
            f"  {c['mention_context'][:120]}",
            f"  Flagged: {c['flagged_at'].strftime('%d %b %Y')}",
            "",
        ]

    return "\n".join(lines)


# ------------------------------------------------------------
# Dispatch — called from telegram_handler.py
# ------------------------------------------------------------
async def handle_ares_command(command: str, args: str, pool: asyncpg.Pool) -> str:
    """
    Main dispatch for /ares_* commands.
    Call this from your existing telegram_handler.py command router.

    Example integration:
        if text.startswith("/ares"):
            response = await handle_ares_command(text, args, pool)
            await send_message(chat_id, response)
    """
    cmd = command.lower().strip()

    if cmd == "/ares":
        return await cmd_ares(pool)
    elif cmd == "/ares_status":
        if not args:
            return "Usage: /ares_status <TICKER> (e.g. /ares_status RKLB)"
        return await cmd_ares_status(pool, args)
    elif cmd == "/ares_radar":
        return await cmd_ares_radar(pool)
    elif cmd == "/ares_watchlist":
        return await cmd_ares_watchlist(pool)
    elif cmd == "/ares_candidates":
        return await cmd_ares_candidates(pool)
    elif cmd == "/ares_thesis":
        return (
            "Opus 4.7 deep analysis not yet scheduled — "
            "Phase 4 will enable this. "
            "Use /ares for today's Sonnet digest."
        )
    else:
        return (
            "Ares commands:\n"
            "/ares — today's briefing\n"
            "/ares_status <TICKER> — equity detail\n"
            "/ares_radar — undervaluation flags\n"
            "/ares_watchlist — full watchlist\n"
            "/ares_candidates — watchlist candidates\n"
            "/ares_thesis — deep analysis (Phase 4)"
        )


# ------------------------------------------------------------
# /ares_approve <id> — Approve a trade proposal
# /ares_approve_all  — Approve all pending proposals
# /ares_reject <id>  — Reject a trade proposal
# /ares_positions    — Show current open positions
# /ares_halt         — Emergency halt — cancel all pending proposals
# ------------------------------------------------------------

async def cmd_ares_approve(pool: asyncpg.Pool, proposal_id: str) -> str:
    """Approve a specific trade proposal."""
    try:
        pid = int(proposal_id.strip())
    except ValueError:
        return f"Invalid proposal ID: {proposal_id}. Use /ares_approve <number>"

    async with pool.acquire() as conn:
        proposal = await conn.fetchrow("""
            UPDATE ares.trade_proposals
            SET status = 'approved', approved_at = NOW()
            WHERE id = $1 AND status = 'pending' AND proposal_date = CURRENT_DATE
            RETURNING ticker, quantity, price_usd, position_gbp
        """, pid)

    if not proposal:
        return f"Proposal #{pid} not found or already actioned."

    return (
        f"APPROVED — Proposal #{pid}\n"
        f"BUY {proposal['quantity']} {proposal['ticker']} "
        f"@ ~${proposal['price_usd']:.2f} (£{proposal['position_gbp']:.2f})\n"
        f"Will execute at US market open (14:30 UTC)."
    )


async def cmd_ares_approve_all(pool: asyncpg.Pool) -> str:
    """Approve all pending proposals for today."""
    async with pool.acquire() as conn:
        count = await conn.fetchval("""
            WITH updated AS (
                UPDATE ares.trade_proposals
                SET status = 'approved', approved_at = NOW()
                WHERE status = 'pending' AND proposal_date = CURRENT_DATE
                RETURNING id
            ) SELECT COUNT(*) FROM updated
        """)

    if count == 0:
        return "No pending proposals to approve today."

    return (
        f"ALL APPROVED — {count} proposal(s) approved.\n"
        f"Will execute at US market open (14:30 UTC)."
    )


async def cmd_ares_reject(pool: asyncpg.Pool, proposal_id: str) -> str:
    """Reject a specific trade proposal."""
    try:
        pid = int(proposal_id.strip())
    except ValueError:
        return f"Invalid proposal ID: {proposal_id}. Use /ares_reject <number>"

    async with pool.acquire() as conn:
        proposal = await conn.fetchrow("""
            UPDATE ares.trade_proposals
            SET status = 'rejected', rejected_at = NOW()
            WHERE id = $1 AND status = 'pending' AND proposal_date = CURRENT_DATE
            RETURNING ticker, quantity
        """, pid)

    if not proposal:
        return f"Proposal #{pid} not found or already actioned."

    return f"REJECTED — Proposal #{pid}: {proposal['quantity']} {proposal['ticker']}"


async def cmd_ares_halt(pool: asyncpg.Pool) -> str:
    """Emergency halt — reject all pending proposals."""
    async with pool.acquire() as conn:
        count = await conn.fetchval("""
            WITH updated AS (
                UPDATE ares.trade_proposals
                SET status = 'rejected', rejected_at = NOW()
                WHERE status IN ('pending', 'approved')
                  AND proposal_date = CURRENT_DATE
                RETURNING id
            ) SELECT COUNT(*) FROM updated
        """)

    return (
        f"HALT EXECUTED — {count} proposal(s) cancelled.\n"
        "No trades will execute today."
    )


async def cmd_ares_positions(pool: asyncpg.Pool) -> str:
    """Show today's executed trades."""
    async with pool.acquire() as conn:
        trades = await conn.fetch("""
            SELECT ticker, quantity, fill_price, fill_quantity,
                   position_gbp, status, executed_at, order_id
            FROM ares.trade_proposals
            WHERE proposal_date = CURRENT_DATE
              AND status IN ('executed', 'submitted', 'approved', 'failed')
            ORDER BY executed_at DESC NULLS LAST
        """)

    if not trades:
        return "ARES POSITIONS\n\nNo trades executed today."

    lines = [f"ARES POSITIONS — {date.today()}", ""]
    for t in trades:
        fill_str = (
            f"{t['fill_quantity']} @ ${t['fill_price']:.2f}"
            if t["fill_price"] else "pending"
        )
        lines.append(
            f"{t['ticker']}: {t['status'].upper()} — "
            f"{fill_str} [#{t['order_id']}]"
        )

    return "\n".join(lines)
