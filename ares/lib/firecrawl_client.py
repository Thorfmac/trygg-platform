"""
Firecrawl client
================

Thin wrapper around Firecrawl for use by the verifier agent.

Hybrid approach:
  - scrape:  uses Firecrawl Python SDK v1.6.0 (working against v1 REST API)
  - search:  uses direct HTTP POST to /v2/search (the v1 SDK does not expose
             search; that endpoint was added in v2 of Firecrawl's REST API)

Both use the same FIRECRAWL_API_KEY env var.

Three core capabilities:
  - scrape(url)                   → fetch one URL, return clean markdown + metadata
  - search(query, max_results)    → web search via direct HTTP to /v2/search
  - search_and_scrape(query, ...) → composite: search then scrape top hits

SDK version notes (v1.6.0):
  - scrape_url() takes params={'formats': ['markdown']} as a dict, NOT keyword args
  - scrape returns dict with 'markdown' and 'metadata' keys
  - search() in the SDK raises "Search is not supported in v1" — bypassed via HTTP

REST API v2 search endpoint:
  - POST https://api.firecrawl.dev/v2/search
  - Headers: Authorization: Bearer {API_KEY}
  - Body: {"query": "...", "limit": N}
  - Response shape:
      {
        "success": true,
        "data": {
          "web": [{"url", "title", "description", "position"}, ...]
        },
        "creditsUsed": N,
        "id": "..."
      }
  - Note: results are nested under data.web (also data.images, data.news may exist)
"""

import logging
import os
import warnings
from typing import Optional

# Suppress the cosmetic Pydantic shadow-attribute warning from Firecrawl v1.6.0
warnings.filterwarnings(
    "ignore",
    message=r'Field name "schema" in "FirecrawlApp.ExtractParams" shadows.*',
)

import requests
from firecrawl import FirecrawlApp

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

API_KEY = os.getenv("FIRECRAWL_API_KEY", "")
if not API_KEY:
    raise RuntimeError(
        "FIRECRAWL_API_KEY env var is required. "
        "Set it in .env from your Firecrawl subscription dashboard."
    )

FIRECRAWL_API_BASE = "https://api.firecrawl.dev"
SEARCH_ENDPOINT = f"{FIRECRAWL_API_BASE}/v2/search"

DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_CONTENT_CHARS = 50_000

_sdk_client: Optional[FirecrawlApp] = None


def _get_sdk() -> FirecrawlApp:
    global _sdk_client
    if _sdk_client is None:
        _sdk_client = FirecrawlApp(api_key=API_KEY)
    return _sdk_client


# -----------------------------------------------------------------------------
# Single-URL scrape (via SDK)
# -----------------------------------------------------------------------------

def scrape(url: str, max_chars: int = DEFAULT_MAX_CONTENT_CHARS) -> dict:
    """
    Fetch one URL and return its content as clean markdown.

    Returns dict: {url, markdown, metadata, success, error}
    """
    if not url:
        return {"url": url, "markdown": "", "metadata": {}, "success": False, "error": "empty url"}

    logger.info("Firecrawl scrape: %s", url)

    try:
        client = _get_sdk()
        result = client.scrape_url(url, params={"formats": ["markdown"]})

        if not isinstance(result, dict):
            return {
                "url": url, "markdown": "", "metadata": {},
                "success": False,
                "error": f"unexpected result type: {type(result).__name__}",
            }

        markdown = result.get("markdown", "") or ""
        metadata = result.get("metadata", {}) or {}

        if len(markdown) > max_chars:
            markdown = markdown[:max_chars] + "\n\n[truncated]"

        return {
            "url": url,
            "markdown": markdown,
            "metadata": metadata,
            "success": True,
            "error": None,
        }

    except Exception as e:
        logger.warning("Firecrawl scrape failed for %s: %s", url, e)
        return {
            "url": url, "markdown": "", "metadata": {},
            "success": False,
            "error": f"{type(e).__name__}: {e}",
        }


# -----------------------------------------------------------------------------
# Web search (via direct HTTP to /v2/search)
# -----------------------------------------------------------------------------

