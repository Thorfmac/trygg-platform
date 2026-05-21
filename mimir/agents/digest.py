# =============================================================
# mimir/agents/digest.py
# =============================================================
# The digest agent.
#
# What it does:
#   1. For each active client, finds all high-scoring signals
#      from the last 24 hours across their watchlist companies
#   2. Calls Claude Sonnet to synthesise those signals into a
#      readable, structured daily briefing
#   3. Stores the digest in the database (for audit and resend)
#   4. Sends it to the client's Telegram chat
#
# Two output types:
#   'daily'  — full briefing covering all companies with activity
#   'alert'  — immediate single-company alert for score >= 9
#              (triggered by triage agent, not the scheduler)
#
# Why Sonnet and not Haiku here?
#   The digest is what the client actually reads. It needs to be
#   well-written, coherent, and genuinely useful. Haiku is fine
#   for mechanical scoring; Sonnet is worth the extra cost when
#   the output is client-facing.
# =============================================================

import json
import logging
import uuid
from datetime import datetime, timezone, timedelta

import anthropic
from telegram import Bot
from telegram.constants import ParseMode

from core.config import Config
from core.database import get_conn, start_job_run, complete_job_run, fail_job_run

logger = logging.getLogger(__name__)

# Only include signals at or above this score in digests
# Signals below this are stored but not surfaced to clients
DIGEST_SCORE_THRESHOLD = 5

# For immediate alerts, only fire above this score
ALERT_SCORE_THRESHOLD = 9

# How far back the daily digest looks
DIGEST_LOOKBACK_HOURS = 24


# ── System prompt for digest synthesis ───────────────────────
# This is sent once per digest call and cached by Anthropic.
# Sonnet is reasoning quality here — the output lands in a
# client's Telegram and represents the Trygg brand.

DIGEST_SYSTEM_PROMPT = """You are Mímir, an investment intelligence analyst for Trygg. You produce daily briefings for investors focused on post-quantum cryptography and deep technology companies.

Your briefings are:
- Concise but substantive — every sentence earns its place
- Written for a sophisticated investor, not a general audience
- Formatted for Telegram using Markdown (bold with *, italic with _, code with `)
- Structured: headline summary → company-by-company findings → overall thesis assessment

Tone: professional, direct, slightly dry. Like a good equity research note, not a news summary.

Use Telegram Markdown formatting:
- *bold* for company names and key terms
- _italic_ for emphasis
- Bullet points with • (not -)
- Section headers with *HEADER*

Never use HTML. Never use triple backticks. Keep the total length under 3,500 characters so Telegram renders it cleanly."""


def run(config: Config) -> dict:
    """
    Main entry point for the scheduled daily digest.
    Generates and sends a digest for every active client.
    """
    job_id = start_job_run("mimir.digest", "mimir")

    try:
        clients = _get_active_clients_with_telegram()
        logger.info(f"Digest: generating for {len(clients)} clients")

        if not clients:
            logger.info("No clients with Telegram configured — skipping digest")
            complete_job_run(job_id, records_processed=0)
            return {"digests_sent": 0}

        client_api = anthropic.Anthropic(api_key=config.anthropic_api_key)
        telegram_bot = Bot(token=config.telegram_bot_token)

        digests_sent = 0
        digests_empty = 0
        errors = 0

        for client in clients:
            try:
                result = _generate_and_send_digest(
                    client=client,
                    config=config,
                    claude=client_api,
                    telegram=telegram_bot,
                    digest_type="daily",
                )
                if result == "sent":
                    digests_sent += 1
                elif result == "empty":
                    digests_empty += 1
            except Exception as e:
                logger.error(f"Digest failed for client {client['name']}: {e}")
                errors += 1

        summary = {
            "digests_sent": digests_sent,
            "digests_empty": digests_empty,
            "errors": errors,
        }
        complete_job_run(job_id, records_processed=digests_sent, metadata=summary)
        logger.info(f"Digest complete — {digests_sent} sent, {digests_empty} empty, {errors} errors")
        return summary

    except Exception as e:
        fail_job_run(job_id, str(e))
        raise


