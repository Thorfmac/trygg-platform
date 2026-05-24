"""
Trygg Ares — Feed Ingestion Agent
Scans GNews for signals on all active Ares watchlist equities.
Stores raw signals in ares.signals (pre-triage).
Also extracts dynamic candidates for watchlist consideration.

Schedule: 06:00 UTC daily
"""

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import asyncpg
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

GNEWS_API_KEY = os.getenv("GNEWS_API_KEY")
GNEWS_BASE    = "https://gnews.io/api/v4"
DATABASE_URL  = os.getenv("DATABASE_URL")

# Rate limit — GNews free tier: 100 req/day shared with Mímir
DELAY_BETWEEN_COMPANIES = 2.0  # seconds

# Company-specific query overrides for better signal quality
QUERY_OVERRIDES = {
    "RKLB": '"Rocket Lab" SpaceX OR IPO OR valuation OR launch',
    "PL":   '"Planet Labs" satellite OR SpaceX OR IPO OR valuation',
    "SPIR": '"Spire Global" satellite OR SpaceX OR space OR data',
    "ASTS": '"AST SpaceMobile" satellite OR SpaceX OR broadband OR LEO',
    "LUNR": '"Intuitive Machines" OR LUNR NASA OR lunar OR SpaceX',
    "MYNA": '"Mynaric" laser OR satellite OR SpaceX OR communications',
    "KTOS": '"Kratos Defense" satellite OR space OR defence OR SpaceX',
    "VSAT": '"Viasat" satellite OR SpaceX OR ground OR broadband',
}

# Candidate extraction — companies to watch for in article text
# If agent sees these mentioned alongside watchlist equities, flag them
CANDIDATE_WATCH_TERMS = [
    "Relativity Space", "Astra Space", "Virgin Orbit", "Momentus",
    "Satellogic", "BlackSky", "Terran Orbital", "York Space",
    "Ursa Major", "Launcher", "ABL Space", "Phantom Space",
]


def make_content_hash(title: str, url: str) -> str:
    """SHA-256 dedup hash — same as Mímir pattern."""
    return hashlib.sha256(f"{title}{url}".encode()).hexdigest()


def extract_candidates(text: str, source_url: str) -> list[dict]:
    """Check article text for unlisted candidate companies."""
    candidates = []
    text_lower = text.lower()
    for term in CANDIDATE_WATCH_TERMS:
        if term.lower() in text_lower:
            candidates.append({
                "name":            term,
                "mentioned_in":    source_url,
                "mention_context": f"Mentioned in Ares signal: {text[:200]}",
                "confidence":      6,
            })
    return candidates


