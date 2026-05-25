# =============================================================
# mimir/telegram_handler.py
# =============================================================
import logging
import os
import sys
import asyncio

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


# ── Auth ──────────────────────────────────────────────────────

def _is_authorised(update: Update) -> bool:
    owner_id = os.getenv("TELEGRAM_OWNER_CHAT_ID", "")
    if not owner_id:
        logger.warning("TELEGRAM_OWNER_CHAT_ID not set — all commands rejected")
        return False
    return str(update.effective_chat.id) == str(owner_id)


# ── Mímir command handlers ────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        return
    await update.message.reply_text(
        "*Mímir is online* 🟢\n\n"
        "*Mímir commands:*\n"
        "• `/status <company>` — latest signals and IPO score\n"
        "• `/scores` — all companies ranked by IPO readiness\n"
        "• `/watchlist` — full company list\n"
        "• `/digest` — trigger digest now\n"
        "• `/alert on|off` — toggle immediate alerts\n\n"
        "*Ares commands:*\n"
        "• `/ares` — today's Ares briefing\n"
        "• `/ares_status <TICKER>` — equity detail\n"
        "• `/ares_radar` — undervaluation flags\n"
        "• `/ares_watchlist` — full Ares watchlist\n"
        "• `/ares_candidates` — watchlist candidates\n"
        "• `/ares_approve <ID>` — approve trade proposal\n"
        "• `/ares_approve_all` — approve all proposals\n"
        "• `/ares_reject <ID>` — reject trade proposal\n"
        "• `/ares_positions` — today's executed trades\n"
        "• `/ares_halt` — emergency halt all trades\n",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: `/status <company>`\nExample: `/status pqshield`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    query = " ".join(context.args).strip().lower()
    company = _find_company(query)
    if not company:
        await update.message.reply_text(
            f"Company not found: `{query}`\n\nTry `/watchlist` to see available companies.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    signals = _get_recent_signals(company["id"], limit=5)
    ipo = _get_latest_ipo_score(company["id"])
    lines = [
        f"*{company['name']}*",
        f"_{company['sector']} · {company['country']} · {company['stage'].replace('_', ' ')}_",
        "",
    ]
    if ipo:
        bar = _score_bar(ipo["score"], 100)
        lines += [
            "*IPO Readiness*",
            f"{bar} {ipo['score']}/100 — _{ipo['strength'].replace('_', ' ')}_",
        ]
        if ipo.get("rationale"):
            lines.append(f"_{ipo['rationale']}_")
        lines.append("")
    else:
        lines += ["*IPO Readiness*", "_Not yet scored_", ""]
    if signals:
        lines.append("*Recent signals*")
        for sig in signals:
            score = sig.get("relevance_score")
            score_label = f"[{score}/10]" if score else "[unscored]"
            sentiment_mark = {"positive": "▲", "negative": "▼", "neutral": "—"}.get(sig.get("sentiment", "neutral"), "—")
            lines.append(f"• {sentiment_mark} {sig['headline'][:80]} {score_label}")
            if sig.get("investment_implication"):
                lines.append(f"  _{sig['investment_implication'][:120]}_")
    else:
        lines.append("_No recent signals_")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)


async def cmd_scores(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        return
    companies = _get_all_companies_with_scores()
    if not companies:
        await update.message.reply_text("No companies found in watchlist.")
        return
    lines = ["*IPO Readiness Ranking*", ""]
    scored = [c for c in companies if c.get("ipo_score") is not None]
    unscored = [c for c in companies if c.get("ipo_score") is None]
    for i, co in enumerate(scored, 1):
        bar = _score_bar(co["ipo_score"], 100, width=8)
        strength = (co.get("ipo_strength") or "").replace("_", " ")
        lines.append(f"{i}. *{co['name']}* {bar} {co['ipo_score']}/100" + (f" — _{strength}_" if strength else ""))
    if unscored:
        if scored:
            lines.append("")
        lines.append("_Not yet scored:_")
        for co in unscored:
            lines.append(f"• {co['name']} ({co['stage'].replace('_', ' ')})")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        return
    companies = _get_all_active_companies()
    if not companies:
        await update.message.reply_text("No active companies in watchlist.")
        return
    by_stage = {}
    for co in companies:
        stage = co["stage"].replace("_", " ").title()
        by_stage.setdefault(stage, []).append(co)
    stage_order = ["Pre Ipo", "Private Growth", "Private Early", "Public", "Acquired"]
    lines = [f"*Watchlist — {len(companies)} companies*", ""]
    for stage in stage_order:
        if stage not in by_stage:
            continue
        lines.append(f"*{stage}*")
        for co in by_stage[stage]:
            ticker = f" ({co['ticker']})" if co.get("ticker") else ""
            lines.append(f"• {co['name']}{ticker} — {co['country']}")
        lines.append("")
    await update.message.reply_text("\n".join(lines).strip(), parse_mode=ParseMode.MARKDOWN)


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        return
    await update.message.reply_text("Generating digest… this takes 30–60 seconds ⏳")
    try:
        from mimir.agents import digest as digest_agent
        result = digest_agent.run(config)
        if result["digests_sent"] == 0:
            await update.message.reply_text("_No signals above threshold — nothing to digest._", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(f"✓ Digest sent ({result['digests_sent']} briefing{'s' if result['digests_sent'] != 1 else ''})")
    except Exception as e:
        logger.error(f"On-demand digest failed: {e}")
        await update.message.reply_text(f"Digest failed: `{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN)


async def cmd_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        current = _get_alert_preference()
        status = "on 🟢" if current else "off 🔴"
        await update.message.reply_text(f"Immediate alerts are currently *{status}*\n\nTo change: `/alert on` or `/alert off`", parse_mode=ParseMode.MARKDOWN)
        return
    enabled = context.args[0].lower() == "on"
    _set_alert_preference(enabled)
    status = "on 🟢" if enabled else "off 🔴"
    await update.message.reply_text(f"Immediate alerts turned *{status}*", parse_mode=ParseMode.MARKDOWN)


# ── Ares command handlers ─────────────────────────────────────

async def _ares_dispatch(update: Update, command: str, args: str) -> None:
    import asyncpg
    pool = await asyncpg.create_pool(os.getenv("DATABASE_URL"), min_size=1, max_size=3)
    try:
        from ares.telegram_commands import handle_ares_command
        response = await handle_ares_command(command, args, pool)
    finally:
        await pool.close()
    chunks = [response[i:i+4000] for i in range(0, len(response), 4000)]
    for chunk in chunks:
        await update.message.reply_text(chunk)


async def cmd_ares(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update): return
    await _ares_dispatch(update, "/ares", "")

async def cmd_ares_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update): return
    await _ares_dispatch(update, "/ares_status", " ".join(context.args) if context.args else "")

async def cmd_ares_radar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update): return
    await _ares_dispatch(update, "/ares_radar", "")

async def cmd_ares_watchlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update): return
    await _ares_dispatch(update, "/ares_watchlist", "")

async def cmd_ares_candidates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update): return
    await _ares_dispatch(update, "/ares_candidates", "")

