"""
Trygg Ares — Financial Snapshot Agent
Pulls daily price, P/S, P/E, and revenue data from FMP stable API.
Stores in ares.equity_snapshots.

Schedule: 06:15 UTC daily
"""

import asyncio
import json
import logging
import os
from datetime import date
from typing import Optional

import aiohttp
import asyncpg
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

FMP_API_KEY  = os.getenv("FMP_API_KEY")
FMP_BASE     = "https://financialmodelingprep.com/stable"
DATABASE_URL = os.getenv("DATABASE_URL")


async def fetch_json(session: aiohttp.ClientSession, url: str) -> Optional[dict]:
    """Fetch JSON from FMP. Returns None on error."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                text = await resp.text()
                logger.warning(f"FMP {resp.status} for {url}: {text[:200]}")
                return None
    except Exception as e:
        logger.error(f"FMP fetch error for {url}: {e}")
        return None


async def get_quote(session: aiohttp.ClientSession, ticker: str) -> Optional[dict]:
    """Get current price, market cap via stable quote endpoint."""
    url = f"{FMP_BASE}/quote?symbol={ticker}&apikey={FMP_API_KEY}"
    data = await fetch_json(session, url)
    if isinstance(data, list) and data:
        return data[0]
    if isinstance(data, dict) and data:
        return data
    return None


async def get_key_metrics(session: aiohttp.ClientSession, ticker: str) -> Optional[dict]:
    """Get TTM key metrics including P/S and P/E ratios."""
    url = f"{FMP_BASE}/key-metrics-ttm?symbol={ticker}&apikey={FMP_API_KEY}"
    data = await fetch_json(session, url)
    if isinstance(data, list) and data:
        return data[0]
    if isinstance(data, dict) and data:
        return data
    return None


async def get_income(session: aiohttp.ClientSession, ticker: str) -> Optional[list]:
    """Get latest two annual income statements for revenue and YoY growth."""
    url = f"{FMP_BASE}/income-statement?symbol={ticker}&limit=2&apikey={FMP_API_KEY}"
    data = await fetch_json(session, url)
    if isinstance(data, list) and data:
        return data
    return None


def calculate_yoy_growth(statements: list) -> Optional[float]:
    """Calculate YoY revenue growth from two income statements."""
    try:
        if len(statements) >= 2:
            current  = statements[0].get("revenue", 0) or 0
            previous = statements[1].get("revenue", 0) or 0
            if previous > 0:
                return round(((current - previous) / previous) * 100, 2)
    except (KeyError, TypeError, ZeroDivisionError):
        pass
    return None


async def snapshot_equity(
    session: aiohttp.ClientSession,
    pool: asyncpg.Pool,
    equity: dict,
) -> bool:
    """Fetch and store snapshot for a single equity. Returns True on success."""
    ticker    = equity["ticker"]
    equity_id = equity["id"]
    today     = date.today()

    logger.info(f"Snapshotting {ticker}...")

    # Fetch all data concurrently
    quote_data, metrics_data, income_data = await asyncio.gather(
        get_quote(session, ticker),
        get_key_metrics(session, ticker),
        get_income(session, ticker),
    )

    if not quote_data:
        logger.warning(f"No quote data for {ticker} — skipping")
        return False

    # Extract values from quote
    price      = quote_data.get("price")
    market_cap = quote_data.get("marketCap")

    # Extract ratios from key metrics TTM
    ps_ratio = None
    pe_ratio = None
    if metrics_data:
        ps_ratio = metrics_data.get("priceToSalesRatioTTM") or metrics_data.get("priceToSalesTTM")
        pe_ratio = metrics_data.get("peRatioTTM") or metrics_data.get("priceEarningsRatioTTM")

    # Revenue and YoY growth from income statements
    revenue_ttm    = None
    yoy_growth_pct = None
    if income_data:
        revenue_ttm    = income_data[0].get("revenue")
        yoy_growth_pct = calculate_yoy_growth(income_data)

    # Build raw data blob
    raw_data = {
        "quote":   quote_data,
        "metrics": metrics_data,
        "income":  income_data[0] if income_data else None,
    }

    # Upsert — one snapshot per equity per day
    async with pool.acquire() as conn:
        try:
            await conn.execute("""
                INSERT INTO ares.equity_snapshots (
                    equity_id, snapshot_date, price, market_cap,
                    ps_ratio, pe_ratio, sector_ps_median,
                    revenue_ttm, yoy_growth_pct, raw_data
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                ON CONFLICT (equity_id, snapshot_date) DO UPDATE SET
                    price            = EXCLUDED.price,
                    market_cap       = EXCLUDED.market_cap,
                    ps_ratio         = EXCLUDED.ps_ratio,
                    pe_ratio         = EXCLUDED.pe_ratio,
                    revenue_ttm      = EXCLUDED.revenue_ttm,
                    yoy_growth_pct   = EXCLUDED.yoy_growth_pct,
                    raw_data         = EXCLUDED.raw_data
            """,
                equity_id, today, price, market_cap,
                ps_ratio, pe_ratio, None,  # sector_ps_median — not available on Starter
                revenue_ttm, yoy_growth_pct,
                json.dumps(raw_data),
            )
            rev_str = f"${revenue_ttm:,}" if revenue_ttm else "n/a"
            logger.info(f"  {ticker}: price=${price}, P/S={ps_ratio}, rev_ttm={rev_str}")
            return True
        except Exception as e:
            logger.error(f"DB insert failed for {ticker}: {e}")
            return False


async def run_financial_snapshot():
    """Main entry point — snapshot all active public equities."""
    if not FMP_API_KEY:
        logger.error("FMP_API_KEY not set in .env — aborting")
        return

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)

    async with pool.acquire() as conn:
        equities = await conn.fetch("""
            SELECT e.id, e.name, e.ticker, e.exchange, e.country,
                   s.name AS sector_name, s.order_level
            FROM ares.equities e
            JOIN ares.sectors s ON s.id = e.sector_id
            WHERE e.is_active = TRUE AND e.is_public = TRUE AND e.ticker IS NOT NULL
            ORDER BY s.order_level, e.name
        """)

    if not equities:
        logger.warning("No active public equities found in ares.equities")
        await pool.close()
        return

    logger.info(f"Snapshotting {len(equities)} equities...")

    success = 0
    failed  = 0

    async with aiohttp.ClientSession() as session:
        for equity in equities:
            ok = await snapshot_equity(session, pool, dict(equity))
            if ok:
                success += 1
            else:
                failed += 1
            await asyncio.sleep(1.0)

    await pool.close()
    logger.info(f"Financial snapshot complete — {success} succeeded, {failed} failed")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(run_financial_snapshot())