def send_alert(signal_id: str, config: Config) -> None:
    """
    Send an immediate alert for a single high-scoring signal.
    Called by the triage agent when a score >= ALERT_SCORE_THRESHOLD
    is detected, rather than waiting for the daily digest.

    This is a separate function because alerts are time-sensitive —
    a score-9 IPO filing signal should reach the client within
    minutes, not at 7am the next day.
    """
    signal = _get_signal_by_id(signal_id)
    if not signal:
        logger.warning(f"Alert requested for unknown signal: {signal_id}")
        return

    clients = _get_clients_watching_company(signal["company_id"])
    if not clients:
        return

    telegram_bot = Bot(token=config.telegram_bot_token)

    for client in clients:
        if not client.get("telegram_chat_id"):
            continue

        message = _format_alert_message(signal)
        _send_telegram_message(telegram_bot, client["telegram_chat_id"], message)
        _store_notification(client["id"], "mimir", "alert", signal["headline"], message)

    logger.info(
        f"Alert sent for {signal['company_name']} "
        f"(score {signal['relevance_score']}) to {len(clients)} clients"
    )


# ── Internal helpers ─────────────────────────────────────────

def _generate_and_send_digest(
    client: dict,
    config: Config,
    claude: anthropic.Anthropic,
    telegram: Bot,
    digest_type: str,
) -> str:
    """
    Generate and send a digest for one client.
    Returns 'sent', 'empty', or raises on error.
    """
    # Get all signals for this client's watchlist companies
    # from the last 24 hours, above the score threshold
    signals = _get_signals_for_client(
        client_id=client["id"],
        lookback_hours=DIGEST_LOOKBACK_HOURS,
        min_score=DIGEST_SCORE_THRESHOLD,
    )

    if not signals:
        logger.info(f"No signals above threshold for {client['name']} — skipping digest")
        return "empty"

    # Group signals by company for the synthesis prompt
    by_company = _group_signals_by_company(signals)

    # Generate the digest text with Sonnet
    digest_text = _synthesise_digest(
        client=client,
        by_company=by_company,
        digest_type=digest_type,
        claude=claude,
        config=config,
    )

    if not digest_text:
        return "empty"

    # Store in the database first (so it's preserved even if Telegram fails)
    digest_id = _store_digest(
        client_id=client["id"],
        watchlist_id=client["primary_watchlist_id"],
        digest_type=digest_type,
        content=digest_text,
        signal_count=len(signals),
        company_count=len(by_company),
        model_used=config.model_synthesis,
    )

    # Send via Telegram
    if client.get("telegram_chat_id"):
        _send_telegram_message(telegram, client["telegram_chat_id"], digest_text)
        _mark_digest_delivered(digest_id)
        _store_notification(
            client["id"], "mimir", "digest",
            f"Daily digest — {len(by_company)} companies",
            digest_text,
        )
        logger.info(f"Digest sent to {client['name']} ({len(signals)} signals, {len(by_company)} companies)")
    else:
        logger.warning(f"No Telegram chat ID for {client['name']} — digest stored but not sent")

    return "sent"


def _synthesise_digest(
    client: dict,
    by_company: dict,
    digest_type: str,
    claude: anthropic.Anthropic,
    config: Config,
) -> str | None:
    """
    Call Claude Sonnet to synthesise the signals into a readable digest.
    Returns the formatted text, or None if something went wrong.
    """
    # Build a structured summary of all signals to feed to the model
    signal_summary = _build_signal_summary(by_company)

    today = datetime.now(timezone.utc).strftime("%A %d %B %Y")

    user_prompt = f"""Generate a daily intelligence briefing for {client['name']}.

Date: {today}
Period: Last 24 hours

Signals to synthesise:
{signal_summary}

Produce the briefing now."""

    try:
        response = claude.messages.create(
            model=config.model_synthesis,
            max_tokens=1500,
            system=[
                {
                    "type": "text",
                    "text": DIGEST_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_prompt}],
        )
        return response.content[0].text.strip()

    except anthropic.APIError as e:
        logger.error(f"Anthropic API error during digest synthesis: {e}")
        return None


