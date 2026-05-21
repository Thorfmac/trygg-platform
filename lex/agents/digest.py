# =============================================================
# lex/agents/digest.py
# =============================================================
# Trygg Lex — daily regulatory digest agent.
#
# What it does:
#   1. Gets all clients with Telegram IDs
#   2. For each client, fetches regulatory items from last 24h
#      that scored >= 5 on their firm relevance score
#   3. Calls Claude Sonnet to synthesise a briefing
#   4. Sends via Telegram and stores in lex.digests
#
# Runs daily at 08:30 UTC (30 minutes after ingestion).
# =============================================================

import json
import logging
import uuid
from datetime import datetime, timezone

import anthropic
import requests as req

from core.config import Config
from core.database import get_conn, start_job_run, complete_job_run, fail_job_run

logger = logging.getLogger(__name__)

DIGEST_SCORE_THRESHOLD = 5
DIGEST_LOOKBACK_HOURS = 24

LEX_SYSTEM_PROMPT = """You are a senior regulatory compliance analyst for UK financial services.

You produce daily regulatory intelligence briefings for regulated firms — specifically IFA firms and wealth managers.

Your briefings are:
- Concise but substantive — every sentence earns its place
- Written for a principal or compliance officer, not a lawyer
- Formatted for Telegram using Markdown: *bold* for headings, _italic_ for emphasis
- Structured: headline summary → items by regulator → action checklist
- Honest about urgency — don't cry wolf, but don't downplay genuine risks

For each significant item, explain:
1. What the regulator has done or proposed
2. Why it matters for this specific firm type
3. What action (if any) is needed and by when

Use section dividers: ━━━━━━━━━━━━━━━━━━━━━━

Keep the total length under 3,000 characters for Telegram readability.
If there is nothing material, say so briefly — "No material regulatory developments today."

Never use HTML tags."""


def run(config: Config) -> dict:
    """Main entry point. Called by the scheduler daily at 08:30 UTC."""
    job_id = start_job_run("lex.digest", "lex")

    try:
        clients = _get_clients_with_telegram()
        logger.info(f"Lex digest: {len(clients)} clients")

        if not clients:
            complete_job_run(job_id, records_processed=0)
            return {"digests_sent": 0, "digests_empty": 0, "errors": 0}

        claude = anthropic.Anthropic(api_key=config.anthropic_api_key)

        digests_sent = 0
        digests_empty = 0
        errors = 0

        for client in clients:
            try:
                result = _generate_and_send(client, claude, config)
                if result == "sent":
                    digests_sent += 1
                elif result == "empty":
                    digests_empty += 1
            except Exception as e:
                logger.error(f"Lex digest failed for {client['name']}: {e}")
                errors += 1

        summary = {
            "digests_sent": digests_sent,
            "digests_empty": digests_empty,
            "errors": errors,
        }
        complete_job_run(job_id, records_processed=digests_sent, metadata=summary)
        logger.info(f"Lex digest complete — {digests_sent} sent, {digests_empty} empty, {errors} errors")
        return summary

    except Exception as e:
        fail_job_run(job_id, str(e))
        raise


def _generate_and_send(client: dict, claude: anthropic.Anthropic, config: Config) -> str:
    """Generate and send a Lex digest for one client."""
    items = _get_items_for_client(client["id"])

    if not items:
        logger.info(f"  {client['name']}: no items above threshold — skipping")
        return "empty"

    # Group by regulator
    by_regulator = {}
    for item in items:
        reg = item["regulator"].upper()
        if reg not in by_regulator:
            by_regulator[reg] = []
        by_regulator[reg].append(item)

    # Build synthesis prompt
    today = datetime.now(timezone.utc).strftime("%A %d %B %Y")
    firm_type = client.get("firm_type", "IFA firm")

    signal_summary = _build_signal_summary(by_regulator)

    user_prompt = f"""Produce the daily Trygg Lex regulatory briefing.

Date: {today}
For: {client['name']} ({firm_type})

Regulatory items from the last 24 hours:
{signal_summary}

Produce the briefing now."""

    try:
        response = claude.messages.create(
            model=config.model_synthesis,
            max_tokens=1500,
            system=[{
                "type": "text",
                "text": LEX_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_prompt}],
        )
        digest_text = response.content[0].text.strip()
    except anthropic.APIError as e:
        logger.error(f"Claude synthesis failed: {e}")
        return "empty"

    if not digest_text:
        return "empty"

    # Store digest
    digest_id = _store_digest(
        client_id=client["id"],
        content=digest_text,
        item_count=len(items),
        regulator_count=len(by_regulator),
        model_used=config.model_synthesis,
    )

    # Send via Telegram
    if client.get("telegram_chat_id"):
        _send_telegram(config.telegram_bot_token, client["telegram_chat_id"], digest_text)
        _mark_delivered(digest_id)
        logger.info(f"  {client['name']}: sent ({len(items)} items, {len(by_regulator)} regulators)")

    return "sent"


