"""
SEC EDGAR fetcher
=================

Thin wrapper around the SEC EDGAR public JSON API for use by the verifier agent.

Three core capabilities:
  - get_cik(ticker)              → ticker-to-CIK lookup
  - get_recent_filings(cik, ...) → list filings filtered by form type and freshness
  - fetch_filing_text(...)        → pull primary document text from a filing

SEC compliance notes:
  - Every request MUST include a User-Agent header identifying the caller
    (set via SEC_EDGAR_USER_AGENT env var).
  - Rate limit: 10 requests/second per the SEC EDGAR fair-access policy.
  - All endpoints documented at https://www.sec.gov/os/accessing-edgar-data.

Typical usage:
    from ares.lib.edgar import get_cik, get_recent_filings, fetch_filing_text

    cik = get_cik("IONQ")
    filings = get_recent_filings(cik, form_types=["8-K", "10-Q"], days_back=30)
    for f in filings:
        text = fetch_filing_text(f["accession_number"], f["primary_document"])
        ...
"""

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

USER_AGENT = os.getenv("SEC_EDGAR_USER_AGENT", "")
if not USER_AGENT:
    raise RuntimeError(
        "SEC_EDGAR_USER_AGENT env var is required. SEC EDGAR rejects requests "
        "without a real User-Agent identifying the caller. Format: 'App Name email@domain'"
    )

BASE_DATA_URL = "https://data.sec.gov"
BASE_WWW_URL = "https://www.sec.gov"
TICKERS_INDEX_URL = f"{BASE_WWW_URL}/files/company_tickers.json"

# SEC rate limit is 10 req/sec; we leave headroom at 5/sec.
REQUEST_DELAY_SECONDS = 0.2

# Default request timeout
REQUEST_TIMEOUT = 15


def _headers() -> dict:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/html",
        "Accept-Encoding": "gzip, deflate",
        "Host": "",  # filled per-request below
    }


def _get(url: str) -> requests.Response:
    """Rate-limited GET with proper Host header per URL."""
    headers = _headers()
    headers["Host"] = url.split("/")[2]  # e.g. 'data.sec.gov' or 'www.sec.gov'
    time.sleep(REQUEST_DELAY_SECONDS)
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp


# -----------------------------------------------------------------------------
# Ticker -> CIK lookup (cached for the lifetime of the process)
# -----------------------------------------------------------------------------

_TICKERS_CACHE: Optional[dict] = None
_TICKERS_CACHE_LOADED_AT: Optional[datetime] = None
_TICKERS_CACHE_TTL_HOURS = 24


def _load_tickers_index() -> dict:
    """Fetch and cache the full ticker-to-CIK index from SEC."""
    global _TICKERS_CACHE, _TICKERS_CACHE_LOADED_AT

    now = datetime.now(timezone.utc)
    if (
        _TICKERS_CACHE is not None
        and _TICKERS_CACHE_LOADED_AT is not None
        and (now - _TICKERS_CACHE_LOADED_AT).total_seconds() < _TICKERS_CACHE_TTL_HOURS * 3600
    ):
        return _TICKERS_CACHE

    logger.info("Loading SEC ticker index (this is cached for 24h)")
    resp = _get(TICKERS_INDEX_URL)
    raw = resp.json()
    # raw is { "0": {cik_str, ticker, title}, "1": {...}, ... }
    # Re-key by ticker for O(1) lookup
    by_ticker = {v["ticker"].upper(): v for v in raw.values()}
    _TICKERS_CACHE = by_ticker
    _TICKERS_CACHE_LOADED_AT = now
    logger.info("SEC ticker index loaded: %d tickers", len(by_ticker))
    return by_ticker


def get_cik(ticker: str) -> Optional[str]:
    """
    Look up the 10-digit CIK for a ticker symbol.

    Returns the zero-padded 10-digit CIK string (e.g., '0001824920' for IONQ),
    or None if not found.
    """
    if not ticker:
        return None
    index = _load_tickers_index()
    entry = index.get(ticker.upper())
    if not entry:
        logger.warning("Ticker not found in SEC index: %s", ticker)
        return None
    return f"{entry['cik_str']:010d}"


def get_company_name(ticker: str) -> Optional[str]:
    """Look up the registered company name for a ticker. Useful for sanity checks."""
    index = _load_tickers_index()
    entry = index.get(ticker.upper())
    return entry["title"] if entry else None


# -----------------------------------------------------------------------------
# Filings lookup
# -----------------------------------------------------------------------------

