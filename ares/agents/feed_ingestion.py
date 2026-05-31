"""
Trygg Ares — Feed Ingestion Agent
=================================

Scans GNews for signals on all active Ares universe entities and writes to
ares.signals with deterministic source-tier classification at insert time.

Discipline applied per Source-Authority Spec v1:
- source_tier classified at insert (NOT NULL constraint on ares.signals)
- Default tier is 4 (conservative) — unknown sources cap relevance via triage
- Deduplication via SHA-256(headline + url) → body_hash UNIQUE constraint
- Tier 6 (off-universe) entities are NOT scanned — they don't burn API quota

Per Universe Doc v5:
- Active (Tier 1-2), conditional (Tier 3), catalyst-trade (Tier 4) and
  watchlist (Tier 5) entities scanned
- Per-entity query overrides disambiguate generic tickers (SATS, FLY, etc.)

Schedule via scheduler.py — runs once daily at 06:00 UTC.
"""

import asyncio
import hashlib
import logging
import os
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import aiohttp
import asyncpg
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL")
GNEWS_API_KEY = os.getenv("GNEWS_API_KEY")
GNEWS_BASE = "https://gnews.io/api/v4"

DELAY_BETWEEN_ENTITIES = 1.5  # seconds — be polite to GNews rate limit
MAX_RESULTS_PER_ENTITY = 5    # GNews articles per entity per run
REQUEST_TIMEOUT = 10          # seconds


# -----------------------------------------------------------------------------
# Per-entity query overrides
# -----------------------------------------------------------------------------
# Generic tickers and ambiguous names need disambiguating context. The pattern
# is to wrap the full name in quotes and add 2-3 contextual terms that pull
# the right signal noise floor.
# -----------------------------------------------------------------------------

ENTITY_QUERIES = {
    # Tier 1 — conviction equity positions
    "LUNR":   '"Intuitive Machines" lunar OR Artemis OR NASA OR Goonhilly OR earnings',
    "SATS":   '"EchoStar" SpaceX OR AT T OR spectrum OR Boost',
    "UFO":    '"Procure Space ETF" OR "space sector ETF"',
    "QTUM":   '"Defiance Quantum" OR "quantum computing ETF"',

    # Tier 2 — CSP entries
    "IONQ":   'IonQ quantum OR earnings OR "256-qubit"',

    # Tier 3 — conditional
    "RKLB":   '"Rocket Lab" Neutron OR Electron OR earnings OR SDA',
    "QNT":    'Quantinuum IPO OR listing OR quantum',

    # Tier 4 — catalyst trade
    "SPCX":   'SpaceX IPO OR S1 OR roadshow OR lockup OR Starlink',

    # Tier 5 — watchlist
    "ASTS":   '"AST SpaceMobile" BlueBird OR satellite OR Verizon OR "Blue Origin"',
    "RDW":    '"Redwire" space OR satellite OR "Edge Autonomy" OR SciTec',
    "FLY":    '"Firefly Aerospace" launch OR lunar OR "Blue Ghost" OR earnings',
    "SATL":   '"Satellogic" earth observation OR satellite',
    "BKSY":   '"BlackSky" earth observation OR satellite OR defence',
    "MDA.TO": '"MDA Space" satellite OR Canadarm OR earnings',
    "FTC.L":  '"Filtronic" satellite OR communications OR earnings',
}


# -----------------------------------------------------------------------------
# Source-tier classification
# -----------------------------------------------------------------------------
# Domain-based classification per Source-Authority Spec v1.
# The LLM in triage can elevate authority_tier when corroboration is found;
# source_tier itself is mechanical.
# -----------------------------------------------------------------------------

TIER_1_DOMAINS = frozenset({
    # SEC and US federal regulators
    "sec.gov", "edgar.sec.gov",
    "fcc.gov", "faa.gov", "nist.gov", "nasa.gov",
    "federalreserve.gov",
    # UK and EU regulators
    "fca.org.uk", "esma.europa.eu", "bankofengland.co.uk",
    "companieshouse.gov.uk",
    # Issuer press release wires (treat as Tier 1 when conveying issuer's own release)
    "globenewswire.com", "prnewswire.com", "businesswire.com",
})