def _build_signal_summary(by_company: dict) -> str:
    """
    Format the signals dict into a structured text block for the prompt.

    Example output:
        PQSHIELD (score range: 7-8, sentiment: positive)
        • PQShield selected for NCSC 2026 pilot expansion [score: 8, positive]
          Implication: Strengthens government credibility as UK's leading PQC vendor
        • PQShield CTO presents at RSA Conference [score: 6, neutral]
          Implication: Maintains visibility but no new commercial signal
    """
    lines = []
    for company_name, data in by_company.items():
        signals = data["signals"]
        scores = [s["relevance_score"] for s in signals if s.get("relevance_score")]
        score_range = f"{min(scores)}-{max(scores)}" if len(scores) > 1 else str(scores[0]) if scores else "unscored"
        sentiments = [s["sentiment"] for s in signals if s.get("sentiment")]
        dominant_sentiment = max(set(sentiments), key=sentiments.count) if sentiments else "neutral"

        lines.append(f"\n{company_name.upper()} (score range: {score_range}, dominant sentiment: {dominant_sentiment})")
        for sig in signals:
            score_label = f"[score: {sig.get('relevance_score', '?')}, {sig.get('sentiment', 'neutral')}]"
            lines.append(f"  • {sig['headline']} {score_label}")
            if sig.get("investment_implication"):
                lines.append(f"    Implication: {sig['investment_implication']}")

    return "\n".join(lines)


def _group_signals_by_company(signals: list[dict]) -> dict:
    """
    Transform a flat list of signals into a dict keyed by company name.
    Sorted by highest score first within each company.
    """
    grouped = {}
    for signal in signals:
        name = signal["company_name"]
        if name not in grouped:
            grouped[name] = {"company_id": signal["company_id"], "signals": []}
        grouped[name]["signals"].append(signal)

    # Sort each company's signals by score descending
    for name in grouped:
        grouped[name]["signals"].sort(
            key=lambda s: s.get("relevance_score") or 0,
            reverse=True,
        )

    # Sort companies by their highest signal score
    return dict(
        sorted(
            grouped.items(),
            key=lambda item: max(
                (s.get("relevance_score") or 0 for s in item[1]["signals"]),
                default=0,
            ),
            reverse=True,
        )
    )


def _format_alert_message(signal: dict) -> str:
    """
    Format a single high-scoring signal as an immediate Telegram alert.
    Kept deliberately brief — this interrupts the client's day.
    """
    score = signal.get("relevance_score", "?")
    sentiment_emoji = {"positive": "🟢", "negative": "🔴", "neutral": "🟡"}.get(
        signal.get("sentiment", "neutral"), "🟡"
    )

    lines = [
        f"⚡ *MÍMIR ALERT* {sentiment_emoji}",
        f"",
        f"*{signal['company_name']}* — Score {score}/10",
        f"",
        f"{signal['headline']}",
    ]
    if signal.get("investment_implication"):
        lines += ["", f"_{signal['investment_implication']}_"]
    if signal.get("source_url"):
        lines += ["", f"[Read more]({signal['source_url']})"]

    return "\n".join(lines)


def _send_telegram_message(bot: Bot, chat_id: str, text: str) -> None:
    """
    Send a message via Telegram.
    Splits messages longer than 4096 chars (Telegram's hard limit).
    """
    max_length = 4096
    if len(text) <= max_length:
        bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
        return

    # Split on paragraph boundaries to avoid cutting mid-sentence
    chunks = _split_message(text, max_length)
    for i, chunk in enumerate(chunks):
        prefix = f"_(part {i+1}/{len(chunks)})_\n\n" if len(chunks) > 1 else ""
        bot.send_message(
            chat_id=chat_id,
            text=prefix + chunk,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )


def _split_message(text: str, max_length: int) -> list[str]:
    """Split a long message at paragraph boundaries."""
    paragraphs = text.split("\n\n")
    chunks = []
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 2 > max_length:
            if current:
                chunks.append(current.strip())
            current = para
        else:
            current = current + "\n\n" + para if current else para
    if current:
        chunks.append(current.strip())
    return chunks


# ── Database queries ─────────────────────────────────────────