async def cmd_ares_thesis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update): return
    await _ares_dispatch(update, "/ares_thesis", "")

async def cmd_ares_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update): return
    await _ares_dispatch(update, "/ares_approve", " ".join(context.args) if context.args else "")

async def cmd_ares_approve_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update): return
    await _ares_dispatch(update, "/ares_approve_all", "")

async def cmd_ares_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update): return
    await _ares_dispatch(update, "/ares_reject", " ".join(context.args) if context.args else "")

async def cmd_ares_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update): return
    await _ares_dispatch(update, "/ares_positions", "")

async def cmd_ares_halt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update): return
    await _ares_dispatch(update, "/ares_halt", "")


async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update): return
    await update.message.reply_text("Unknown command. Try `/start` for the command list.", parse_mode=ParseMode.MARKDOWN)


# ── Database helpers ──────────────────────────────────────────

def _find_company(query: str) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, slug, sector, country, stage, ticker
                FROM mimir.companies WHERE is_active = TRUE
                  AND (slug = %s OR LOWER(name) = %s OR LOWER(name) LIKE %s)
                ORDER BY CASE WHEN slug = %s THEN 0 WHEN LOWER(name) = %s THEN 1 ELSE 2 END
                LIMIT 1
            """, (query, query, f"%{query}%", query, query))
            row = cur.fetchone()
            return dict(row) if row else None

def _get_recent_signals(company_id: str, limit: int = 5) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT headline, relevance_score, sentiment, investment_implication, source_url, discovered_at
                FROM mimir.signals WHERE company_id = %s AND is_duplicate = FALSE AND scored_at IS NOT NULL
                ORDER BY discovered_at DESC LIMIT %s
            """, (company_id, limit))
            return [dict(row) for row in cur.fetchall()]

def _get_latest_ipo_score(company_id: str) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT score, strength, rationale, scored_at FROM mimir.ipo_scores WHERE company_id = %s ORDER BY scored_at DESC LIMIT 1", (company_id,))
            row = cur.fetchone()
            return dict(row) if row else None

