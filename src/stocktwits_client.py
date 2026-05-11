"""Fetch trending symbols and recent messages from StockTwits.

StockTwits is a finance-focused social network with a free public API.
For Phase 1 we use:
  - GET /api/2/trending/symbols.json
        Returns ~30 trending tickers, each with a watchlist_count.
  - GET /api/2/streams/symbol/<TICKER>.json
        Returns ~30 most recent messages for a ticker, with bullish/bearish
        sentiment labels when the author tagged them.

No auth needed for read-only public data. Rate limits are generous (~200/hr
unauthenticated) but we still throttle politely.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests

API_BASE = "https://api.stocktwits.com/api/2"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) stock-buzz/0.1"

# Concurrency for per-symbol stream fetches. StockTwits handles ~6-8 concurrent
# requests cleanly without 429ing in practice.
MAX_CONCURRENT = 6


@dataclass
class TrendingSymbol:
    symbol: str
    title: str
    watchlist_count: int
    exchange: Optional[str] = None


@dataclass
class StockTwitsMessage:
    id: int
    body: str
    created_at: str  # ISO 8601 string from API
    username: str
    user_followers: int
    sentiment: Optional[str]  # "Bullish", "Bearish", or None
    symbols: list[str] = field(default_factory=list)

    @property
    def created_at_dt(self) -> datetime:
        return datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))


def _get(path: str, params: dict | None = None) -> dict:
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(f"{API_BASE}{path}", headers=headers, params=params, timeout=15)
    if resp.status_code == 429:
        time.sleep(15)
        resp = requests.get(f"{API_BASE}{path}", headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def fetch_trending(limit: int = 30) -> list[TrendingSymbol]:
    payload = _get("/trending/symbols.json", params={"limit": limit})
    out: list[TrendingSymbol] = []
    for s in payload.get("symbols", []):
        out.append(
            TrendingSymbol(
                symbol=s.get("symbol", ""),
                title=s.get("title", "") or "",
                watchlist_count=int(s.get("watchlist_count", 0) or 0),
                exchange=s.get("exchange"),
            )
        )
    return out


def fetch_messages_for_symbol(symbol: str, limit: int = 30) -> list[StockTwitsMessage]:
    """Fetch recent messages for a symbol. Includes user-tagged sentiment."""
    payload = _get(f"/streams/symbol/{symbol}.json", params={"limit": limit})
    out: list[StockTwitsMessage] = []
    for m in payload.get("messages", []):
        user = m.get("user", {}) or {}
        entities = m.get("entities", {}) or {}
        sent = entities.get("sentiment") or {}
        sentiment_label = sent.get("basic") if isinstance(sent, dict) else None

        # Symbols mentioned in the message (cross-ticker mentions matter for buzz).
        msg_symbols = [s.get("symbol", "") for s in (m.get("symbols", []) or []) if s.get("symbol")]

        out.append(
            StockTwitsMessage(
                id=int(m.get("id", 0)),
                body=m.get("body", "") or "",
                created_at=m.get("created_at", "") or "",
                username=user.get("username", "") or "",
                user_followers=int(user.get("followers", 0) or 0),
                sentiment=sentiment_label,
                symbols=msg_symbols,
            )
        )
    return out


def fetch_trending_with_messages(
    trending_limit: int = 30,
    messages_per_symbol: int = 30,
    verbose: bool = True,
    max_concurrent: int = MAX_CONCURRENT,
) -> list[tuple[TrendingSymbol, list[StockTwitsMessage]]]:
    """Fetch trending symbols, then pull recent messages for each in parallel.

    Returns list ordered by trending rank (so callers see the same ordering
    regardless of which fetch completed first).
    """
    trending = fetch_trending(limit=trending_limit)
    if verbose:
        print(f"  trending symbols: {len(trending)}")

    results: dict[str, tuple[TrendingSymbol, list[StockTwitsMessage]]] = {}

    def _fetch(sym: TrendingSymbol):
        try:
            msgs = fetch_messages_for_symbol(sym.symbol, limit=messages_per_symbol)
            return sym, msgs
        except requests.HTTPError as e:
            print(f"  ${sym.symbol}: HTTP error {e}")
        except requests.RequestException as e:
            print(f"  ${sym.symbol}: request failed: {e}")
        return sym, []

    with ThreadPoolExecutor(max_workers=max_concurrent) as ex:
        futs = {ex.submit(_fetch, sym): sym for sym in trending}
        for fut in as_completed(futs):
            sym, msgs = fut.result()
            results[sym.symbol] = (sym, msgs)
            if verbose:
                print(f"  ${sym.symbol}: {len(msgs)} messages")

    # Preserve trending-rank order in the returned list
    return [results[sym.symbol] for sym in trending if sym.symbol in results]