def search(query: str, max_results: int = 5) -> list:
    """
    Run a web search via Firecrawl's v2 REST API.

    Returns: list of dicts [{url, title, description, position}, ...]
    Empty list on failure (errors logged).
    """
    if not query:
        return []

    logger.info("Firecrawl search: %s (max_results=%d)", query, max_results)

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {"query": query, "limit": max_results}

    try:
        resp = requests.post(
            SEARCH_ENDPOINT,
            headers=headers,
            json=payload,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            logger.warning(
                "Firecrawl search returned status %d for '%s': %s",
                resp.status_code, query, resp.text[:300]
            )
            return []

        body = resp.json()

    except Exception as e:
        logger.warning("Firecrawl search HTTP failed for '%s': %s", query, e)
        return []

    if not isinstance(body, dict):
        logger.warning("Firecrawl search returned non-dict body: %s", type(body).__name__)
        return []

    if body.get("success") is False:
        logger.warning(
            "Firecrawl search returned success=false: %s",
            body.get("error", "no error message")
        )
        return []

    # v2 response shape: data is a dict with 'web', 'images', 'news' sub-keys.
    # Some older versions may return a flat list — handle both defensively.
    data = body.get("data", {})
    if isinstance(data, list):
        web_results = data
    elif isinstance(data, dict):
        web_results = data.get("web", [])
        if not isinstance(web_results, list):
            logger.warning(
                "Firecrawl search data.web is not a list: %s",
                type(web_results).__name__
            )
            return []
    else:
        logger.warning("Firecrawl search 'data' unexpected type: %s", type(data).__name__)
        return []

    credits = body.get("creditsUsed")
    if credits is not None:
        logger.debug("Firecrawl search used %d credits", credits)

    out = []
    for item in web_results[:max_results]:
        if not isinstance(item, dict):
            continue
        url = item.get("url", "")
        if not url:
            continue
        out.append({
            "url": url,
            "title": item.get("title", "") or "",
            "description": item.get("description", "") or "",
            "position": item.get("position"),
        })

    logger.info("Firecrawl search returned %d results for: %s", len(out), query)
    return out


# -----------------------------------------------------------------------------
# Composite: search then scrape top hits
# -----------------------------------------------------------------------------

def search_and_scrape(
    query: str,
    max_results: int = 3,
    max_chars_per_page: int = DEFAULT_MAX_CONTENT_CHARS,
    allowed_domains: Optional[list] = None,
) -> list:
    """
    Search the web, then scrape the top hits.

    If allowed_domains is provided, filter results to URLs containing one of these
    substrings (e.g., ['sec.gov', 'nasa.gov']) — used to prefer Tier 1 primaries.

    Returns: list of scrape() results with added 'title', 'description', 'position'.
    """
    fetch_count = max_results * 3 if allowed_domains else max_results
    results = search(query, max_results=fetch_count)

    if allowed_domains:
        results = [
            r for r in results
            if any(d.lower() in r["url"].lower() for d in allowed_domains)
        ]

    results = results[:max_results]

    out = []
    for r in results:
        scraped = scrape(r["url"], max_chars=max_chars_per_page)
        scraped["title"] = r.get("title", "")
        scraped["description"] = r.get("description", "")
        scraped["position"] = r.get("position")
        out.append(scraped)

    return out


# -----------------------------------------------------------------------------
# Smoke test
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("=== Step 1: scrape a known URL ===")
    r = scrape("https://www.nasa.gov/news/", max_chars=300)
    print(f"Success: {r['success']}")
    print(f"URL: {r['url']}")
    print(f"Metadata title: {r['metadata'].get('title', 'n/a')}")
    print(f"First 300 chars of markdown:")
    print(r["markdown"][:300])
    if r.get("error"):
        print(f"Error: {r['error']}")

    print()
    print("=== Step 2: web search (v2 REST, parsing data.web) ===")
    results = search("NASA Lunar Terrain Vehicle award Lunar Outpost Astrolab", max_results=5)
    if not results:
        print("(no results returned)")
    for i, item in enumerate(results, 1):
        print(f"  {i}. [{item.get('position', '?')}] {item['url']}")
        print(f"     title: {(item['title'] or '')[:90]}")
        print(f"     desc:  {(item['description'] or '')[:120]}")

    print()
    print("=== Step 3: search-and-scrape, preferring Tier 1 primaries ===")
    composite = search_and_scrape(
        "Intuitive Machines LUNR Lunar Terrain Vehicle NASA award",
        max_results=2,
        max_chars_per_page=500,
        allowed_domains=["nasa.gov", "sec.gov", "intuitivemachines.com"],
    )
    if not composite:
        print("(no domain-matched results found)")
    for i, r in enumerate(composite, 1):
        print(f"  {i}. {r['url']}")
        print(f"     success: {r['success']}")
        print(f"     title: {r.get('title', '')[:90]}")
        print(f"     first 300 chars of content:")
        print(f"       {r['markdown'][:300]}")
