"""
Trygg Ares — Verifier Agent
===========================

Processes PENDING VERIFICATION items in ares.active_ledger by:
  1. Classifying the required_primary into a fetch strategy (EDGAR vs Firecrawl)
  2. Executing the fetch with multiple query shapes for breadth
  3. Passing claim + fetched content to Sonnet 4.6 for structured verdict extraction
  4. Updating ares.ledger with the verifier's proposal
  5. Auto-resolving ONLY if: HIGH confidence + Tier 1 source + verdict in (confirmed, denied)

Source-authority discipline preserved: the verifier never auto-resolves on
Tier 3-only sources, even with apparent corroboration. That gate is structural.

Usage:
    docker exec -it trygg-scheduler python -m ares.agents.verifier
    docker exec -e VERIFIER_DRY_RUN=true -it trygg-scheduler python -m ares.agents.verifier
    docker exec -e VERIFIER_LEDGER_ID=4 -it trygg-scheduler python -m ares.agents.verifier  # process one row

Scheduled via scheduler.py — runs daily at 07:30 UTC (15 min after Ares digest).
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import anthropic
import asyncpg
from dotenv import load_dotenv

# These imports happen lazily inside _classify_strategy to avoid loading them
# when DRY_RUN tests don't need them. Both modules raise on import if their
# respective env vars are missing.

load_dotenv()

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

DRY_RUN = os.getenv("VERIFIER_DRY_RUN", "").lower() in ("true", "1", "yes")
SINGLE_ROW_ID = os.getenv("VERIFIER_LEDGER_ID")
MAX_ITEMS_PER_RUN = int(os.getenv("VERIFIER_MAX_ITEMS", "5"))

# Model
EXTRACTION_MODEL = "claude-sonnet-4-6"
MAX_OUTPUT_TOKENS = 2048

# Fetch budget per claim (controls Firecrawl spend)
MAX_FIRECRAWL_QUERIES_PER_CLAIM = 2
MAX_FIRECRAWL_SCRAPES_PER_CLAIM = 3
MAX_EDGAR_FILINGS_PER_CLAIM = 3

# Per-source content cap before sending to Sonnet
MAX_CHARS_PER_SOURCE = 30_000

PROMPT_PATH = Path(__file__).parent / "verifier_prompt.md"
if not PROMPT_PATH.exists():
    raise FileNotFoundError(
        f"Verifier prompt not found at {PROMPT_PATH}. "
        "This is a required dependency."
    )
SYSTEM_PROMPT = PROMPT_PATH.read_text(encoding="utf-8")


# -----------------------------------------------------------------------------
# Strategy classification
# -----------------------------------------------------------------------------

# Tier 1 domains we prefer for general (non-EDGAR) Firecrawl fetches
TIER_1_DOMAINS = [
    "sec.gov", "data.sec.gov",
    "nasa.gov",
    "fcc.gov", "faa.gov", "sda.mil", "defense.gov",
    # Issuer IR pages (added per-entity in entity_ir_domain below)
]

# Issuer IR pages per ticker — used as Tier 1 fallback for press releases
ENTITY_IR_DOMAIN = {
    "LUNR": "intuitivemachines.com",
    "IONQ": "ionq.com",
    "RKLB": "rocketlabusa.com",
    "ASTS": "ast-science.com",
    "SATS": "echostar.com",
    "QNT":  "quantinuum.com",
    "HON":  "honeywell.com",
    "RDW":  "redwirespace.com",
    "FLY":  "fireflyspace.com",
    "SATL": "satellogic.com",
    "BKSY": "blacksky.com",
    "MDA":  "mda.space",
    "FTC":  "filtronic.com",
    "DXYZ": "destiny.xyz",
    "IRDM": "iridium.com",
    "VSAT": "viasat.com",
}

# Keywords that suggest the EDGAR path is preferred
EDGAR_KEYWORDS_RE = re.compile(
    r"\b(sec edgar|sec filing|edgar|8-?k|10-?q|10-?k|s-?1|424b|defa14a|13d|13g)\b",
    re.IGNORECASE,
)

# Heuristic form types to fetch based on claim type
EDGAR_FORM_HINTS = {
    "earnings": ["8-K", "10-Q", "10-K"],
    "revenue":  ["8-K", "10-Q", "10-K"],
    "guidance": ["8-K", "10-Q"],
    "ipo":      ["S-1", "424B4", "424B3", "424B2"],
    "merger":   ["8-K", "425", "S-4"],
    "contract": ["8-K"],
    "default":  ["8-K", "10-Q"],
}


def _detect_edgar_form_hint(claim: str) -> list:
    """Pick which form types to prioritise based on claim language."""
    text = claim.lower()
    if any(w in text for w in ("revenue", "earnings", "ebitda", "guidance")):
        return EDGAR_FORM_HINTS["revenue"]
    if "ipo" in text or "s-1" in text or "s1 filing" in text:
        return EDGAR_FORM_HINTS["ipo"]
    if "merger" in text or "acquisition" in text or "transaction" in text:
        return EDGAR_FORM_HINTS["merger"]
    if "contract" in text or "award" in text or "task order" in text:
        return EDGAR_FORM_HINTS["contract"]
    return EDGAR_FORM_HINTS["default"]


def _classify_strategy(claim: str, required_primary: str, ticker: str) -> dict:
    """
    Pick the fetch strategy. Returns dict with keys:
      route:  'edgar' | 'firecrawl' | 'both'
      form_types:    list of EDGAR form types if 'edgar' or 'both'
      search_queries: list of search query strings if 'firecrawl' or 'both'
      allowed_domains: list of preferred domains for Firecrawl
    """
    required = required_primary or ""
    edgar_signal = bool(EDGAR_KEYWORDS_RE.search(required)) or bool(EDGAR_KEYWORDS_RE.search(claim))

    queries = []
    # Query 1: ticker + claim shape (focused)
    queries.append(f"{ticker} {claim[:120]}")
    # Query 2: extract entity keywords + restate as primary search
    queries.append(f'{ticker} {_extract_query_keywords(claim)}')

    # Domain preference: Tier 1 base + issuer IR for this ticker
    allowed = list(TIER_1_DOMAINS)
    ir = ENTITY_IR_DOMAIN.get(ticker)
    if ir:
        allowed.append(ir)

    if edgar_signal:
        return {
            "route": "both",  # EDGAR primary, Firecrawl supplemental
            "form_types": _detect_edgar_form_hint(claim),
            "search_queries": queries[:MAX_FIRECRAWL_QUERIES_PER_CLAIM],
            "allowed_domains": allowed,
        }

    return {
        "route": "firecrawl",
        "form_types": [],
        "search_queries": queries[:MAX_FIRECRAWL_QUERIES_PER_CLAIM],
        "allowed_domains": allowed,
    }


def _extract_query_keywords(claim: str) -> str:
    """Pull a few high-signal keywords from the claim for a second query shape."""
    # Strip dollar signs, parentheses noise; keep entity references and event words
    text = re.sub(r"[\$\(\)\[\]\{\}]", " ", claim)
    # Take words longer than 3 chars, dedupe preserving order, cap to 6
    seen = []
    for word in re.findall(r"[A-Za-z][A-Za-z0-9\-]{3,}", text):
        if word.lower() not in {w.lower() for w in seen}:
            seen.append(word)
        if len(seen) >= 6:
            break
    return " ".join(seen)


# -----------------------------------------------------------------------------
# Fetch execution
# -----------------------------------------------------------------------------

def _classify_url_tier(url: str) -> int:
    """Best-effort tier classification by URL. Mirrors verifier_prompt.md heuristics."""
    if not url:
        return 4
    url_lower = url.lower()
    # Tier 1
    tier_1_patterns = [
        "sec.gov", "data.sec.gov",
        "nasa.gov", "fcc.gov", "faa.gov", "sda.mil", "defense.gov",
    ]
    # Issuer IR (treat any URL on the known IR domains as Tier 1)
    for ir_domain in ENTITY_IR_DOMAIN.values():
        if ir_domain.lower() in url_lower:
            return 1
    if any(p in url_lower for p in tier_1_patterns):
        return 1
    # Tier 2
    tier_2_patterns = ["reuters.com", "bloomberg.com", "ft.com", "wsj.com",
                       "spglobal.com", "barrons.com", "marketscreener.com",
                       "bnnbloomberg.ca"]
    if any(p in url_lower for p in tier_2_patterns):
        return 2
    # Tier 4 — social/encyclopaedic
    tier_4_patterns = ["wikipedia.org", "reddit.com", "stocktwits.com",
                       "facebook.com", "twitter.com", "x.com", "youtube.com"]
    if any(p in url_lower for p in tier_4_patterns):
        return 4
    # Default: Tier 3 (aggregators, contributor sites, generic news)
    return 3


async def _fetch_sources(strategy: dict, ticker: str) -> list:
    """
    Execute the strategy's fetches. Returns list of dicts:
        {url, source_label, tier, text, success}
    """
    # Lazy imports so DRY_RUN classification tests can run without these env vars
    from ares.lib import edgar
    from ares.lib import firecrawl_client

    out = []

    # EDGAR leg
    if strategy["route"] in ("edgar", "both"):
        try:
            cik = edgar.get_cik(ticker)
            if cik:
                filings = edgar.get_recent_filings(
                    cik,
                    form_types=strategy["form_types"],
                    days_back=90,
                    max_results=MAX_EDGAR_FILINGS_PER_CLAIM,
                )
                for f in filings:
                    try:
                        text = edgar.fetch_filing_text(
                            f["accession_number"],
                            f["primary_document"],
                            cik=cik,
                            max_chars=MAX_CHARS_PER_SOURCE,
                        )
                        url = (
                            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                            f"{f['accession_number'].replace('-', '')}/{f['primary_document']}"
                        )
                        out.append({
                            "url": url,
                            "source_label": f"SEC EDGAR {f['form']} ({f['filing_date']})",
                            "tier": 1,
                            "text": text,
                            "success": True,
                        })
                    except Exception as e:
                        logger.warning("EDGAR fetch_filing_text failed: %s", e)
            else:
                logger.warning("EDGAR: no CIK found for ticker %s", ticker)
        except Exception as e:
            logger.warning("EDGAR leg failed for %s: %s", ticker, e)

    # Firecrawl leg
    if strategy["route"] in ("firecrawl", "both"):
        try:
            scrapes_done = 0
            seen_urls = {s["url"] for s in out}
            for query in strategy["search_queries"]:
                if scrapes_done >= MAX_FIRECRAWL_SCRAPES_PER_CLAIM:
                    break
                results = firecrawl_client.search(query, max_results=6)
                # Prefer allowed domains
                allowed = strategy.get("allowed_domains") or []
                ranked = sorted(
                    results,
                    key=lambda r: 0 if any(d in r["url"].lower() for d in allowed) else 1,
                )
                for r in ranked:
                    if scrapes_done >= MAX_FIRECRAWL_SCRAPES_PER_CLAIM:
                        break
                    if r["url"] in seen_urls:
                        continue
                    seen_urls.add(r["url"])
                    scraped = firecrawl_client.scrape(
                        r["url"], max_chars=MAX_CHARS_PER_SOURCE
                    )
                    if scraped["success"] and scraped["markdown"]:
                        out.append({
                            "url": scraped["url"],
                            "source_label": r["title"] or scraped["url"],
                            "tier": _classify_url_tier(scraped["url"]),
                            "text": scraped["markdown"],
                            "success": True,
                        })
                        scrapes_done += 1
        except Exception as e:
            logger.warning("Firecrawl leg failed for %s: %s", ticker, e)

    return out


# -----------------------------------------------------------------------------
# Sonnet extraction
# -----------------------------------------------------------------------------

def _format_sources_for_prompt(sources: list) -> str:
    if not sources:
        return "(no sources fetched — verifier should return verdict=inconclusive, confidence=failed)"
    blocks = []
    for i, s in enumerate(sources, start=1):
        blocks.append(
            f"--- SOURCE {i} ---\n"
            f"URL: {s['url']}\n"
            f"Label: {s['source_label']}\n"
            f"Tier (URL-classified): {s['tier']}\n"
            f"Content:\n{s['text']}\n"
        )
    return "\n".join(blocks)


async def _extract_verdict(
    client: anthropic.AsyncAnthropic,
    claim: str,
    required_primary: str,
    sources: list,
) -> dict:
    """Call Sonnet to produce a structured verdict JSON."""
    user_message = (
        f"=== CLAIM UNDER INVESTIGATION ===\n{claim}\n\n"
        f"=== REQUIRED PRIMARY (what would settle this) ===\n{required_primary or 'unspecified'}\n\n"
        f"=== SOURCES FETCHED ===\n{_format_sources_for_prompt(sources)}\n\n"
        f"Render your JSON verdict per the system prompt."
    )

    try:
        response = await client.messages.create(
            model=EXTRACTION_MODEL,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=[{"type": "text", "text": SYSTEM_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_message}],
        )
    except anthropic.APIError as e:
        logger.error("Anthropic API error during verdict extraction: %s", e)
        return {
            "verdict": "inconclusive", "confidence": "failed",
            "source_tier_used": None, "sources_used": "",
            "summary": f"Anthropic API error: {e}",
            "evidence_quotes": [], "primary_source_needed": None,
            "notes_for_human": "Verifier could not call Anthropic API; retry later.",
        }

    raw = response.content[0].text.strip()
    # Strip code fences if Sonnet adds them
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("Verifier output is not valid JSON: %s. Raw: %s", e, raw[:500])
        return {
            "verdict": "inconclusive", "confidence": "failed",
            "source_tier_used": None, "sources_used": "",
            "summary": "Sonnet output failed JSON parse — verdict could not be extracted.",
            "evidence_quotes": [], "primary_source_needed": None,
            "notes_for_human": f"JSON parse failed. Raw output begins: {raw[:200]}",
        }


# -----------------------------------------------------------------------------
# Ledger update
# -----------------------------------------------------------------------------

def _should_auto_resolve(verdict: dict, sources: list) -> bool:
    """
    Auto-resolution gate: HIGH confidence + Tier 1 source actually used +
    unambiguous verdict (confirmed | denied).
    """
    if verdict.get("confidence") != "high":
        return False
    if verdict.get("verdict") not in ("confirmed", "denied"):
        return False
    tier = verdict.get("source_tier_used")
    if tier != 1:
        return False
    # And we actually have a Tier 1 source in our fetched pool
    if not any(s["tier"] == 1 and s["success"] for s in sources):
        return False
    return True


async def _update_ledger_row(
    pool: asyncpg.Pool,
    ledger_id: int,
    verdict: dict,
    sources: list,
    auto_resolve: bool,
    dry_run: bool,
) -> None:
    sources_used_str = verdict.get("sources_used") or ""
    evidence = "\n".join(verdict.get("evidence_quotes", []) or [])
    proposal = (
        f"VERDICT: {verdict.get('verdict', 'inconclusive').upper()} "
        f"(confidence: {verdict.get('confidence', 'failed')}, "
        f"tier used: {verdict.get('source_tier_used')})\n\n"
        f"Summary: {verdict.get('summary', '')}\n\n"
        f"Sources cited: {sources_used_str}"
    )
    notes_for_human = verdict.get("notes_for_human")
    if notes_for_human:
        proposal += f"\n\nNotes: {notes_for_human}"
    if verdict.get("primary_source_needed"):
        proposal += f"\n\nNext primary to fetch: {verdict['primary_source_needed']}"

    if dry_run:
        logger.info(
            "[DRY RUN] would update ledger row %d: auto_resolve=%s, verdict=%s",
            ledger_id, auto_resolve, verdict.get("verdict"),
        )
        logger.info("[DRY RUN] proposal:\n%s", proposal)
        return

    async with pool.acquire() as conn:
        if auto_resolve:
            verdict_field = verdict.get("verdict")
            await conn.execute("""
                UPDATE ares.ledger SET
                    verifier_attempted_at  = NOW(),
                    verifier_proposal      = $1,
                    verifier_confidence    = $2,
                    verifier_source_tier   = $3,
                    verifier_sources_used  = $4,
                    verifier_evidence      = $5,
                    verifier_auto_resolved = TRUE,
                    resolved               = TRUE,
                    resolution_date        = CURRENT_DATE,
                    resolution_summary     = $6,
                    resolution_verdict     = $7
                WHERE id = $8
            """,
                proposal,
                verdict.get("confidence"),
                verdict.get("source_tier_used"),
                sources_used_str,
                evidence,
                verdict.get("summary"),
                verdict_field,
                ledger_id,
            )
            logger.info("Auto-resolved ledger row %d (verdict=%s, tier=1, HIGH)",
                        ledger_id, verdict_field)
        else:
            await conn.execute("""
                UPDATE ares.ledger SET
                    verifier_attempted_at  = NOW(),
                    verifier_proposal      = $1,
                    verifier_confidence    = $2,
                    verifier_source_tier   = $3,
                    verifier_sources_used  = $4,
                    verifier_evidence      = $5,
                    verifier_auto_resolved = FALSE
                WHERE id = $6
            """,
                proposal,
                verdict.get("confidence"),
                verdict.get("source_tier_used"),
                sources_used_str,
                evidence,
                ledger_id,
            )
            logger.info(
                "Proposal written to ledger row %d (verdict=%s, confidence=%s, NOT auto-resolved)",
                ledger_id, verdict.get("verdict"), verdict.get("confidence"),
            )


# -----------------------------------------------------------------------------
# Main run
# -----------------------------------------------------------------------------

async def run_verifier():
    if not DATABASE_URL:
        logger.error("DATABASE_URL not set — aborting")
        return
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set — aborting")
        return

    if DRY_RUN:
        logger.info("VERIFIER_DRY_RUN=true — no ledger writes will be performed")
    if SINGLE_ROW_ID:
        logger.info("VERIFIER_LEDGER_ID=%s — single-row mode", SINGLE_ROW_ID)

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

    # Open audit row
    try:
        async with pool.acquire() as conn:
            job_run_id = await conn.fetchval("""
                INSERT INTO platform.job_runs (job_name, job_module, started_at, status)
                VALUES ('ares_verifier', 'ares.agents.verifier', NOW(), 'running')
                RETURNING id
            """)
    except asyncpg.PostgresError as e:
        logger.warning("Could not insert job_run row: %s", e)
        job_run_id = None

    # Pull PENDING items
    async with pool.acquire() as conn:
        if SINGLE_ROW_ID:
            rows = await conn.fetch("""
                SELECT id, entity_ticker, claim, required_primary
                FROM ares.ledger
                WHERE id = $1
                  AND system_recommendation = 'withhold'
                  AND NOT resolved
            """, int(SINGLE_ROW_ID))
        else:
            rows = await conn.fetch("""
                SELECT id, entity_ticker, claim, required_primary
                FROM ares.ledger
                WHERE system_recommendation = 'withhold'
                  AND NOT resolved
                  AND verifier_attempted_at IS NULL
                ORDER BY signal_date DESC, id DESC
                LIMIT $1
            """, MAX_ITEMS_PER_RUN)

    if not rows:
        logger.info("No PENDING items to process (LIMIT %d)", MAX_ITEMS_PER_RUN)
        if job_run_id is not None:
            async with pool.acquire() as conn:
                await conn.execute("""
                    UPDATE platform.job_runs
                    SET completed_at = NOW(), status = 'completed', records_processed = 0
                    WHERE id = $1
                """, job_run_id)
        await pool.close()
        await client.close()
        return

    logger.info("Processing %d PENDING item(s)", len(rows))

    auto_resolved_count = 0
    proposed_count = 0

    for row in rows:
        ledger_id = row["id"]
        ticker = row["entity_ticker"]
        claim = row["claim"]
        required = row["required_primary"] or ""

        logger.info("--- Processing row %d: %s ---", ledger_id, ticker)
        logger.info("Claim: %s", claim[:200])

        # 1. Classify
        strategy = _classify_strategy(claim, required, ticker)
        logger.info("Strategy: route=%s form_types=%s queries=%s",
                    strategy["route"], strategy["form_types"], strategy["search_queries"])

        # 2. Fetch
        sources = await _fetch_sources(strategy, ticker)
        logger.info("Fetched %d sources", len(sources))
        for s in sources:
            logger.info("  → tier=%d url=%s", s["tier"], s["url"][:100])

        # 3. Extract verdict
        verdict = await _extract_verdict(client, claim, required, sources)
        logger.info(
            "Verdict: %s (confidence=%s, tier=%s)",
            verdict.get("verdict"), verdict.get("confidence"),
            verdict.get("source_tier_used"),
        )

        # 4. Decide auto-resolve vs proposal
        auto_resolve = _should_auto_resolve(verdict, sources)

        # 5. Update ledger
        await _update_ledger_row(pool, ledger_id, verdict, sources, auto_resolve, DRY_RUN)

        if auto_resolve:
            auto_resolved_count += 1
        else:
            proposed_count += 1

    logger.info(
        "Verifier run complete: %d auto-resolved, %d proposed (PENDING human review)",
        auto_resolved_count, proposed_count,
    )

    if job_run_id is not None:
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE platform.job_runs
                SET completed_at = NOW(), status = 'completed', records_processed = $1
                WHERE id = $2
            """, len(rows), job_run_id)

    await pool.close()
    await client.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(run_verifier())