# Patterns like investors.lunr.com or ir.echostar.com → Tier 1 (issuer IR pages)
TIER_1_PATTERNS = [
    re.compile(r"^investors\..+$"),
    re.compile(r"^ir\..+$"),
]

TIER_2_DOMAINS = frozenset({
    # Wire services and majors
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com",
    # Ratings and analysis
    "spglobal.com", "moodys.com", "fitchratings.com",
    # Financial-data aggregators with disclosed methodology
    "stockanalysis.com", "marketscreener.com", "simplywall.st",
    "crunchbase.com", "pitchbook.com",
    # Established trade press
    "spacenews.com", "viasatellite.com", "satellitetoday.com",
    "spacepolicyonline.com", "viaspace.com", "thespacereview.com",
    # Transcript aggregators (sourced from primary calls)
    "quartr.com",
})

TIER_3_DOMAINS = frozenset({
    "cnbc.com", "marketwatch.com",
    "fool.com", "motleyfool.com",
    "seekingalpha.com", "forbes.com", "benzinga.com",
    "yahoo.com", "finance.yahoo.com", "uk.yahoo.com",
    "investing.com", "tipranks.com",
    "investorplace.com", "zacks.com",
    "thestreet.com", "barrons.com",
})

TIER_4_DOMAINS = frozenset({
    "stocktwits.com", "reddit.com",
    "twitter.com", "x.com",
    "medium.com",  # mixed quality; default down
})


def normalize_domain(url: str) -> str:
    """Extract a normalised domain string from a URL."""
    try:
        domain = urlparse(url).netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return ""


def classify_source_tier(url: str) -> int:
    """Classify URL into source tier 1-4. Defaults to 4 (lowest, conservative)."""
    domain = normalize_domain(url)
    if not domain:
        return 4

    if domain in TIER_1_DOMAINS:
        return 1
    for pattern in TIER_1_PATTERNS:
        if pattern.match(domain):
            return 1

    if domain in TIER_2_DOMAINS:
        return 2
    if domain in TIER_3_DOMAINS:
        return 3
    if domain in TIER_4_DOMAINS:
        return 4

    return 4  # Conservative default for unknown domains


# -----------------------------------------------------------------------------
# Deduplication
# -----------------------------------------------------------------------------

def make_body_hash(headline: str, url: str) -> str:
    """SHA-256 hash for dedup. Matches Mímir's pattern."""
    payload = f"{headline}|{url}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# -----------------------------------------------------------------------------
# GNews fetch
# -----------------------------------------------------------------------------