def get_recent_filings(
    cik: str,
    form_types: Optional[list] = None,
    days_back: int = 30,
    max_results: int = 50,
) -> list:
    """
    Retrieve recent filings for a CIK, optionally filtered by form type and freshness.

    Args:
        cik: 10-digit zero-padded CIK string
        form_types: list of form types to include (e.g., ['8-K', '10-Q', '10-K', 'S-1'])
                    If None, includes all forms.
        days_back: only include filings within this many days from today
        max_results: cap on number of results

    Returns:
        List of dicts: {accession_number, form, filing_date, primary_document, primary_doc_description, reporting_period}
        Sorted newest first.
    """
    url = f"{BASE_DATA_URL}/submissions/CIK{cik}.json"
    resp = _get(url)
    data = resp.json()

    recent = data.get("filings", {}).get("recent", {})
    if not recent:
        return []

    accessions = recent.get("accessionNumber", [])
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    primary_docs = recent.get("primaryDocument", [])
    descriptions = recent.get("primaryDocDescription", [])
    periods = recent.get("reportDate", [])

    cutoff_date = datetime.now(timezone.utc).date() - timedelta(days=days_back)
    form_filter = set(f.upper() for f in form_types) if form_types else None

    results = []
    for i in range(len(accessions)):
        try:
            filing_date = datetime.strptime(dates[i], "%Y-%m-%d").date()
        except (ValueError, IndexError):
            continue
        if filing_date < cutoff_date:
            continue
        form = forms[i].upper() if i < len(forms) else ""
        if form_filter and form not in form_filter:
            continue

        results.append({
            "accession_number": accessions[i],
            "form": form,
            "filing_date": dates[i],
            "primary_document": primary_docs[i] if i < len(primary_docs) else "",
            "primary_doc_description": descriptions[i] if i < len(descriptions) else "",
            "reporting_period": periods[i] if i < len(periods) else "",
            "cik": cik,
        })

        if len(results) >= max_results:
            break

    return results


# -----------------------------------------------------------------------------
# Filing document fetch
# -----------------------------------------------------------------------------

def _accession_to_path(accession_number: str) -> str:
    """Convert '0001824920-26-000017' to '000182492026000017' (no dashes) for URL path."""
    return accession_number.replace("-", "")


def fetch_filing_text(
    accession_number: str,
    primary_document: str,
    cik: str,
    max_chars: int = 100_000,
) -> str:
    """
    Fetch the primary document of a filing and return cleaned text.

    URL pattern is:
        https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_path}/{primary_document}

    Note: CIK in the path is the integer form (no leading zeros), unlike the
    /submissions/ endpoint which uses zero-padded.
    """
    cik_int = int(cik)
    accession_path = _accession_to_path(accession_number)
    url = (
        f"{BASE_WWW_URL}/Archives/edgar/data/{cik_int}/"
        f"{accession_path}/{primary_document}"
    )
    logger.info("Fetching filing document: %s", url)
    resp = _get(url)

    content_type = resp.headers.get("content-type", "").lower()
    raw = resp.text

    if "html" in content_type or primary_document.lower().endswith((".htm", ".html")):
        text = _extract_text_from_html(raw)
    else:
        text = raw

    text = re.sub(r"\n\s*\n", "\n\n", text)  # collapse multi-blank lines
    text = text.strip()

    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[truncated]"

    return text


def _extract_text_from_html(html: str) -> str:
    """Strip HTML and return clean text suitable for LLM ingestion."""
    soup = BeautifulSoup(html, "html.parser")

    # Remove script/style/header noise
    for tag in soup(["script", "style", "head", "meta", "link"]):
        tag.decompose()

    # Use line breaks as separators to preserve some structure
    text = soup.get_text(separator="\n")

    # Tidy whitespace
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


# -----------------------------------------------------------------------------
# Convenience: full lookup-to-text in one call
# -----------------------------------------------------------------------------

def fetch_recent_filings_with_text(
    ticker: str,
    form_types: Optional[list] = None,
    days_back: int = 30,
    max_results: int = 5,
    max_chars_per_filing: int = 50_000,
) -> list:
    """
    Full convenience pipeline: ticker -> CIK -> recent filings -> text content.

    Returns list of dicts each containing the filing metadata plus a 'text' field
    with the extracted document content.
    """
    cik = get_cik(ticker)
    if not cik:
        logger.warning("No CIK found for ticker %s", ticker)
        return []

    filings = get_recent_filings(
        cik, form_types=form_types, days_back=days_back, max_results=max_results
    )

    out = []
    for f in filings:
        try:
            text = fetch_filing_text(
                f["accession_number"],
                f["primary_document"],
                cik=cik,
                max_chars=max_chars_per_filing,
            )
            f["text"] = text
            out.append(f)
        except Exception as e:
            logger.warning(
                "Failed to fetch filing %s for %s: %s",
                f["accession_number"], ticker, e
            )

    return out


# -----------------------------------------------------------------------------
# Smoke test (run as a script)
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("=== Step 1: ticker lookup ===")
    cik = get_cik("IONQ")
    name = get_company_name("IONQ")
    print(f"IONQ → CIK {cik} ({name})")

    print()
    print("=== Step 2: recent filings (last 90 days, all forms) ===")
    filings = get_recent_filings(cik, days_back=90, max_results=10)
    for f in filings:
        print(f"  {f['filing_date']}  {f['form']:6s}  {f['accession_number']}  {f['primary_doc_description'][:50]}")

    print()
    print("=== Step 3: fetch text of most recent 8-K (if any) ===")
    eight_ks = get_recent_filings(cik, form_types=["8-K"], days_back=180, max_results=1)
    if eight_ks:
        f = eight_ks[0]
        text = fetch_filing_text(f["accession_number"], f["primary_document"], cik)
        print(f"Filing: {f['form']} dated {f['filing_date']}")
        print(f"Document: {f['primary_document']}")
        print(f"Text length: {len(text)} chars")
        print(f"First 500 chars:")
        print(text[:500])
    else:
        print("No 8-K filings found in last 180 days.")
