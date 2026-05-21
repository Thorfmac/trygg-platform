# =============================================================
# mimir/agents/triage.py
# =============================================================
# The triage agent.
#
# What it does:
#   1. Finds all signals that have been ingested but not yet scored
#   2. For each signal, calls Claude Haiku with a structured prompt
#   3. Parses the AI response — relevance score, sentiment,
#      investment implication
#   4. Writes the scores back to the signal record in the database
#   5. Flags high-scoring signals (>=7) for immediate alert consideration
#
# Why Haiku and not Sonnet?
#   This agent processes potentially hundreds of articles per day.
#   Haiku is fast and cheap ($1/$5 per million tokens) and the task
#   is mechanical — score this article on a scale of 1-10.
#   Sonnet is reserved for synthesis and digest generation where
#   reasoning quality visibly matters to the reader.
#
# Prompt caching:
#   The system prompt is identical on every call, so it's cached
#   after the first call — 90% cost reduction on the system prompt
#   for every subsequent article scored in the same session.
# =============================================================

import json
import logging
import time

import anthropic
import psycopg2

from core.config import Config
from core.database import (
    get_conn,
    update_signal_scores,
    start_job_run,
    complete_job_run,
    fail_job_run,
)

logger = logging.getLogger(__name__)

# Only score signals above this word count — very short snippets
# (often just headlines repeated) don't give the model enough to
# work with and produce unreliable scores
MIN_CONTENT_LENGTH = 50

# Pause between API calls to avoid rate limit errors
# Haiku rate limit is generous but good practice
API_CALL_DELAY_SECONDS = 0.3


# The system prompt — sent once per session, cached by Anthropic
# after the first call. Every subsequent article scored in the
# same session pays only 10% of the system prompt token cost.
TRIAGE_SYSTEM_PROMPT = """You are an investment intelligence analyst specialising in deep technology companies, particularly in post-quantum cryptography (PQC), cybersecurity, and adjacent sectors.

Your task is to evaluate news articles about companies on an investment watchlist and score them for investment relevance.

You must respond with valid JSON only. No preamble, no explanation, no markdown code fences. Just the JSON object.

Scoring criteria:
- 1-3: Low relevance. General industry news, minor product updates, or tangential mentions.
- 4-6: Moderate relevance. Meaningful company activity but not a direct investment signal.
- 7-8: High relevance. Significant signal — major contract, substantial funding, key hire, regulatory milestone.
- 9-10: Critical. IPO filing, acquisition offer, transformative partnership, or major threat to thesis.

Sentiment:
- "positive": strengthens the investment thesis
- "negative": weakens the investment thesis or raises concerns
- "neutral": informational but no clear directional implication

Response format (JSON only):
{
  "relevance_score": <integer 1-10>,
  "sentiment": "<positive|negative|neutral>",
  "investment_implication": "<one sentence, max 150 chars, explaining what this means for an investor>",
  "signal_type_refined": "<funding_round|ipo_signal|leadership_change|partnership|product_launch|patent|regulatory_filing|news>"
}"""


def run(config: Config) -> dict:
    """
    Main entry point. Called by the scheduler after feed ingestion.
    Scores all unscored signals in the database.
    """
    job_id = start_job_run("mimir.triage", "mimir")

    try:
        # Get all signals that have been ingested but not yet scored
        unscored = _get_unscored_signals()
        logger.info(f"Triage: {len(unscored)} signals to score")

        if not unscored:
            complete_job_run(job_id, records_processed=0)
            return {"scored": 0, "skipped": 0, "errors": 0}

        # Initialise the Anthropic client
        client = anthropic.Anthropic(api_key=config.anthropic_api_key)

        scored = 0
        skipped = 0
        errors = 0
        high_value_signals = []  # score >= 7

        for signal in unscored:
            # Skip signals with too little content to score reliably
            content = _build_content(signal)
            if len(content) < MIN_CONTENT_LENGTH:
                skipped += 1
                continue

            try:
                result = _score_signal(signal, content, client, config)

                if result:
                    update_signal_scores(
                        signal_id=signal["id"],
                        relevance_score=result["relevance_score"],
                        sentiment=result["sentiment"],
                        investment_implication=result["investment_implication"],
                        model_used=config.model_triage,
                    )
                    scored += 1

                    # Track high-value signals for the alert system
                    if result["relevance_score"] >= 7:
                        high_value_signals.append({
                            "signal_id": signal["id"],
                            "company_name": signal["company_name"],
                            "headline": signal["headline"],
                            "score": result["relevance_score"],
                            "sentiment": result["sentiment"],
                            "implication": result["investment_implication"],
                        })
                        logger.info(
                            f"  HIGH VALUE [{result['relevance_score']}/10] "
                            f"{signal['company_name']}: {signal['headline'][:80]}"
                        )
                else:
                    errors += 1

                # Polite pause between API calls
                time.sleep(API_CALL_DELAY_SECONDS)

            except Exception as e:
                logger.error(f"Error scoring signal {signal['id']}: {e}")
                errors += 1
                continue

        summary = {
            "scored": scored,
            "skipped": skipped,
            "errors": errors,
            "high_value_count": len(high_value_signals),
            "high_value_signals": high_value_signals,
        }

        complete_job_run(job_id, records_processed=scored, metadata=summary)
        logger.info(
            f"Triage complete — {scored} scored, "
            f"{len(high_value_signals)} high-value, "
            f"{skipped} skipped, {errors} errors"
        )
        return summary

    except Exception as e:
        fail_job_run(job_id, str(e))
        raise


