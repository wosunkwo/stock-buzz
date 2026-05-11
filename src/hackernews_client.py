"""Hacker News client — search HN via the Algolia public API.

We use HN as a complementary signal: it skews tech/builder rather than
retail-trader, and gives us full post titles + URLs + comment counts that
the AI summarizer can chew on. Particularly strong for chip/AI/SaaS tickers,
weaker for biotech/energy/consumer.

API: https://hn.algolia.com/api/v1/search

We don't pull HN for every ticker — only for a candidate list (the top-N
buzzy ones from other sources) to keep request volume sane and avoid noise
from short ticker symbols matching unrelated text.

Filtering strategy to reduce false positives (e.g. "AI" or "U" matching
unrelated stories):
- Tickers ≤ 2 chars: require `$<TICKER>` prefix in the search
- Tickers 3-5 chars: search by ticker AND the company name token
- Tickers known to be ETFs (SPY, QQQ, etc.): skip — not company-specific
- Restrict to last 30 days
- Require minimum points or comment threshold
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional

import requests

API = "https://hn.algolia.com/api/v1/search_by_date"
USER_AGENT = "stock-buzz/0.4 (personal research)"
DEFAULT_TIMEOUT = 10
REQUEST_DELAY_SECONDS = 0.15

# Tickers we don't search for — too generic, would drown in noise.
SKIP_TICKERS = {
    # ETFs (broad-market, not company-specific signal)
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "ARKK", "ARKG", "ARKW",
    "XLF", "XLE", "XLK", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE",
    "TLT", "IEF", "GLD", "SLV", "USO", "UNG", "UVXY", "VXX", "SQQQ", "TQQQ",
    "SOXX", "SMH", "SOXL", "SOXS", "TSLL", "NVDL", "BITO", "GBTC", "ETHE",
    # Single-letter or 2-letter common-word tickers
    "U", "AI", "DD", "EV", "ON", "GO", "IT", "OR", "BE", "DO",
    # Common English words masquerading as tickers
    "ALL", "OPEN", "PLAY", "REAL", "WELL", "FAST", "NEW", "NICE",
}


@dataclass
class HNStory:
    ticker: str
    object_id: str
    title: str
    url: Optional[str]      # external URL; None for Ask/Show HN
    author: str
    points: int
    num_comments: int
    created_at_unix: float
    matched_query: str

    @property
    def hn_url(self) -> str:
        """Permalink to the HN discussion thread."""
        return f"https://news.ycombinator.com/item?id={self.object_id}"

    @property
    def display_url(self) -> str:
        """Best link for the modal — prefer the article URL, fall back to HN."""
        return self.url or self.hn_url


def _search_one(ticker: str, name_hint: Optional[str] = None,
                lookback_days: int = 30,
                min_points: int = 5,
                limit: int = 5) -> list[HNStory]:
    """Search HN for stories about a single ticker.

    Returns the top hits (by recency, since we use search_by_date) that
    pass the points filter. We also filter post-hoc by checking the title
    for the ticker symbol — Algolia's relevance can be loose.
    """
    if ticker in SKIP_TICKERS:
        return []

    cutoff = int(time.time() - lookback_days * 86400)
    headers = {"User-Agent": USER_AGENT}

    # Build the query. For short tickers, require company name to disambiguate.
    if len(ticker) <= 2:
        if not name_hint:
            return []
        query = f'"{name_hint}"'
    else:
        # Use the ticker — short symbols still have decent recall on HN.
        query = ticker

    params = {
        "query": query,
        "tags": "story",
        "numericFilters": f"created_at_i>{cutoff},points>={min_points}",
        "hitsPerPage": limit * 3,  # over-fetch so we can filter post-hoc
    }
    try:
        r = requests.get(API, headers=headers, params=params, timeout=DEFAULT_TIMEOUT)
        if r.status_code != 200:
            return []
        payload = r.json()
    except (requests.RequestException, ValueError):
        return []

    out: list[HNStory] = []
    ticker_upper = ticker.upper()
    name_lower = (name_hint or "").lower()
    for hit in payload.get("hits", []) or []:
        title = (hit.get("title") or "").strip()
        if not title:
            continue
        title_upper = title.upper()
        title_lower = title.lower()

        # Post-hoc relevance filter: the title should mention either the
        # ticker (case-insensitive, word-bounded) or the company name. This
        # cuts false positives from Algolia's loose match.
        ticker_in_title = (
            f" {ticker_upper} " in f" {title_upper} "
            or f"${ticker_upper}" in title_upper
            or f"({ticker_upper})" in title_upper
            or f"({ticker_upper}:" in title_upper
            or title_upper.startswith(f"{ticker_upper} ")
            or title_upper.endswith(f" {ticker_upper}")
        )
        name_in_title = name_lower and name_lower in title_lower
        if not (ticker_in_title or name_in_title):
            continue

        out.append(HNStory(
            ticker=ticker_upper,
            object_id=str(hit.get("objectID") or ""),
            title=title,
            url=hit.get("url"),
            author=hit.get("author") or "",
            points=int(hit.get("points") or 0),
            num_comments=int(hit.get("num_comments") or 0),
            created_at_unix=float(hit.get("created_at_i") or 0),
            matched_query=query,
        ))
        if len(out) >= limit:
            break
    return out


def fetch_hackernews(
    tickers: list[str],
    name_hints: Optional[dict[str, str]] = None,
    lookback_days: int = 30,
    min_points: int = 5,
    limit_per_ticker: int = 5,
    max_concurrent: int = 6,
    verbose: bool = True,
) -> dict[str, list[HNStory]]:
    """Search HN for each ticker. Returns {ticker: [HNStory, ...]}.

    name_hints: optional {ticker: company_name} map. Required for tickers
    of length ≤ 2; ignored otherwise.
    """
    name_hints = name_hints or {}
    out: dict[str, list[HNStory]] = {}

    with ThreadPoolExecutor(max_workers=max_concurrent) as ex:
        futs = {
            ex.submit(_search_one, t, name_hints.get(t),
                      lookback_days, min_points, limit_per_ticker): t
            for t in tickers if t not in SKIP_TICKERS
        }
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                stories = fut.result() or []
            except Exception:
                stories = []
            if stories:
                out[t] = stories
            if verbose and stories:
                print(f"  HN {t}: {len(stories)} stories")
            time.sleep(REQUEST_DELAY_SECONDS)

    return out