def _get_active_clients_with_telegram() -> list[dict]:
    """Return all active clients who have a Telegram chat ID configured."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    c.id,
                    c.name,
                    c.telegram_chat_id,
                    c.tier,
                    -- Get their first active watchlist subscription
                    -- as the primary watchlist for this digest
                    (
                        SELECT cws.watchlist_id
                        FROM mimir.client_watchlist_subscriptions cws
                        JOIN mimir.watchlists w ON w.id = cws.watchlist_id
                        WHERE cws.client_id = c.id
                          AND w.is_active = TRUE
                        ORDER BY cws.subscribed_at
                        LIMIT 1
                    ) AS primary_watchlist_id
                from platform.clients c
                WHERE c.is_active = TRUE
                  AND c.telegram_chat_id IS NOT NULL
            """)
            return [dict(row) for row in cur.fetchall()]


def _get_signals_for_client(
    client_id: str,
    lookback_hours: int,
    min_score: int,
) -> list[dict]:
    """
    Get all signals above the score threshold for a client's watchlists,
    within the lookback window.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT
                    s.id,
                    s.company_id,
                    co.name AS company_name,
                    s.signal_type,
                    s.headline,
                    s.summary,
                    s.source_url,
                    s.source_name,
                    s.published_at,
                    s.relevance_score,
                    s.sentiment,
                    s.investment_implication
                FROM mimir.signals s
                JOIN mimir.companies co ON co.id = s.company_id
                JOIN mimir.watchlist_companies wc ON wc.company_id = s.company_id
                JOIN mimir.client_watchlist_subscriptions cws
                    ON cws.watchlist_id = wc.watchlist_id
                WHERE cws.client_id = %s
                  AND s.discovered_at >= NOW() - INTERVAL '%s hours'
                  AND s.relevance_score >= %s
                  AND s.is_duplicate = FALSE
                ORDER BY s.relevance_score DESC, s.discovered_at DESC
            """, (client_id, lookback_hours, min_score))
            return [dict(row) for row in cur.fetchall()]


def _get_signal_by_id(signal_id: str) -> dict | None:
    """Retrieve a single signal with its company name."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.*, c.name AS company_name
                FROM mimir.signals s
                JOIN mimir.companies c ON c.id = s.company_id
                WHERE s.id = %s
            """, (signal_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def _get_clients_watching_company(company_id: str) -> list[dict]:
    """Find all clients who are watching a given company."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT cl.id, cl.name, cl.telegram_chat_id
                from platform.clients cl
                JOIN mimir.client_watchlist_subscriptions cws ON cws.client_id = cl.id
                JOIN mimir.watchlist_companies wc ON wc.watchlist_id = cws.watchlist_id
                WHERE wc.company_id = %s
                  AND cl.is_active = TRUE
            """, (company_id,))
            return [dict(row) for row in cur.fetchall()]


def _store_digest(
    client_id: str,
    watchlist_id: str,
    digest_type: str,
    content: str,
    signal_count: int,
    company_count: int,
    model_used: str,
) -> str:
    """Persist the digest to the database. Returns the digest ID."""
    digest_id = str(uuid.uuid4())
    period_end = datetime.now(timezone.utc)
    period_start = period_end - timedelta(hours=DIGEST_LOOKBACK_HOURS)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO mimir.digests (
                    id, client_id, watchlist_id, digest_type,
                    period_start, period_end, content,
                    signal_count, company_count, model_used
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                digest_id, client_id, watchlist_id, digest_type,
                period_start, period_end, content,
                signal_count, company_count, model_used,
            ))
    return digest_id


def _mark_digest_delivered(digest_id: str) -> None:
    """Record the delivery timestamp on a digest."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE mimir.digests
                SET delivered_at = NOW()
                WHERE id = %s
            """, (digest_id,))


def _store_notification(
    client_id: str,
    module: str,
    notification_type: str,
    subject: str,
    body: str,
) -> None:
    """Log every outbound notification to platform.notifications."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO platform.notifications
                    (id, client_id, module, notification_type, subject, body, sent_at, delivery_status)
                VALUES (%s, %s, %s, %s, %s, %s, NOW(), 'sent')
            """, (
                str(uuid.uuid4()),
                client_id, module, notification_type,
                subject[:500], body,
            ))
