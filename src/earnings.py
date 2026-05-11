"""Earnings + trusted-source-news fetcher.

For each ticker we fetch:
- Most recent past earnings: actual EPS, estimated EPS → beat / meet / miss
- Next scheduled earnings date (already on MarketData; this module also
  surfaces the EPS estimate for it)
- Recent company news from Finnhub, filtered to authoritative sources first
  (SEC, company PR feed, Reuters, Bloomberg, AP, WSJ, FT)
- A canonical SEC EDGAR filings link — always present for US-listed tickers,
  doesn't require an API call

All Finnhub-backed pieces only run when FINNHUB_API_KEY is set. Without it,
we still surface the SEC EDGAR link as a baseline trusted-source pointer.

Cache: 6 hours. Earnings dates don't move minute-to-minute, and the news
endpoint pulls 7 days of history — refreshing every 6h is plenty.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

from .config import DB_PATH
from .storage import get_conn

FINNHUB_BASE = "https://finnhub.io/api/v1"

# News sources we consider authoritative. Order = preference.
TRUSTED_SOURCE_RANK = {
    "SEC Filings": 0,           # SEC EDGAR (we synthesize this)
    "BusinessWire": 1,          # Company press releases
    "PR Newswire": 1,
    "GlobeNewswire": 1,
    "Reuters": 2,
    "Bloomberg": 2,
    "Wall Street Journal": 2,
    "The Wall Street Journal": 2,
    "Financial Times": 2,
    "Associated Press": 2,
    "AP News": 2,
    "CNBC": 3,
    "MarketWatch": 3,
    "Barron's": 3,
    "Forbes": 4,
    "Yahoo": 5,
    "Seeking Alpha": 5,
}

CACHE_TTL_SECONDS = 6 * 60 * 60  # 6 hours


SCHEMA = """
CREATE TABLE IF NOT EXISTS earnings_data (
    ticker TEXT PRIMARY KEY,
    last_actual_eps REAL,
    last_estimate_eps REAL,
    last_period TEXT,            -- e.g. "2025-12-31"
    last_surprise TEXT,          -- "beat" | "meet" | "miss" | NULL
    last_surprise_pct REAL,      -- (actual - estimate) / |estimate| * 100
    next_estimate_eps REAL,      -- consensus estimate for next quarter
    next_date TEXT,              -- ISO date, may also be on market_data
    news_json TEXT,              -- JSON list of {source, headline, url, datetime, is_trusted}
    fetched_at REAL NOT NULL
);
"""


@dataclass
class NewsItem:
    source: str
    headline: str
    url: str
    datetime: int  # unix
    is_trusted: bool = False


@dataclass
class EarningsData:
    ticker: str
    last_actual_eps: Optional[float] = None
    last_estimate_eps: Optional[float] = None
    last_period: Optional[str] = None
    last_surprise: Optional[str] = None  # "beat" | "meet" | "miss"
    last_surprise_pct: Optional[float] = None
    next_estimate_eps: Optional[float] = None
    next_date: Optional[str] = None
    news: list[NewsItem] = field(default_factory=list)
    fetched_at: float = 0.0

    @property
    def sec_edgar_url(self) -> str:
        # Browse-EDGAR by ticker — works for any US-listed equity, no API key.
        return (
            f"https://www.sec.gov/cgi-bin/browse-edgar"
            f"?action=getcompany&CIK={self.ticker}&type=10-&dateb=&owner=include&count=10"
        )


def init_earnings_table(db_path: Path = DB_PATH) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)


def _classify_surprise(actual: float, estimate: float) -> tuple[str, float]:
    """Return ("beat"|"meet"|"miss", surprise_pct)."""
    if estimate == 0:
        return ("meet" if actual == 0 else "beat" if actual > 0 else "miss"), 0.0
    pct = (actual - estimate) / abs(estimate) * 100.0
    # 1% tolerance for "meet" since reported EPS rounds.
    if abs(pct) < 1.0:
        return "meet", pct
    return ("beat" if pct > 0 else "miss"), pct


def _fetch_finnhub_earnings(ticker: str, api_key: str) -> dict:
    """Returns the parts populated from Finnhub's /stock/earnings + /calendar/earnings."""
    out: dict = {}
    headers = {"X-Finnhub-Token": api_key}
    try:
        # Past earnings — array sorted newest first
        r = requests.get(f"{FINNHUB_BASE}/stock/earnings",
                         params={"symbol": ticker, "limit": 4},
                         headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json() or []
            if data:
                last = data[0]
                actual = last.get("actual")
                estimate = last.get("estimate")
                if actual is not None and estimate is not None:
                    surprise, pct = _classify_surprise(actual, estimate)
                    out["last_actual_eps"] = actual
                    out["last_estimate_eps"] = estimate
                    out["last_surprise"] = surprise
                    out["last_surprise_pct"] = pct
                    out["last_period"] = last.get("period")
                elif actual is not None:
                    out["last_actual_eps"] = actual
                    out["last_period"] = last.get("period")

        # Upcoming earnings — Finnhub's calendar
        today = time.strftime("%Y-%m-%d", time.gmtime(time.time()))
        ahead = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 120 * 86400))
        r = requests.get(f"{FINNHUB_BASE}/calendar/earnings",
                         params={"symbol": ticker, "from": today, "to": ahead},
                         headers=headers, timeout=10)
        if r.status_code == 200:
            entries = (r.json() or {}).get("earningsCalendar", []) or []
            entries.sort(key=lambda e: e.get("date") or "")
            if entries:
                nxt = entries[0]
                out["next_date"] = nxt.get("date")
                est = nxt.get("epsEstimate")
                if est is not None:
                    out["next_estimate_eps"] = est
    except requests.RequestException:
        pass
    return out