def _build_signal_summary(by_regulator: dict) -> str:
    lines = []
    for regulator, items in by_regulator.items():
        lines.append(f"\n{regulator}:")
        for item in items:
            score = item.get("firm_relevance_score") or item.get("relevance_score", "?")
            urgency = item.get("urgency", "low")
            lines.append(f"  • [{score}/10, {urgency}] {item['title']}")
            if item.get("firm_specific_note"):
                lines.append(f"    → {item['firm_specific_note']}")
            elif item.get("ai_commentary"):
                lines.append(f"    → {item['ai_commentary']}")
    return "\n".join(lines)


def _send_telegram(bot_token: str, chat_id: str, text: str) -> None:
    """Send message via direct HTTP — works synchronously."""
    max_length = 4096
    if len(text) <= max_length:
        chunks = [text]
    else:
        # Split at paragraph boundaries
        chunks = []
        current = ""
        for para in text.split("\n\n"):
            if len(current) + len(para) + 2 <= max_length:
                current += para + "\n\n"
            else:
                if current:
                    chunks.append(current.strip())
                current = para + "\n\n"
        if current:
            chunks.append(current.strip())

    for i, chunk in enumerate(chunks):
        prefix = f"_(part {i+1}/{len(chunks)})_\n\n" if len(chunks) > 1 else ""
        try:
            req.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": prefix + chunk,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")


# ── Database helpers ─────────────────────────────────────────

def _get_clients_with_telegram() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, telegram_chat_id, tier,
                       COALESCE(metadata->>'firm_type', 'IFA firm') AS firm_type
                FROM platform.clients
                WHERE is_active = TRUE
                  AND telegram_chat_id IS NOT NULL
            """)
            return [dict(row) for row in cur.fetchall()]


def _get_items_for_client(client_id: str) -> list[dict]:
    """Get regulatory items scored for this client in the last 24 hours."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    ri.id, ri.regulator, ri.title, ri.summary,
                    ri.source_url, ri.published_at, ri.urgency,
                    ri.relevance_score, ri.impact_areas, ri.ai_commentary,
                    ics.firm_relevance_score, ics.firm_specific_note
                FROM lex.regulatory_items ri
                LEFT JOIN lex.item_client_scores ics
                    ON ics.item_id = ri.id AND ics.client_id = %s
                WHERE ri.discovered_at >= NOW() - INTERVAL '24 hours'
                  AND (
                      COALESCE(ics.firm_relevance_score, ri.relevance_score) >= %s
                  )
                ORDER BY
                    COALESCE(ics.firm_relevance_score, ri.relevance_score) DESC,
                    ri.discovered_at DESC
            """, (client_id, DIGEST_SCORE_THRESHOLD))
            return [dict(row) for row in cur.fetchall()]


def _store_digest(
    client_id: str,
    content: str,
    item_count: int,
    regulator_count: int,
    model_used: str,
) -> str:
    digest_id = str(uuid.uuid4())
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO lex.digests (
                    id, client_id, digest_type, content,
                    item_count, period_start, period_end,
                    generated_at, model_used
                ) VALUES (
                    %s, %s, 'daily', %s,
                    %s, NOW() - INTERVAL '24 hours', NOW(),
                    NOW(), %s
                )
            """, (digest_id, client_id, content, item_count, model_used))
    return digest_id


def _mark_delivered(digest_id: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE lex.digests SET delivered_at = NOW() WHERE id = %s",
                (digest_id,)
            )