async def fetch_gnews(
    session: aiohttp.ClientSession,
    query: str,
    max_results: int = MAX_RESULTS_PER_ENTITY,
) -> list[dict]:
    """Fetch articles from GNews API. Returns [] on any error."""
    params = {
        "q": query,
        "lang": "en",
        "max": max_results,
        "apikey": GNEWS_API_KEY,
    }
    try:
        async with session.get(
            f"{GNEWS_BASE}/search",
            params=params,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as resp:
            if resp.status == 429:
                logger.warning("GNews rate-limited (429) — aborting remaining queries")
                raise asyncio.CancelledError("GNews quota exhausted")
            if resp.status != 200:
                logger.warning("GNews status %d for query: %s", resp.status, query)
                return []
            data = await resp.json()
            return data.get("articles", [])
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        logger.warning("GNews timeout for query: %s", query)
        return []
    except Exception as e:
        logger.error("GNews error for query '%s': %s", query, e)
        return []


# -----------------------------------------------------------------------------
# Per-entity ingestion
# -----------------------------------------------------------------------------

async def ingest_entity(
    session: aiohttp.ClientSession,
    pool: asyncpg.Pool,
    entity: dict,
) -> tuple[int, int]:
    """Scan GNews for one entity. Returns (new_inserts, duplicate_skips)."""
    ticker = entity["ticker"]
    query = ENTITY_QUERIES.get(ticker)

    if not query:
        # Default: use the entity name directly
        query = f'"{entity["name"]}"'
        logger.info("No override for %s — using default query: %s", ticker, query)

    articles = await fetch_gnews(session, query)

    if not articles:
        return 0, 0

    new_count = 0
    dup_count = 0

    for article in articles:
        headline = (article.get("title") or "").strip()
        url = (article.get("url") or "").strip()
        source = (article.get("source") or {}).get("name") or normalize_domain(url)
        body = (article.get("description") or "").strip()
        published_at_str = article.get("publishedAt", "")

        if not headline or not url:
            continue

        body_hash = make_body_hash(headline, url)
        source_tier = classify_source_tier(url)

        # Parse published_at if present
        published_at: Optional[datetime] = None
        if published_at_str:
            try:
                published_at = datetime.fromisoformat(
                    published_at_str.replace("Z", "+00:00")
                )
            except ValueError:
                pass

        try:
            async with pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO ares.signals (
                        entity_id, headline, url, source, body, body_hash,
                        source_tier, published_at, fetched_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
                """,
                    entity["id"],
                    headline,
                    url,
                    source,
                    body,
                    body_hash,
                    source_tier,
                    published_at,
                )
                new_count += 1
        except asyncpg.UniqueViolationError:
            dup_count += 1
        except Exception as e:
            logger.error("Insert failed for %s — %s: %s", ticker, headline[:80], e)

    return new_count, dup_count


# -----------------------------------------------------------------------------
# Main entry point
# -----------------------------------------------------------------------------

async def run_feed_ingestion():
    """Scan all active Ares entities. Excludes off-universe (Tier 6)."""
    if not GNEWS_API_KEY:
        logger.error("GNEWS_API_KEY not set in .env — aborting")
        return
    if not DATABASE_URL:
        logger.error("DATABASE_URL not set in .env — aborting")
        return

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)

    # Open job_run audit row
    try:
        async with pool.acquire() as conn:
            job_run_id = await conn.fetchval("""
                INSERT INTO platform.job_runs (job_name, job_module, started_at, status)
                VALUES ('ares_feed_ingestion', 'ares.agents.feed_ingestion',
                        NOW(), 'running')
                RETURNING id
            """)
    except asyncpg.PostgresError as e:
        logger.warning(
            "Could not insert job_run row: %s. Continuing without audit row.", e
        )
        job_run_id = None

    # Pull active entities. Universe_status filter excludes 'off_universe'.
    async with pool.acquire() as conn:
        entities = await conn.fetch("""
            SELECT id, ticker, name, sector, universe_tier
            FROM ares.entities
            WHERE universe_status IN ('active', 'watchlist', 'deferred')
            ORDER BY universe_tier, ticker
        """)

    if not entities:
        logger.warning("No active entities to scan — nothing to do")
        await pool.close()
        return

    logger.info("Scanning %d active Ares entities...", len(entities))

    total_new = 0
    total_dup = 0

    try:
        async with aiohttp.ClientSession() as session:
            for entity in entities:
                entity_dict = dict(entity)
                new, dup = await ingest_entity(session, pool, entity_dict)
                total_new += new
                total_dup += dup
                logger.info(
                    "  %-7s tier=%d: %d new, %d dup",
                    entity_dict["ticker"],
                    entity_dict["universe_tier"],
                    new,
                    dup,
                )
                await asyncio.sleep(DELAY_BETWEEN_ENTITIES)
    except asyncio.CancelledError:
        logger.warning("Ingestion aborted early due to rate limit")

    # Close job_run audit row
    if job_run_id is not None:
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE platform.job_runs
                SET completed_at = NOW(),
                    status = 'completed',
                    records_processed = $1
                WHERE id = $2
            """, total_new, job_run_id)

    # Final summary including untriaged count
    async with pool.acquire() as conn:
        untriaged = await conn.fetchval("""
            SELECT COUNT(*) FROM ares.signals WHERE relevance_score IS NULL
        """)

    await pool.close()

    logger.info(
        "Feed ingestion complete — %d new, %d dup, %d untriaged awaiting triage",
        total_new, total_dup, untriaged
    )


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(run_feed_ingestion())