def _fetch_finnhub_news(ticker: str, api_key: str, days: int = 7,
                       max_items: int = 5) -> list[NewsItem]:
    """Fetch recent company news, ranked by source trust + recency."""
    headers = {"X-Finnhub-Token": api_key}
    today = time.strftime("%Y-%m-%d", time.gmtime(time.time()))
    week_ago = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))
    try:
        r = requests.get(f"{FINNHUB_BASE}/company-news",
                         params={"symbol": ticker, "from": week_ago, "to": today},
                         headers=headers, timeout=10)
        if r.status_code != 200:
            return []
        items = r.json() or []
    except requests.RequestException:
        return []

    parsed: list[NewsItem] = []
    for it in items:
        source = (it.get("source") or "").strip()
        url = (it.get("url") or "").strip()
        headline = (it.get("headline") or "").strip()
        if not (source and url and headline):
            continue
        rank = TRUSTED_SOURCE_RANK.get(source)
        is_trusted = rank is not None and rank <= 3
        parsed.append(NewsItem(
            source=source,
            headline=headline,
            url=url,
            datetime=int(it.get("datetime") or 0),
            is_trusted=is_trusted,
        ))

    # Sort: trusted first by source rank, then by recency. Untrusted at the
    # bottom in recency order.
    def sort_key(n: NewsItem):
        rank = TRUSTED_SOURCE_RANK.get(n.source, 99)
        # Negate datetime so newer is "smaller"
        return (rank, -n.datetime)
    parsed.sort(key=sort_key)
    return parsed[:max_items]


