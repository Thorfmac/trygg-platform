# =============================================================
# lex/agents/ingestion.py
# =============================================================
# Trygg Lex — regulatory feed ingestion agent.
#
# What it does:
#   1. Fetches RSS feeds from FCA, PRA, ESMA, ICO, and EBA/DORA
#   2. Parses each item — title, summary, link, published date
#   3. Deduplicates against already-stored items
#   4. Scores each item for general relevance using Claude Haiku
#   5. Scores each item against each active client's firm profile
#   6. Stores results in lex.regulatory_items
#
# Feed sources:
#   FCA  — https://www.fca.org.uk/rss.xml (news + warnings)
#   PRA  — https://www.bankofengland.co.uk/rss/news
#   ESMA — https://www.esma.europa.eu/press-news/esma-news/rss.xml
#   ICO  — https://ico.org.uk/about-the-ico/what-we-do/our-work-programme/project-updates/rss/
#   EBA  — https://www.eba.europa.eu/rss/latest-news (covers DORA)
#
# This agent runs once daily at 08:00 UTC via the scheduler.
# =============================================================

import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import anthropic
import feedparser
import requests

from core.config import Config
from core.database import get_conn, start_job_run, complete_job_run, fail_job_run

logger = logging.getLogger(__name__)

# Feed definitions
REGULATORY_FEEDS = [
    {
        "regulator": "fca",
        "name": "FCA News & Publications",
        "url": "https://www.fca.org.uk/rss.xml",
    },
    {
        "regulator": "pra",
        "name": "PRA / Bank of England",
        "url": "https://www.bankofengland.co.uk/rss/news",
    },
    {
        "regulator": "esma",
        "name": "ESMA",
        "url": "https://www.esma.europa.eu/press-news/esma-news/rss.xml",
    },
    {
    "regulator": "fca_enforcement",
    "name": "FCA Enforcement & Warnings",
    "url": "https://www.fca.org.uk/news/rss.xml",
    },
    {
    "regulator": "eba",
    "name": "EBA (DORA & prudential)",
    "url": "https://www.eba.europa.eu/news-press/news/rss.xml",
   },
]

# Haiku system prompt for regulatory item scoring
TRIAGE_SYSTEM_PROMPT = """You are a regulatory intelligence analyst for UK financial services firms.

Your task is to evaluate regulatory publications and score their relevance and urgency for regulated firms.

You must respond with valid JSON only. No preamble, no explanation, no markdown code fences.

Scoring criteria for relevance_score (1-10):
- 1-3: General industry news, speeches, minor updates with no direct operational impact
- 4-6: Moderately relevant — policy developments worth monitoring
- 7-8: Directly relevant — consultation papers, policy statements, guidance affecting operations
- 9-10: Critical — final rules, enforcement actions, deadlines within 90 days, DORA/Consumer Duty/SMCR updates

Urgency levels:
- "low": Informational, no near-term action required
- "medium": Monitor closely, may require response within 6 months
- "high": Action likely required within 3 months
- "critical": Immediate attention — final rules, enforcement, imminent deadlines

Impact areas (select all that apply from this list):
consumer_duty, smcr, operational_resilience, data_protection, aml, cryptoassets,
ai_governance, investment_management, retail_distribution, compliance_monitoring,
capital_requirements, dora, cyber_resilience, sustainability, enforcement

Response format (JSON only):
{
  "relevance_score": <integer 1-10>,
  "urgency": "<low|medium|high|critical>",
  "action_required": <true|false>,
  "impact_areas": ["area1", "area2"],
  "ai_commentary": "<2-3 sentences: what this means for a typical IFA or wealth manager>"
}"""


# Firm profile matching prompt
FIRM_PROFILE_PROMPT = """You are scoring a regulatory item for relevance to a specific financial services firm.

Firm profile:
{firm_profile}

Regulatory item:
Title: {title}
Summary: {summary}
Regulator: {regulator}
General impact areas: {impact_areas}

Score this item specifically for this firm type and profile on a scale of 1-10.
Consider: their business model, client types, regulatory permissions, and risk areas.

Respond with JSON only:
{{"firm_relevance_score": <integer 1-10>, "firm_specific_note": "<one sentence why this matters or doesn't for this firm>"}}"""