def _get_unscored_signals() -> list[dict]:
    """
    Return all signals that have been ingested but not yet scored.
    Joins to companies table to get the company name for context.
    Ordered oldest-first so we process in chronological order.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    s.id,
                    s.company_id,
                    c.name AS company_name,
                    c.sector AS company_sector,
                    s.signal_type,
                    s.headline,
                    s.summary,
                    s.source_name,
                    s.published_at
                FROM mimir.signals s
                JOIN mimir.companies c ON c.id = s.company_id
                WHERE s.scored_at IS NULL
                  AND s.is_duplicate = FALSE
                ORDER BY s.discovered_at ASC
                LIMIT 200  -- process at most 200 per run to avoid rate limits
            """)
            return [dict(row) for row in cur.fetchall()]


def _build_content(signal: dict) -> str:
    """
    Assemble the article content to send to the model.
    We include company context so the model understands the investment thesis.
    """
    parts = [
        f"Company: {signal['company_name']} ({signal['company_sector']})",
        f"Headline: {signal['headline']}",
    ]
    if signal.get("summary"):
        parts.append(f"Summary: {signal['summary']}")
    if signal.get("source_name"):
        parts.append(f"Source: {signal['source_name']}")

    return "\n".join(parts)


def _score_signal(
    signal: dict,
    content: str,
    client: anthropic.Anthropic,
    config: Config,
) -> dict | None:
    """
    Send one signal to Claude Haiku for scoring.
    Returns the parsed scoring dict, or None if something went wrong.

    The system prompt is marked for caching — after the first call
    in a session, Anthropic caches it and charges only 10% of the
    normal input token cost for the cached portion on every
    subsequent call. For 200 signals per run, this saves ~90%
    of system prompt costs.
    """
    try:
        response = client.messages.create(
            model=config.model_triage,
            max_tokens=256,  # scores are short — 256 is more than enough
            system=[
                {
                    "type": "text",
                    "text": TRIAGE_SYSTEM_PROMPT,
                    # This tells Anthropic to cache the system prompt
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": f"Score this signal:\n\n{content}",
                }
            ],
        )

        raw_text = response.content[0].text.strip()
        return _parse_score_response(raw_text)

    except anthropic.RateLimitError:
        logger.warning("Anthropic rate limit hit — pausing 10 seconds")
        time.sleep(10)
        return None
    except anthropic.APIError as e:
        logger.error(f"Anthropic API error: {e}")
        return None


def _parse_score_response(raw_text: str) -> dict | None:
    """
    Parse the JSON response from the model.
    Returns None if the response is malformed.

    We're defensive here — if the model returns anything unexpected
    we log it and move on rather than crashing the whole batch.
    """
    try:
        # Strip markdown code fences if Claude wraps the response
        clean = raw_text.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1]  # remove ```json line
            clean = clean.rsplit("```", 1)[0]  # remove closing ```
        data = json.loads(clean.strip())

        # Validate required fields
        score = int(data.get("relevance_score", 0))
        sentiment = data.get("sentiment", "neutral")
        implication = data.get("investment_implication", "")

        # Clamp score to valid range just in case
        score = max(1, min(10, score))

        if sentiment not in ("positive", "negative", "neutral"):
            sentiment = "neutral"

        return {
            "relevance_score": score,
            "sentiment": sentiment,
            "investment_implication": implication[:200],  # hard cap
        }

    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning(f"Could not parse score response: {e}\nRaw: {raw_text[:200]}")
        return None