def fetch_for_ticker(ticker: str, db_path: Path = DB_PATH,
                     use_cache: bool = True) -> Optional[EarningsData]:
    init_earnings_table(db_path)
    api_key = os.environ.get("FINNHUB_API_KEY") or None

    if use_cache:
        with get_conn(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM earnings_data WHERE ticker = ?",
                (ticker,),
            ).fetchone()
        if row and (time.time() - (row["fetched_at"] or 0)) < CACHE_TTL_SECONDS:
            return _row_to_earnings(row)

    if not api_key:
        # Without Finnhub we still produce the SEC link.
        ed = EarningsData(ticker=ticker, fetched_at=time.time())
        return ed

    parts = _fetch_finnhub_earnings(ticker, api_key)
    news = _fetch_finnhub_news(ticker, api_key)

    ed = EarningsData(
        ticker=ticker,
        last_actual_eps=parts.get("last_actual_eps"),
        last_estimate_eps=parts.get("last_estimate_eps"),
        last_period=parts.get("last_period"),
        last_surprise=parts.get("last_surprise"),
        last_surprise_pct=parts.get("last_surprise_pct"),
        next_estimate_eps=parts.get("next_estimate_eps"),
        next_date=parts.get("next_date"),
        news=news,
        fetched_at=time.time(),
    )
    _save(ed, db_path)
    return ed


def fetch_for_tickers(tickers: list[str], db_path: Path = DB_PATH,
                      use_cache: bool = True, verbose: bool = True,
                      max_concurrent: int = 6) -> dict[str, EarningsData]:
    """Fetch earnings + news for many tickers in parallel."""
    init_earnings_table(db_path)
    out: dict[str, EarningsData] = {}
    if not tickers:
        return out

    # Prime cache lookups in one query
    if use_cache:
        with get_conn(db_path) as conn:
            placeholders = ",".join("?" * len(tickers))
            rows = conn.execute(
                f"SELECT * FROM earnings_data WHERE ticker IN ({placeholders})",
                tickers,
            ).fetchall()
        for r in rows:
            ed = _row_to_earnings(r)
            if (time.time() - ed.fetched_at) < CACHE_TTL_SECONDS:
                out[ed.ticker] = ed

    to_fetch = [t for t in tickers if t not in out]
    if verbose:
        print(f"  earnings: {len(out)} from cache, {len(to_fetch)} to fetch")

    if not to_fetch:
        return out

    with ThreadPoolExecutor(max_workers=min(max_concurrent, len(to_fetch))) as ex:
        futures = {ex.submit(fetch_for_ticker, t, db_path, False): t for t in to_fetch}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                ed = fut.result()
            except Exception:
                ed = None
            if ed:
                out[t] = ed

    return out


def _row_to_earnings(row) -> EarningsData:
    import json
    news = []
    if row["news_json"]:
        try:
            for n in json.loads(row["news_json"]):
                news.append(NewsItem(**n))
        except (ValueError, TypeError):
            pass
    return EarningsData(
        ticker=row["ticker"],
        last_actual_eps=row["last_actual_eps"],
        last_estimate_eps=row["last_estimate_eps"],
        last_period=row["last_period"],
        last_surprise=row["last_surprise"],
        last_surprise_pct=row["last_surprise_pct"],
        next_estimate_eps=row["next_estimate_eps"],
        next_date=row["next_date"],
        news=news,
        fetched_at=row["fetched_at"] or 0.0,
    )


def _save(ed: EarningsData, db_path: Path = DB_PATH) -> None:
    import json
    news_json = json.dumps([n.__dict__ for n in ed.news]) if ed.news else None
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO earnings_data(
                ticker, last_actual_eps, last_estimate_eps, last_period,
                last_surprise, last_surprise_pct, next_estimate_eps, next_date,
                news_json, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                last_actual_eps=excluded.last_actual_eps,
                last_estimate_eps=excluded.last_estimate_eps,
                last_period=excluded.last_period,
                last_surprise=excluded.last_surprise,
                last_surprise_pct=excluded.last_surprise_pct,
                next_estimate_eps=excluded.next_estimate_eps,
                next_date=excluded.next_date,
                news_json=excluded.news_json,
                fetched_at=excluded.fetched_at
            """,
            (ed.ticker, ed.last_actual_eps, ed.last_estimate_eps, ed.last_period,
             ed.last_surprise, ed.last_surprise_pct, ed.next_estimate_eps,
             ed.next_date, news_json, ed.fetched_at),
        )