def run(config: Config) -> dict:
    """Main entry point. Called by the scheduler daily at 08:00 UTC."""
    job_id = start_job_run("lex.ingestion", "lex")

    try:
        claude = anthropic.Anthropic(api_key=config.anthropic_api_key)
        clients = _get_active_clients_with_profiles()

        total_new = 0
        total_duplicate = 0
        feed_results = []

        for feed in REGULATORY_FEEDS:
            result = _process_feed(feed, clients, claude, config)
            total_new += result["new"]
            total_duplicate += result["duplicates"]
            feed_results.append(result)
            logger.info(
                f"  {feed['name']}: {result['new']} new, {result['duplicates']} duplicates"
            )
            time.sleep(1)  # polite pause between feeds

        summary = {
            "feeds_scanned": len(REGULATORY_FEEDS),
            "items_new": total_new,
            "items_duplicate": total_duplicate,
            "feed_detail": feed_results,
        }

        complete_job_run(job_id, records_processed=total_new, metadata=summary)
        logger.info(f"Lex ingestion complete — {total_new} new items across {len(REGULATORY_FEEDS)} feeds")
        return summary

    except Exception as e:
        fail_job_run(job_id, str(e))
        raise


def _process_feed(feed: dict, clients: list, claude: anthropic.Anthropic, config: Config) -> dict:
    """Fetch, parse and store items from one RSS feed."""
    regulator = feed["regulator"]
    new_count = 0
    duplicate_count = 0

    try:
        parsed = feedparser.parse(feed["url"])
    except Exception as e:
        logger.error(f"Failed to fetch {feed['name']}: {e}")
        return {"regulator": regulator, "new": 0, "duplicates": 0, "error": str(e)}

    for entry in parsed.entries[:10]:  # cap at 20 per feed per run
        title = entry.get("title", "").strip()
        link = entry.get("link", "").strip()
        summary = entry.get("summary", entry.get("description", "")).strip()
        published_at = _parse_feed_date(entry)

        if not title or not link:
            continue

        content_hash = _hash_item(title, link)

        if _item_exists(content_hash):
            duplicate_count += 1
            continue

        # Score with Claude Haiku
        scores = _score_item(title, summary, regulator, claude, config)

        if not scores:
            continue

        # Store the item
        item_id = _store_item(
            regulator=regulator,
            title=title,
            summary=summary[:2000] if summary else None,
            source_url=link,
            published_at=published_at,
            content_hash=content_hash,
            scores=scores,
            model_used=config.model_triage,
        )

        # Score against each client's firm profile
        if item_id and clients and scores.get("relevance_score", 0) >= 5:
            for client in clients:
                _score_for_client(item_id, client, title, summary, regulator, scores, claude, config)
                time.sleep(0.3)

        new_count += 1
        time.sleep(1.5)  # pace Claude API calls

    return {"regulator": regulator, "new": new_count, "duplicates": duplicate_count}


