# =============================================================
# mimir/telegram_handler.py
# =============================================================
# The Telegram command handler.
#
# What it does:
#   Listens for commands sent to the Trygg bot and responds
#   with live data from the database.
#
# Security model (single-user stage):
#   Only one Telegram chat ID is allowed to issue commands —
#   yours. Any message from any other chat is silently ignored.
#   This is set via TELEGRAM_OWNER_CHAT_ID in your .env file.
#
# Commands implemented:
#   /status <company>  — latest signals + IPO score for one company
#   /digest            — trigger an immediate digest right now
#   /watchlist         — list all monitored companies
#   /scores            — all companies ranked by IPO readiness
#   /alert on|off      — toggle whether score>=9 alerts are sent
#
# How it runs:
#   This runs as a separate long-lived process alongside the
#   scheduler. In docker-compose it's a second service that
#   shares the same database and .env file.
#
#   Start it with:
#     python -m mimir.telegram_handler
# =============================================================

import logging
import os
import sys

from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.constants import ParseMode

from core.config import load_config
from core.database import init_pool, get_conn

logger = logging.getLogger(__name__)


# ── Auth ─────────────────────────────────────────────────────

def _is_authorised(update: Update) -> bool:
    """
    Return True only if the message comes from your chat ID.
    Every command handler calls this first — if it returns False,
    the command is silently dropped.
    """
    owner_id = os.getenv("TELEGRAM_OWNER_CHAT_ID", "")
    if not owner_id:
        logger.warning("TELEGRAM_OWNER_CHAT_ID not set — all commands rejected")
        return False
    return str(update.effective_chat.id) == str(owner_id)


