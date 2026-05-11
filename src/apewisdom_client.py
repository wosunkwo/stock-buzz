"""Apewisdom client — aggregated Reddit/StockTwits ticker buzz.

Apewisdom (https://apewisdom.io) scrapes a basket of investing subreddits
(wallstreetbets, stocks, investing, ValueInvesting, StockMarket, options,
pennystocks, Daytrading, SmallStreetBets, StocksAndTrading) plus StockTwits
and exposes a free JSON API with mention counts and 24h-ago rank deltas.

We use this as a stand-in for direct Reddit access (which is blocked from
this network).

Per-result fields:
  rank, ticker, name, mentions, upvotes, rank_24h_ago, mentions_24h_ago

Notes & limitations:
- No per-post text, just aggregate counts → AI summarizer has less material
  for tickers that ONLY appear via Apewisdom.
- Sentiment is reported but is mostly null in the response, so we ignore it.
- "all-stocks" filter = union across all sources Apewisdom scrapes (the WSB,
  stocks, etc. subreddits + StockTwits). To avoid double-counting StockTwits
  buzz which we already collect directly, we use the dedicated subreddit
  filters instead and combine them.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional

import requests

API_BASE = "https://apewisdom.io/api/v1.0/filter"
USER_AGENT = "stock-buzz/0.4 (personal research)"
REQUEST_DELAY_SECONDS = 0.3
DEFAULT_TIMEOUT = 12

# Apewisdom filters — these are the buckets we'd actually pull from.
# "all-stocks" includes StockTwits which we'd double-count, so we explicitly
# enumerate the Reddit-only subreddits instead. Each is a separate API call;
# we paginate the first 2 pages of each (≈100 tickers per sub) and merge.
REDDIT_FILTERS = [
    "wallstreetbets",
    "stocks",
    "investing",
    "stockmarket",
    "options",
    "pennystocks",
    "daytrading",
    "smallstreetbets",
    "valueinvesting",
    "stocksandtrading",
]
PAGES_PER_FILTER = 2  # ~100 tickers per filter


@dataclass
class ApewisdomEntry:
    ticker: str
    name: Optional[str]
    filter_name: str          # e.g. "wallstreetbets"
    rank: int
    mentions: int
    upvotes: int
    rank_24h_ago: Optional[int]
    mentions_24h_ago: Optional[int]
    fetched_at: float

    @property
    def rank_delta(self) -> Optional[int]:
        """Positive = climbed; e.g. went from rank 14 to rank 3 → delta = +11."""
        if self.rank_24h_ago is None:
            return None
        return self.rank_24h_ago - self.rank

    @property
    def mention_growth_pct(self) -> Optional[float]:
        if not self.mentions_24h_ago:
            return None
        return (self.mentions - self.mentions_24h_ago) / max(self.mentions_24h_ago, 1) * 100.0


def _fetch_page(filter_name: str, page: int) -> list[ApewisdomEntry]:
    url = f"{API_BASE}/{filter_name}/page/{page}"
    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
        if r.status_code != 200:
            return []
        payload = r.json()
    except (requests.RequestException, ValueError):
        return []

    now = time.time()
    out: list[ApewisdomEntry] = []
    for d in payload.get("results", []) or []:
        ticker = (d.get("ticker") or "").upper().strip()
        if not ticker:
            continue
        try:
            out.append(ApewisdomEntry(
                ticker=ticker,
                name=d.get("name"),
                filter_name=filter_name,
                rank=int(d.get("rank") or 0),
                mentions=int(d.get("mentions") or 0),
                upvotes=int(d.get("upvotes") or 0),
                rank_24h_ago=int(d["rank_24h_ago"]) if d.get("rank_24h_ago") is not None else None,
                mentions_24h_ago=int(d["mentions_24h_ago"]) if d.get("mentions_24h_ago") is not None else None,
                fetched_at=now,
            ))
        except (TypeError, ValueError):
            continue
    return out


def fetch_apewisdom(
    filters: Optional[list[str]] = None,
    pages_per_filter: int = PAGES_PER_FILTER,
    verbose: bool = True,
) -> list[ApewisdomEntry]:
    """Fetch aggregate ticker buzz across the configured Reddit filters.

    Returns a flat list of entries — same ticker appearing in multiple
    subreddits will be returned once per subreddit. The orchestrator dedups
    when rolling these into mentions.
    """
    filters = filters or REDDIT_FILTERS
    all_entries: list[ApewisdomEntry] = []

    # Concurrent fetches — Apewisdom has been generous with rate limits;
    # cap at 6 concurrent to be polite.
    work = [(f, p) for f in filters for p in range(1, pages_per_filter + 1)]
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_fetch_page, f, p): (f, p) for f, p in work}
        for fut in as_completed(futs):
            f, p = futs[fut]
            try:
                entries = fut.result() or []
            except Exception:
                entries = []
            all_entries.extend(entries)
            if verbose:
                print(f"  apewisdom r/{f} page {p}: {len(entries)}")
            time.sleep(REQUEST_DELAY_SECONDS)

    return all_entries