def _score_item(title: str, summary: str, regulator: str, claude: anthropic.Anthropic, config: Config) -> dict | None:
    """Score a regulatory item using Claude Haiku."""
    content = f"Regulator: {regulator.upper()}\nTitle: {title}"
    if summary:
        content += f"\nSummary: {summary[:500]}"

    try:
        response = claude.messages.create(
            model=config.model_triage,
            max_tokens=300,
            system=[{
                "type": "text",
                "text": TRIAGE_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": f"Score this regulatory item:\n\n{content}"}],
        )
        raw = response.content[0].text.strip()

        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]

        return json.loads(raw.strip())

    except (json.JSONDecodeError, anthropic.APIError) as e:
        logger.warning(f"Scoring failed for '{title[:60]}': {e}")
        return None


def _score_for_client(
    item_id: str,
    client: dict,
    title: str,
    summary: str,
    regulator: str,
    general_scores: dict,
    claude: anthropic.Anthropic,
    config: Config,
) -> None:
    """Score a regulatory item against a specific client's firm profile."""
    firm_profile = client.get("firm_profile", {})
    if not firm_profile:
        return

    prompt = FIRM_PROFILE_PROMPT.format(
        firm_profile=json.dumps(firm_profile, indent=2),
        title=title,
        summary=(summary or "")[:300],
        regulator=regulator.upper(),
        impact_areas=", ".join(general_scores.get("impact_areas", [])),
    )

    try:
        response = claude.messages.create(
            model=config.model_triage,
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]

        result = json.loads(raw.strip())

        _store_client_score(
            item_id=item_id,
            client_id=client["id"],
            firm_relevance_score=result.get("firm_relevance_score", 5),
            firm_specific_note=result.get("firm_specific_note", ""),
        )

    except (json.JSONDecodeError, anthropic.APIError) as e:
        logger.warning(f"Firm scoring failed for client {client['name']}: {e}")


# ── Database helpers ─────────────────────────────────────────

def _item_exists(content_hash: str) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM lex.regulatory_items WHERE content_hash = %s",
                (content_hash,)
            )
            return cur.fetchone() is not None


def _store_item(
    regulator: str,
    title: str,
    summary: str | None,
    source_url: str,
    published_at: datetime | None,
    content_hash: str,
    scores: dict,
    model_used: str,
) -> str | None:
    item_id = str(uuid.uuid4())
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO lex.regulatory_items (
                        id, regulator, item_type, title, summary,
                        source_url, published_at, discovered_at,
                        relevance_score, urgency, action_required,
                        impact_areas, ai_commentary, model_used, scored_at,
                        content_hash
                    ) VALUES (
                        %s, %s, 'other', %s, %s,
                        %s, %s, NOW(),
                        %s, %s, %s,
                        %s, %s, %s, NOW(),
                        %s
                    )
                """, (
                    item_id, regulator, title, summary,
                    source_url, published_at,
                    scores.get("relevance_score"),
                    scores.get("urgency", "low"),
                    scores.get("action_required", False),
                    scores.get("impact_areas", []),
                    scores.get("ai_commentary"),
                    model_used,
                    content_hash,
                ))
        return item_id
    except Exception as e:
        logger.error(f"Failed to store item '{title[:60]}': {e}")
        return None


def _store_client_score(item_id: str, client_id: str, firm_relevance_score: int, firm_specific_note: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO lex.item_client_scores (id, item_id, client_id, firm_relevance_score, firm_specific_note, scored_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
                ON CONFLICT (item_id, client_id) DO UPDATE
                SET firm_relevance_score = EXCLUDED.firm_relevance_score,
                    firm_specific_note = EXCLUDED.firm_specific_note,
                    scored_at = NOW()
            """, (str(uuid.uuid4()), item_id, client_id, firm_relevance_score, firm_specific_note))


def _get_active_clients_with_profiles() -> list[dict]:
    """Return all active clients that have a firm profile configured."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, telegram_chat_id, tier,
                       metadata->>'firm_profile' AS firm_profile_json
                FROM platform.clients
                WHERE is_active = TRUE
                  AND metadata ? 'firm_profile'
            """)
            rows = cur.fetchall()
            clients = []
            for row in rows:
                client = dict(row)
                if client.get("firm_profile_json"):
                    try:
                        client["firm_profile"] = json.loads(client["firm_profile_json"])
                    except json.JSONDecodeError:
                        client["firm_profile"] = {}
                clients.append(client)
            return clients


def _hash_item(title: str, url: str) -> str:
    raw = f"{title.strip().lower()}|{url.strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _parse_feed_date(entry) -> datetime | None:
    for field in ("published", "updated", "created"):
        val = entry.get(f"{field}_parsed") or entry.get(field)
        if val:
            try:
                if isinstance(val, str):
                    return parsedate_to_datetime(val).replace(tzinfo=timezone.utc)
                elif hasattr(val, "tm_year"):
                    import calendar
                    return datetime.fromtimestamp(calendar.timegm(val), tz=timezone.utc)
            except Exception:
                continue
    return None