def _get_all_companies_with_scores() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.id, c.name, c.stage, c.country, c.ticker,
                       latest.score AS ipo_score, latest.strength AS ipo_strength
                FROM mimir.companies c
                LEFT JOIN LATERAL (SELECT score, strength FROM mimir.ipo_scores WHERE company_id = c.id ORDER BY scored_at DESC LIMIT 1) latest ON TRUE
                WHERE c.is_active = TRUE ORDER BY latest.score DESC NULLS LAST, c.name
            """)
            return [dict(row) for row in cur.fetchall()]

def _get_all_active_companies() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT c.id, c.name, c.slug, c.stage, c.country, c.ticker, c.sector
                FROM mimir.companies c
                JOIN mimir.watchlist_companies wc ON wc.company_id = c.id
                JOIN mimir.watchlists w ON w.id = wc.watchlist_id
                WHERE c.is_active = TRUE AND w.is_active = TRUE ORDER BY c.name
            """)
            return [dict(row) for row in cur.fetchall()]

def _get_alert_preference() -> bool:
    owner_chat_id = os.getenv("TELEGRAM_OWNER_CHAT_ID", "")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT metadata->>'alerts_enabled' AS alerts_enabled FROM platform.clients WHERE telegram_chat_id = %s AND is_active = TRUE LIMIT 1", (owner_chat_id,))
            row = cur.fetchone()
            if not row or row["alerts_enabled"] is None: return True
            return row["alerts_enabled"].lower() == "true"

def _set_alert_preference(enabled: bool) -> None:
    owner_chat_id = os.getenv("TELEGRAM_OWNER_CHAT_ID", "")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE platform.clients SET metadata = jsonb_set(metadata, '{alerts_enabled}', %s::jsonb) WHERE telegram_chat_id = %s", ("true" if enabled else "false", owner_chat_id))

def _score_bar(score: int, max_score: int, width: int = 10) -> str:
    filled = round((score / max_score) * width)
    return "█" * filled + "░" * (width - filled)


# ── Entry point ───────────────────────────────────────────────

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s", stream=sys.stdout)
    global config
    config = load_config()
    init_pool(config.db_url)

    if not os.getenv("TELEGRAM_OWNER_CHAT_ID"):
        logger.critical("TELEGRAM_OWNER_CHAT_ID not set — commands will be rejected.")
        sys.exit(1)

    app = Application.builder().token(config.telegram_bot_token).build()

    # Mímir
    app.add_handler(CommandHandler("start",            cmd_start))
    app.add_handler(CommandHandler("status",           cmd_status))
    app.add_handler(CommandHandler("scores",           cmd_scores))
    app.add_handler(CommandHandler("watchlist",        cmd_watchlist))
    app.add_handler(CommandHandler("digest",           cmd_digest))
    app.add_handler(CommandHandler("alert",            cmd_alert))
    # Ares
    app.add_handler(CommandHandler("ares",             cmd_ares))
    app.add_handler(CommandHandler("ares_status",      cmd_ares_status))
    app.add_handler(CommandHandler("ares_radar",       cmd_ares_radar))
    app.add_handler(CommandHandler("ares_watchlist",   cmd_ares_watchlist_cmd))
    app.add_handler(CommandHandler("ares_candidates",  cmd_ares_candidates))
    app.add_handler(CommandHandler("ares_thesis",      cmd_ares_thesis))
    app.add_handler(CommandHandler("ares_approve",     cmd_ares_approve))
    app.add_handler(CommandHandler("ares_approve_all", cmd_ares_approve_all))
    app.add_handler(CommandHandler("ares_reject",      cmd_ares_reject))
    app.add_handler(CommandHandler("ares_positions",   cmd_ares_positions))
    app.add_handler(CommandHandler("ares_halt",        cmd_ares_halt))
    # Catch-all
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))

    async def post_init(application):
        await application.bot.set_my_commands([
            BotCommand("start",            "Command list"),
            BotCommand("status",           "Latest signals for a company"),
            BotCommand("scores",           "IPO readiness ranking"),
            BotCommand("watchlist",        "All monitored companies"),
            BotCommand("digest",           "Trigger Mímir digest now"),
            BotCommand("alert",            "Toggle immediate alerts"),
            BotCommand("ares",             "Today's Ares briefing"),
            BotCommand("ares_status",      "Equity detail"),
            BotCommand("ares_radar",       "Undervaluation flags"),
            BotCommand("ares_watchlist",   "Ares watchlist"),
            BotCommand("ares_approve",     "Approve trade proposal"),
            BotCommand("ares_approve_all", "Approve all proposals"),
            BotCommand("ares_reject",      "Reject trade proposal"),
            BotCommand("ares_positions",   "Today's executed trades"),
            BotCommand("ares_halt",        "Emergency halt all trades"),
        ])

    app.post_init = post_init
    logger.info("Telegram handler started.")
    logger.info(f"Authorised chat ID: {os.getenv('TELEGRAM_OWNER_CHAT_ID')}")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