async def fetch_gnews(
    session: aiohttp.ClientSession,
    query: str,
    max_results: int = 5,
) -> list[dict]:
    """Fetch articles from GNews API."""
    params = {
        "q":        query,
        "lang":     "en",
        "max":      max_results,
        "apikey":   GNEWS_API_KEY,
        "sortby":   "publishedAt",
    }
    try:
        async with session.get(
            f"{GNEWS_BASE}/search",
            params=params,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("articles", [])
            else:
                text = await resp.text()
                logger.warning(f"GNews {resp.status} for '{query}': {text[:200]}")
                return []
    except Exception as e:
        logger.error(f"GNews fetch error for '{query}': {e}")
        return []


async def store_signal(
    conn: asyncpg.Connection,
    equity_id: int,
    article: dict,
) -> bool:
    """Store a single signal. Returns True if new, False if duplicate."""
    title = article.get("title", "")
    url   = article.get("url", "")

    if not title or not url:
        return False

    content_hash = make_content_hash(title, url)

    # Parse published date
    published_at = None
    pub_str = article.get("publishedAt")
    if pub_str:
        try:
            published_at = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
        except ValueError:
            pass

    try:
        await conn.execute("""
            INSERT INTO ares.signals (
                equity_id, title, url, source, published_at,
                content_hash, created_at
            ) VALUES ($1,$2,$3,$4,$5,$6,NOW())
            ON CONFLICT (content_hash) DO NOTHING
        """,
            equity_id,
            title,
            url,
            article.get("source", {}).get("name"),
            published_at,
            content_hash,
        )
        return True
    except Exception as e:
        logger.error(f"Signal insert error: {e}")
        return False


async def store_candidate(
    conn: asyncpg.Connection,
    candidate: dict,
) -> None:
    """Store a dynamic candidate if not already flagged."""
    try:
        await conn.execute("""
            INSERT INTO ares.candidates (
                name, mentioned_in, mention_context, confidence, flagged_at
            ) VALUES ($1,$2,$3,$4,NOW())
            ON CONFLICT DO NOTHING
        """,
            candidate["name"],
            candidate["mentioned_in"],
            candidate["mention_context"],
            candidate["confidence"],
        )
    except Exception as e:
        logger.debug(f"Candidate insert skipped (likely duplicate): {e}")


async def ingest_equity(
    session: aiohttp.ClientSession,
    pool: asyncpg.Pool,
    equity: dict,
) -> tuple[int, int]:
    """Ingest signals for one equity. Returns (new_signals, duplicates)."""
    ticker    = equity["ticker"]
    equity_id = equity["id"]
    name      = equity["name"]

    query = QUERY_OVERRIDES.get(ticker, f'"{name}" SpaceX OR satellite OR space OR IPO')
    logger.info(f"Ingesting {ticker}: {query}")

    articles = await fetch_gnews(session, query, max_results=5)

    if not articles:
        logger.warning(f"No articles returned for {ticker}")
        return 0, 0

    new_count = 0
    dup_count = 0
    all_candidates = []

    async with pool.acquire() as conn:
        for article in articles:
            is_new = await store_signal(conn, equity_id, article)
            if is_new:
                new_count += 1
                # Extract candidates from new articles only
                content = article.get("content", "") + article.get("description", "")
                candidates = extract_candidates(content, article.get("url", ""))
                all_candidates.extend(candidates)
            else:
                dup_count += 1

        # Store any candidates found
        for candidate in all_candidates:
            await store_candidate(conn, candidate)

    logger.info(f"  {ticker}: {new_count} new, {dup_count} duplicates")
    return new_count, dup_count


async def run_feed_ingestion():
    """Main entry point — ingest all active watchlist equities."""
    if not GNEWS_API_KEY:
        logger.error("GNEWS_API_KEY not set in .env — aborting")
        return

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)

    async with pool.acquire() as conn:
        equities = await conn.fetch("""
            SELECT e.id, e.name, e.ticker, s.name AS sector_name, s.order_level
            FROM ares.equities e
            JOIN ares.sectors s ON s.id = e.sector_id
            WHERE e.is_active = TRUE AND e.ticker IS NOT NULL
            ORDER BY s.order_level, e.name
        """)

    if not equities:
        logger.warning("No active equities found")
        await pool.close()
        return

    logger.info(f"Ingesting signals for {len(equities)} equities...")

    total_new = 0
    total_dup = 0

    async with aiohttp.ClientSession() as session:
        for equity in equities:
            new, dup = await ingest_equity(session, pool, dict(equity))
            total_new += new
            total_dup += dup
            await asyncio.sleep(DELAY_BETWEEN_COMPANIES)

    # Log summary
    async with pool.acquire() as conn:
        untriaged = await conn.fetchval("""
            SELECT COUNT(*) FROM ares.signals
            WHERE relevance_score IS NULL
        """)
        candidates = await conn.fetchval("""
            SELECT COUNT(*) FROM ares.candidates
            WHERE reviewed = FALSE
        """)

    await pool.close()
    logger.info(
        f"Feed ingestion complete — {total_new} new signals, "
        f"{total_dup} duplicates, {untriaged} awaiting triage, "
        f"{candidates} candidates flagged"
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(run_feed_ingestion())