# ── Command handlers ─────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /start — shown when someone first messages the bot.
    For unauthorised users this is the only response they ever get.
    """
    if not _is_authorised(update):
        return  # silent reject

    await update.message.reply_text(
        "*Mímir is online* 🟢\n\n"
        "Commands:\n"
        "• `/status <company>` — latest signals and IPO score\n"
        "• `/scores` — all companies ranked by IPO readiness\n"
        "• `/watchlist` — full company list\n"
        "• `/digest` — trigger digest now\n"
        "• `/alert on|off` — toggle immediate alerts\n",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /status <company name or slug>
    Returns the latest signals and current IPO score for one company.

    Examples:
      /status pqshield
      /status PQShield
      /status isara
    """
    if not _is_authorised(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: `/status <company>`\nExample: `/status pqshield`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Join args so "/status post quantum" also works
    query = " ".join(context.args).strip().lower()

    company = _find_company(query)
    if not company:
        await update.message.reply_text(
            f"Company not found: `{query}`\n\nTry `/watchlist` to see available companies.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Get the 5 most recent scored signals
    signals = _get_recent_signals(company["id"], limit=5)

    # Get the most recent IPO score
    ipo = _get_latest_ipo_score(company["id"])

    # Build the response
    lines = [
        f"*{company['name']}*",
        f"_{company['sector']} · {company['country']} · {company['stage'].replace('_', ' ')}_",
        "",
    ]

    # IPO readiness section
    if ipo:
        bar = _score_bar(ipo["score"], 100)
        lines += [
            f"*IPO Readiness*",
            f"{bar} {ipo['score']}/100 — _{ipo['strength'].replace('_', ' ')}_",
        ]
        if ipo.get("rationale"):
            lines.append(f"_{ipo['rationale']}_")
        lines.append("")
    else:
        lines += ["*IPO Readiness*", "_Not yet scored_", ""]

    # Recent signals section
    if signals:
        lines.append("*Recent signals*")
        for sig in signals:
            score = sig.get("relevance_score")
            score_label = f"[{score}/10]" if score else "[unscored]"
            sentiment_mark = {
                "positive": "▲", "negative": "▼", "neutral": "–"
            }.get(sig.get("sentiment", "neutral"), "–")
            lines.append(f"• {sentiment_mark} {sig['headline'][:80]} {score_label}")
            if sig.get("investment_implication"):
                lines.append(f"  _{sig['investment_implication'][:120]}_")
    else:
        lines.append("_No recent signals_")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


async def cmd_scores(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /scores
    Lists all watchlist companies ranked by their current IPO readiness score.
    Companies without a score appear at the bottom.
    """
    if not _is_authorised(update):
        return

    companies = _get_all_companies_with_scores()

    if not companies:
        await update.message.reply_text("No companies found in watchlist.")
        return

    lines = ["*IPO Readiness Ranking*", ""]

    scored = [c for c in companies if c.get("ipo_score") is not None]
    unscored = [c for c in companies if c.get("ipo_score") is None]

    # Ranked companies
    for i, co in enumerate(scored, 1):
        bar = _score_bar(co["ipo_score"], 100, width=8)
        strength = (co.get("ipo_strength") or "").replace("_", " ")
        lines.append(
            f"{i}. *{co['name']}* {bar} {co['ipo_score']}/100"
            + (f" — _{strength}_" if strength else "")
        )

    # Unscored companies
    if unscored:
        if scored:
            lines.append("")
        lines.append("_Not yet scored:_")
        for co in unscored:
            lines.append(f"• {co['name']} ({co['stage'].replace('_', ' ')})")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /watchlist
    Lists all companies currently being monitored, grouped by stage.
    """
    if not _is_authorised(update):
        return

    companies = _get_all_active_companies()

    if not companies:
        await update.message.reply_text("No active companies in watchlist.")
        return

    # Group by stage
    by_stage = {}
    for co in companies:
        stage = co["stage"].replace("_", " ").title()
        by_stage.setdefault(stage, []).append(co)

    stage_order = [
        "Pre Ipo", "Private Growth", "Private Early", "Public", "Acquired"
    ]

    lines = [f"*Watchlist — {len(companies)} companies*", ""]

    for stage in stage_order:
        if stage not in by_stage:
            continue
        lines.append(f"*{stage}*")
        for co in by_stage[stage]:
            ticker = f" ({co['ticker']})" if co.get("ticker") else ""
            lines.append(f"• {co['name']}{ticker} — {co['country']}")
        lines.append("")

    await update.message.reply_text(
        "\n".join(lines).strip(),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /digest
    Triggers an immediate digest right now, regardless of schedule.
    Useful for checking in on demand or before a meeting.
    """
    if not _is_authorised(update):
        return

    await update.message.reply_text(
        "Generating digest… this takes 30–60 seconds ⏳",
        parse_mode=ParseMode.MARKDOWN,
    )

    try:
        # Import here to avoid circular imports at module level
        from mimir.agents import digest as digest_agent
        result = digest_agent.run(config)

        if result["digests_sent"] == 0:
            await update.message.reply_text(
                "_No signals above threshold in the last 24 hours — "
                "nothing to digest._",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            # The digest itself was already sent by digest_agent.run()
            # Just confirm it went out
            await update.message.reply_text(
                f"✓ Digest sent ({result['digests_sent']} briefing"
                f"{'s' if result['digests_sent'] != 1 else ''})",
                parse_mode=ParseMode.MARKDOWN,
            )
    except Exception as e:
        logger.error(f"On-demand digest failed: {e}")
        await update.message.reply_text(
            f"Digest failed: `{str(e)[:200]}`",
            parse_mode=ParseMode.MARKDOWN,
        )


async def cmd_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /alert on|off
    Toggle whether score>=9 signals trigger an immediate Telegram message.
    The preference is stored in the database against your client record.
    """
    if not _is_authorised(update):
        return

    if not context.args or context.args[0].lower() not in ("on", "off"):
        # Show current status if no argument given
        current = _get_alert_preference()
        status = "on 🟢" if current else "off 🔴"
        await update.message.reply_text(
            f"Immediate alerts are currently *{status}*\n\n"
            f"To change: `/alert on` or `/alert off`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    enabled = context.args[0].lower() == "on"
    _set_alert_preference(enabled)

    status = "on 🟢" if enabled else "off 🔴"
    await update.message.reply_text(
        f"Immediate alerts turned *{status}*",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch-all for unrecognised commands from authorised user."""
    if not _is_authorised(update):
        return
    await update.message.reply_text(
        "Unknown command. Try `/start` for the command list.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Database helpers ─────────────────────────────────────────

def _find_company(query: str) -> dict | None:
    """
    Find a company by slug, name, or partial name match.
    Case-insensitive. Returns the first match.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, slug, sector, country, stage, ticker
                FROM mimir.companies
                WHERE is_active = TRUE
                  AND (
                      slug = %s
                      OR LOWER(name) = %s
                      OR LOWER(name) LIKE %s
                  )
                ORDER BY
                    CASE WHEN slug = %s THEN 0
                         WHEN LOWER(name) = %s THEN 1
                         ELSE 2 END
                LIMIT 1
            """, (query, query, f"%{query}%", query, query))
            row = cur.fetchone()
            return dict(row) if row else None


def _get_recent_signals(company_id: str, limit: int = 5) -> list[dict]:
    """Most recent scored signals for a company."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT headline, relevance_score, sentiment,
                       investment_implication, source_url, discovered_at
                FROM mimir.signals
                WHERE company_id = %s
                  AND is_duplicate = FALSE
                  AND scored_at IS NOT NULL
                ORDER BY discovered_at DESC
                LIMIT %s
            """, (company_id, limit))
            return [dict(row) for row in cur.fetchall()]


def _get_latest_ipo_score(company_id: str) -> dict | None:
    """Most recent IPO readiness score for a company."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT score, strength, rationale, scored_at
                FROM mimir.ipo_scores
                WHERE company_id = %s
                ORDER BY scored_at DESC
                LIMIT 1
            """, (company_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def _get_all_companies_with_scores() -> list[dict]:
    """All active companies with their latest IPO score, sorted by score desc."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    c.id, c.name, c.stage, c.country, c.ticker,
                    latest.score AS ipo_score,
                    latest.strength AS ipo_strength
                FROM mimir.companies c
                LEFT JOIN LATERAL (
                    SELECT score, strength
                    FROM mimir.ipo_scores
                    WHERE company_id = c.id
                    ORDER BY scored_at DESC
                    LIMIT 1
                ) latest ON TRUE
                WHERE c.is_active = TRUE
                ORDER BY latest.score DESC NULLS LAST, c.name
            """)
            return [dict(row) for row in cur.fetchall()]


def _get_all_active_companies() -> list[dict]:
    """All active watchlist companies."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT c.id, c.name, c.slug, c.stage,
                       c.country, c.ticker, c.sector
                FROM mimir.companies c
                JOIN mimir.watchlist_companies wc ON wc.company_id = c.id
                JOIN mimir.watchlists w ON w.id = wc.watchlist_id
                WHERE c.is_active = TRUE AND w.is_active = TRUE
                ORDER BY c.name
            """)
            return [dict(row) for row in cur.fetchall()]


def _get_alert_preference() -> bool:
    """
    Read the alert preference for the owner client.
    Stored in platform.clients metadata as {"alerts_enabled": true}.
    Defaults to True if not set.
    """
    owner_chat_id = os.getenv("TELEGRAM_OWNER_CHAT_ID", "")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT metadata->>'alerts_enabled' AS alerts_enabled
                from core.clients
                WHERE telegram_chat_id = %s AND is_active = TRUE
                LIMIT 1
            """, (owner_chat_id,))
            row = cur.fetchone()
            if not row or row["alerts_enabled"] is None:
                return True  # default on
            return row["alerts_enabled"].lower() == "true"


def _set_alert_preference(enabled: bool) -> None:
    """Write the alert preference back to the client metadata."""
    owner_chat_id = os.getenv("TELEGRAM_OWNER_CHAT_ID", "")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE platform.clients
                SET metadata = jsonb_set(
                    metadata,
                    '{alerts_enabled}',
                    %s::jsonb
                )
                WHERE telegram_chat_id = %s
            """, (
                "true" if enabled else "false",
                owner_chat_id,
            ))


# ── Formatting helpers ───────────────────────────────────────

def _score_bar(score: int, max_score: int, width: int = 10) -> str:
    """
    Render a simple text progress bar for a score.

    _score_bar(72, 100, width=10) → "███████░░░"
    _score_bar(40, 100, width=8)  → "███░░░░░"
    """
    filled = round((score / max_score) * width)
    return "█" * filled + "░" * (width - filled)


# ── Entry point ──────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        stream=sys.stdout,
    )

    global config
    config = load_config()
    init_pool(config.db_url)

    # Verify TELEGRAM_OWNER_CHAT_ID is set — without it nothing works
    if not os.getenv("TELEGRAM_OWNER_CHAT_ID"):
        logger.critical(
            "TELEGRAM_OWNER_CHAT_ID not set in .env — "
            "commands will be rejected for everyone. Add your chat ID."
        )
        sys.exit(1)

    # Build the bot application
    app = Application.builder().token(config.telegram_bot_token).build()

    # Register command handlers
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("scores",    cmd_scores))
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    app.add_handler(CommandHandler("digest",    cmd_digest))
    app.add_handler(CommandHandler("alert",     cmd_alert))

    # Catch unrecognised commands from the owner
    app.add_handler(
        MessageHandler(filters.COMMAND, cmd_unknown)
    )

    # Register commands with Telegram so they appear in the menu
    # (the little slash menu that appears when you type /)
    async def post_init(application):
        await application.bot.set_my_commands([
            BotCommand("status",    "Latest signals for a company"),
            BotCommand("scores",    "IPO readiness ranking"),
            BotCommand("watchlist", "All monitored companies"),
            BotCommand("digest",    "Trigger digest now"),
            BotCommand("alert",     "Toggle immediate alerts"),
        ])

    app.post_init = post_init

    logger.info("Telegram handler started. Waiting for commands...")
    logger.info(f"Authorised chat ID: {os.getenv('TELEGRAM_OWNER_CHAT_ID')}")

    # Start polling — this blocks until the process is stopped
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
