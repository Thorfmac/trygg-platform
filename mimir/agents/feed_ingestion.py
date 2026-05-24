# =============================================================
# mimir/agents/feed_ingestion.py
# =============================================================
# Switched from NewsAPI to GNews — GNews allows server-side
# requests on free tier. NewsAPI free tier blocks them.
# =============================================================

import hashlib
import logging
from datetime import datetime, timezone, timedelta

import requests

from core.config import Config
from core.database import (
    get_active_companies,
    signal_exists,
    insert_signal,
    start_job_run,
    complete_job_run,
    fail_job_run,
)

logger = logging.getLogger(__name__)

LOOKBACK_HOURS = 7


def run(config: Config) -> dict:
    job_id = start_job_run("mimir.feed_ingestion", "mimir")
    try:
        companies = get_active_companies()
        logger.info(f"Scanning feeds for {len(companies)} companies")

        total_new = 0
        total_duplicate = 0
        company_results = []

        import time
        for company in companies:
            result = _scan_company(company, config)
            time.sleep(1.5)  # respect GNews rate limit (1 req/sec free tier)
            total_new += result["new"]
            total_duplicate += result["duplicates"]
            company_results.append(result)
            logger.info(
                f"  {company['name']}: "
                f"{result['new']} new, {result['duplicates']} duplicates"
            )

        summary = {
            "companies_scanned": len(companies),
            "signals_new": total_new,
            "signals_duplicate": total_duplicate,
            "company_detail": company_results,
        }

        complete_job_run(job_id, records_processed=total_new, metadata=summary)
        logger.info(
            f"Feed ingestion complete — "
            f"{total_new} new signals across {len(companies)} companies"
        )
        return summary

    except Exception as e:
        fail_job_run(job_id, str(e))
        raise


def _scan_company(company: dict, config: Config) -> dict:
    company_id = str(company["id"])
    company_name = company["name"]
    search_query = _build_search_query(company)
    articles = _fetch_news(search_query, config)

    new_count = 0
    duplicate_count = 0

    for article in articles:
        content_hash = _hash_article(article)

        if signal_exists(content_hash):
            duplicate_count += 1
            continue

        signal = {
            "company_id": company_id,
            "signal_type": _classify_signal_type(article),
            "headline": article.get("title", "")[:500],
            "summary": article.get("description"),
            "source_url": article.get("url"),
            "source_name": article.get("source", {}).get("name"),
            "published_at": _parse_date(article.get("publishedAt")),
            "content_hash": content_hash,
            "metadata": {
                "raw_title": article.get("title"),
                "author": article.get("author"),
                "search_query": search_query,
            },
        }

        insert_signal(signal)
        new_count += 1

    return {
        "company_id": company_id,
        "company_name": company_name,
        "new": new_count,
        "duplicates": duplicate_count,
    }


def _fetch_news(query: str, config: Config) -> list[dict]:
    """
    Call the GNews API — allows server-side requests on free tier.
    Free tier: 100 requests/day, up to 10 articles per request.
    """
    try:
        response = requests.get(
            "https://gnews.io/api/v4/search",
            params={
                "q": query,
                "lang": "en",
                "max": 10,
                "sortby": "publishedAt",
                "apikey": config.gnews_api_key,
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()

        if "errors" in data:
            logger.warning(f"GNews error for '{query}': {data['errors']}")
            return []

        # Map GNews fields to match our article processing code
        articles = []
        for item in data.get("articles", []):
            articles.append({
                "title": item.get("title"),
                "description": item.get("description"),
                "url": item.get("url"),
                "source": {"name": item.get("source", {}).get("name")},
                "publishedAt": item.get("publishedAt"),
                "author": None,
            })
        return articles

    except requests.exceptions.Timeout:
        logger.warning(f"GNews timed out for query: '{query}'")
        return []
    except requests.exceptions.RequestException as e:
        logger.error(f"GNews request failed for '{query}': {e}")
        return []


def _build_search_query(company: dict) -> str:
    """Broader queries to catch niche company coverage."""
    slug = company.get("slug", "")

    overrides = {
        "pqshield":          "PQShield quantum cryptography",
        "isara":             "ISARA quantum cryptography standards",
        "crypto4a":          "quantum cryptography hardware security module",
        "evolutionq":        "quantum cryptography network security",
        "crypto-quantique":  "quantum IoT security semiconductor",
        "xiphera":           "FPGA cryptography hardware security",
        "sealsq":            "SEALSQ quantum semiconductor LAES",
        "quantinuum":        "Quantinuum quantum computing IPO",
    }

    if slug in overrides:
        return overrides[slug]

    return company["name"]


def _classify_signal_type(article: dict) -> str:
    headline = (article.get("title") or "").lower()

    if any(w in headline for w in ["fund", "raises", "series", "investment", "million", "capital"]):
        return "funding_round"
    if any(w in headline for w in ["ipo", "listing", "public offering", "nasdaq", "nyse", "lse"]):
        return "ipo_signal"
    if any(w in headline for w in ["patent", "ip ", "intellectual property"]):
        return "patent"
    if any(w in headline for w in ["appoints", "names", "ceo", "cto", "ciso", "joins", "leaves"]):
        return "leadership_change"
    if any(w in headline for w in ["partner", "agreement", "contract", "deal"]):
        return "partnership"
    if any(w in headline for w in ["launch", "releases", "announces", "product"]):
        return "product_launch"

    return "news"


def _hash_article(article: dict) -> str:
    title = (article.get("title") or "").strip().lower()
    url = (article.get("url") or "").strip()
    raw = f"{title}|{url}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _parse_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except ValueError:
        return None