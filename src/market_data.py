"""Fetch live price + (optional) fundamentals for tickers.

Two providers:

1. Yahoo no-auth chart endpoint (always on) — gives:
   - Current price, previous close, day range, 52w range, today's volume,
     currency, exchange, long name.
   - Works without any API key. Rate-limit friendly with light concurrency.

2. Finnhub (only when FINNHUB_API_KEY is set) — adds:
   - Market cap, P/E (trailing), EPS, dividend yield, beta, sector, industry,
     prev/next earnings dates.
   - Free tier: 60 calls/min. Sign up at https://finnhub.io.

If no Finnhub key is configured, fundamentals fields stay None and the UI
gracefully shows "—". Yahoo data still populates everywhere.

Concurrency: we fetch in parallel with a small thread pool — Yahoo's chart
endpoint handles ~10 concurrent requests fine in practice. We still throttle
with a tiny per-request delay.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

from .config import DB_PATH
from .storage import get_conn

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart"
FINNHUB_BASE = "https://finnhub.io/api/v1"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) stock-buzz/0.3"

CACHE_TTL_SECONDS = 5 * 60
MAX_CONCURRENT_FETCHES = 8


@dataclass
class MarketData:
    ticker: str
    price: Optional[float] = None
    previous_close: Optional[float] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    week52_high: Optional[float] = None
    week52_low: Optional[float] = None
    volume: Optional[int] = None
    currency: Optional[str] = None
    exchange: Optional[str] = None
    long_name: Optional[str] = None
    # Finnhub-sourced (only when FINNHUB_API_KEY is set)
    market_cap: Optional[float] = None        # in USD millions
    pe_ratio: Optional[float] = None          # trailing P/E
    eps: Optional[float] = None
    dividend_yield: Optional[float] = None    # as decimal, e.g. 0.025 = 2.5%
    beta: Optional[float] = None
    industry: Optional[str] = None
    next_earnings_date: Optional[str] = None  # ISO date string
    prev_earnings_date: Optional[str] = None
    # Extended fundamentals (added 2026-05-09)
    forward_pe: Optional[float] = None        # forward P/E
    peg_ratio: Optional[float] = None         # forward PEG
    pb_ratio: Optional[float] = None          # price/book
    ev_ebitda: Optional[float] = None         # enterprise value / EBITDA
    gross_margin: Optional[float] = None      # %, TTM
    operating_margin: Optional[float] = None  # %, TTM
    net_margin: Optional[float] = None        # %, TTM
    roe: Optional[float] = None               # %, TTM (return on equity)
    roa: Optional[float] = None               # %, TTM (return on assets)
    debt_to_equity: Optional[float] = None    # ratio, total debt / equity
    revenue_growth_yoy: Optional[float] = None  # %, TTM YoY
    eps_growth_yoy: Optional[float] = None      # %, TTM YoY
    return_13w: Optional[float] = None        # %, 13-week price return
    return_52w: Optional[float] = None        # %, 52-week price return
    fetched_at: float = 0.0

    @property
    def percent_change(self) -> Optional[float]:
        if self.price is None or not self.previous_close:
            return None
        return (self.price - self.previous_close) / self.previous_close * 100.0


SCHEMA = """
CREATE TABLE IF NOT EXISTS market_data (
    ticker TEXT PRIMARY KEY,
    price REAL,
    previous_close REAL,
    day_high REAL,
    day_low REAL,
    week52_high REAL,
    week52_low REAL,
    volume INTEGER,
    currency TEXT,
    exchange TEXT,
    long_name TEXT,
    market_cap REAL,
    pe_ratio REAL,
    eps REAL,
    dividend_yield REAL,
    beta REAL,
    industry TEXT,
    next_earnings_date TEXT,
    prev_earnings_date TEXT,
    forward_pe REAL,
    peg_ratio REAL,
    pb_ratio REAL,
    ev_ebitda REAL,
    gross_margin REAL,
    operating_margin REAL,
    net_margin REAL,
    roe REAL,
    roa REAL,
    debt_to_equity REAL,
    revenue_growth_yoy REAL,
    eps_growth_yoy REAL,
    return_13w REAL,
    return_52w REAL,
    fetched_at REAL NOT NULL
);
"""

# Lightweight ALTER TABLE for existing DBs (idempotent).
_ALTER_COLUMNS = [
    ("market_cap", "REAL"),
    ("pe_ratio", "REAL"),
    ("eps", "REAL"),
    ("dividend_yield", "REAL"),
    ("beta", "REAL"),
    ("industry", "TEXT"),
    ("next_earnings_date", "TEXT"),
    ("prev_earnings_date", "TEXT"),
    ("forward_pe", "REAL"),
    ("peg_ratio", "REAL"),
    ("pb_ratio", "REAL"),
    ("ev_ebitda", "REAL"),
    ("gross_margin", "REAL"),
    ("operating_margin", "REAL"),
    ("net_margin", "REAL"),
    ("roe", "REAL"),
    ("roa", "REAL"),
    ("debt_to_equity", "REAL"),
    ("revenue_growth_yoy", "REAL"),
    ("eps_growth_yoy", "REAL"),
    ("return_13w", "REAL"),
    ("return_52w", "REAL"),
]


def init_market_data_table(db_path: Path = DB_PATH) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)
        # Migrate older DBs that don't have the fundamentals columns yet.
        existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(market_data)").fetchall()}
        for col, typ in _ALTER_COLUMNS:
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE market_data ADD COLUMN {col} {typ}")


def _fetch_yahoo(ticker: str) -> Optional[MarketData]:
    """Fetch price + ranges + volume from Yahoo's no-auth chart endpoint."""
    headers = {"User-Agent": USER_AGENT}
    params = {"interval": "1d", "range": "5d"}
    try:
        resp = requests.get(f"{YAHOO_CHART}/{ticker}", headers=headers, params=params, timeout=10)
        if resp.status_code == 429:
            time.sleep(10)
            resp = requests.get(f"{YAHOO_CHART}/{ticker}", headers=headers, params=params, timeout=10)
        if resp.status_code != 200:
            return None
        payload = resp.json()
    except (requests.RequestException, ValueError):
        return None

    chart = payload.get("chart", {})
    if chart.get("error"):
        return None
    results = chart.get("result") or []
    if not results:
        return None
    meta = results[0].get("meta", {}) or {}

    return MarketData(
        ticker=ticker,
        price=meta.get("regularMarketPrice"),
        previous_close=meta.get("chartPreviousClose"),
        day_high=meta.get("regularMarketDayHigh"),
        day_low=meta.get("regularMarketDayLow"),
        week52_high=meta.get("fiftyTwoWeekHigh"),
        week52_low=meta.get("fiftyTwoWeekLow"),
        volume=meta.get("regularMarketVolume"),
        currency=meta.get("currency"),
        exchange=meta.get("fullExchangeName") or meta.get("exchangeName"),
        long_name=meta.get("longName") or meta.get("shortName"),
        fetched_at=time.time(),
    )


def _fetch_finnhub(ticker: str, api_key: str, md: MarketData) -> MarketData:
    """Augment a MarketData with fundamentals from Finnhub. Mutates and returns md."""
    headers = {"X-Finnhub-Token": api_key}
    try:
        # Profile (market cap, industry, name fallback)
        r = requests.get(f"{FINNHUB_BASE}/stock/profile2",
                         params={"symbol": ticker}, headers=headers, timeout=10)
        if r.status_code == 200:
            d = r.json() or {}
            mc = d.get("marketCapitalization")
            if mc:
                md.market_cap = float(mc)  # already in millions USD
            md.industry = d.get("finnhubIndustry") or md.industry
            if not md.long_name:
                md.long_name = d.get("name")

        # Basic financials + extended fundamentals
        r = requests.get(f"{FINNHUB_BASE}/stock/metric",
                         params={"symbol": ticker, "metric": "all"},
                         headers=headers, timeout=10)
        if r.status_code == 200:
            d = (r.json() or {}).get("metric", {}) or {}
            md.pe_ratio = d.get("peTTM") or d.get("peNormalizedAnnual")
            md.eps = d.get("epsTTM") or d.get("epsNormalizedAnnual")
            dy = d.get("dividendYieldIndicatedAnnual")
            if dy is not None:
                md.dividend_yield = dy / 100.0  # finnhub returns %, normalize
            md.beta = d.get("beta")
            # Valuation ratios
            md.forward_pe = d.get("forwardPE")
            md.peg_ratio = d.get("forwardPEG") or d.get("pegRatio")
            md.pb_ratio = d.get("pb")
            md.ev_ebitda = d.get("evEbitdaTTM")
            # Profitability
            md.gross_margin = d.get("grossMarginTTM") or d.get("grossMarginAnnual")
            md.operating_margin = d.get("operatingMarginTTM") or d.get("operatingMarginAnnual")
            md.net_margin = d.get("netProfitMarginTTM") or d.get("netProfitMarginAnnual")
            md.roe = d.get("roeTTM") or d.get("roeRfy")
            md.roa = d.get("roaTTM") or d.get("roaRfy")
            # Leverage
            md.debt_to_equity = (
                d.get("totalDebt/totalEquityAnnual")
                or d.get("longTermDebt/equityAnnual")
            )
            # Growth
            md.revenue_growth_yoy = d.get("revenueGrowthTTMYoy") or d.get("revenueGrowthQuarterlyYoy")
            md.eps_growth_yoy = d.get("epsGrowthTTMYoy") or d.get("epsGrowthQuarterlyYoy")
            # Recent returns (already %, no conversion needed)
            md.return_13w = d.get("13WeekPriceReturnDaily")
            md.return_52w = d.get("52WeekPriceReturnDaily")

        # Earnings calendar (next + prev)
        r = requests.get(f"{FINNHUB_BASE}/calendar/earnings",
                         params={"symbol": ticker,
                                 "from": time.strftime("%Y-%m-%d",
                                                        time.gmtime(time.time() - 90*86400)),
                                 "to": time.strftime("%Y-%m-%d",
                                                      time.gmtime(time.time() + 90*86400))},
                         headers=headers, timeout=10)
        if r.status_code == 200:
            entries = (r.json() or {}).get("earningsCalendar", []) or []
            now_iso = time.strftime("%Y-%m-%d", time.gmtime(time.time()))
            future = [e for e in entries if (e.get("date") or "") >= now_iso]
            past = [e for e in entries if (e.get("date") or "") < now_iso]
            future.sort(key=lambda e: e.get("date") or "")
            past.sort(key=lambda e: e.get("date") or "", reverse=True)
            if future:
                md.next_earnings_date = future[0].get("date")
            if past:
                md.prev_earnings_date = past[0].get("date")
    except requests.RequestException:
        pass
    return md


def _fetch_one(ticker: str, finnhub_key: Optional[str] = None) -> Optional[MarketData]:
    md = _fetch_yahoo(ticker)
    if md is None:
        return None
    if finnhub_key:
        _fetch_finnhub(ticker, finnhub_key, md)
    return md


def _row_to_market_data(row) -> MarketData:
    keys = row.keys()
    def g(k):
        return row[k] if k in keys else None
    return MarketData(
        ticker=row["ticker"],
        price=row["price"],
        previous_close=row["previous_close"],
        day_high=row["day_high"],
        day_low=row["day_low"],
        week52_high=row["week52_high"],
        week52_low=row["week52_low"],
        volume=row["volume"],
        currency=row["currency"],
        exchange=row["exchange"],
        long_name=row["long_name"],
        market_cap=g("market_cap"),
        pe_ratio=g("pe_ratio"),
        eps=g("eps"),
        dividend_yield=g("dividend_yield"),
        beta=g("beta"),
        industry=g("industry"),
        next_earnings_date=g("next_earnings_date"),
        prev_earnings_date=g("prev_earnings_date"),
        forward_pe=g("forward_pe"),
        peg_ratio=g("peg_ratio"),
        pb_ratio=g("pb_ratio"),
        ev_ebitda=g("ev_ebitda"),
        gross_margin=g("gross_margin"),
        operating_margin=g("operating_margin"),
        net_margin=g("net_margin"),
        roe=g("roe"),
        roa=g("roa"),
        debt_to_equity=g("debt_to_equity"),
        revenue_growth_yoy=g("revenue_growth_yoy"),
        eps_growth_yoy=g("eps_growth_yoy"),
        return_13w=g("return_13w"),
        return_52w=g("return_52w"),
        fetched_at=row["fetched_at"] or 0.0,
    )


def _save(md: MarketData, db_path: Path = DB_PATH) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO market_data(
                ticker, price, previous_close, day_high, day_low,
                week52_high, week52_low, volume, currency, exchange, long_name,
                market_cap, pe_ratio, eps, dividend_yield, beta, industry,
                next_earnings_date, prev_earnings_date,
                forward_pe, peg_ratio, pb_ratio, ev_ebitda,
                gross_margin, operating_margin, net_margin,
                roe, roa, debt_to_equity,
                revenue_growth_yoy, eps_growth_yoy,
                return_13w, return_52w,
                fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                price=excluded.price,
                previous_close=excluded.previous_close,
                day_high=excluded.day_high,
                day_low=excluded.day_low,
                week52_high=excluded.week52_high,
                week52_low=excluded.week52_low,
                volume=excluded.volume,
                currency=excluded.currency,
                exchange=excluded.exchange,
                long_name=excluded.long_name,
                market_cap=COALESCE(excluded.market_cap, market_data.market_cap),
                pe_ratio=COALESCE(excluded.pe_ratio, market_data.pe_ratio),
                eps=COALESCE(excluded.eps, market_data.eps),
                dividend_yield=COALESCE(excluded.dividend_yield, market_data.dividend_yield),
                beta=COALESCE(excluded.beta, market_data.beta),
                industry=COALESCE(excluded.industry, market_data.industry),
                next_earnings_date=COALESCE(excluded.next_earnings_date, market_data.next_earnings_date),
                prev_earnings_date=COALESCE(excluded.prev_earnings_date, market_data.prev_earnings_date),
                forward_pe=COALESCE(excluded.forward_pe, market_data.forward_pe),
                peg_ratio=COALESCE(excluded.peg_ratio, market_data.peg_ratio),
                pb_ratio=COALESCE(excluded.pb_ratio, market_data.pb_ratio),
                ev_ebitda=COALESCE(excluded.ev_ebitda, market_data.ev_ebitda),
                gross_margin=COALESCE(excluded.gross_margin, market_data.gross_margin),
                operating_margin=COALESCE(excluded.operating_margin, market_data.operating_margin),
                net_margin=COALESCE(excluded.net_margin, market_data.net_margin),
                roe=COALESCE(excluded.roe, market_data.roe),
                roa=COALESCE(excluded.roa, market_data.roa),
                debt_to_equity=COALESCE(excluded.debt_to_equity, market_data.debt_to_equity),
                revenue_growth_yoy=COALESCE(excluded.revenue_growth_yoy, market_data.revenue_growth_yoy),
                eps_growth_yoy=COALESCE(excluded.eps_growth_yoy, market_data.eps_growth_yoy),
                return_13w=COALESCE(excluded.return_13w, market_data.return_13w),
                return_52w=COALESCE(excluded.return_52w, market_data.return_52w),
                fetched_at=excluded.fetched_at
            """,
            (md.ticker, md.price, md.previous_close, md.day_high, md.day_low,
             md.week52_high, md.week52_low, md.volume, md.currency, md.exchange,
             md.long_name,
             md.market_cap, md.pe_ratio, md.eps, md.dividend_yield, md.beta,
             md.industry, md.next_earnings_date, md.prev_earnings_date,
             md.forward_pe, md.peg_ratio, md.pb_ratio, md.ev_ebitda,
             md.gross_margin, md.operating_margin, md.net_margin,
             md.roe, md.roa, md.debt_to_equity,
             md.revenue_growth_yoy, md.eps_growth_yoy,
             md.return_13w, md.return_52w,
             md.fetched_at),
        )


def ensure_fundamentals(
    tickers: list[str],
    db_path: Path = DB_PATH,
    verbose: bool = True,
) -> dict[str, MarketData]:
    """Guarantee Finnhub fundamentals for the given tickers.

    Uses sequential calls with a polite 1-second sleep so we stay clear of
    Finnhub's 60-req/min free-tier limit even when multiple favorites need
    refresh. Skips tickers that already have `forward_pe` populated (good
    proxy for "Finnhub call already succeeded").

    No-op when FINNHUB_API_KEY is unset.
    """
    init_market_data_table(db_path)
    finnhub_key = os.environ.get("FINNHUB_API_KEY") or None
    if not finnhub_key or not tickers:
        return {}

    out: dict[str, MarketData] = {}

    # Find which tickers are missing fundamentals.
    with get_conn(db_path) as conn:
        placeholders = ",".join("?" * len(tickers))
        rows = conn.execute(
            f"SELECT * FROM market_data WHERE ticker IN ({placeholders})",
            tickers,
        ).fetchall()
    have_fundamentals = {r["ticker"] for r in rows if r["forward_pe"] is not None}
    needs_retry = [t for t in tickers if t not in have_fundamentals]

    if verbose:
        print(f"  ensure_fundamentals: {len(have_fundamentals)} already have, {len(needs_retry)} to retry")

    for t in needs_retry:
        md = _fetch_one(t, finnhub_key)
        if md and md.forward_pe is not None:
            _save(md, db_path)
            out[t] = md
            if verbose:
                print(f"    ✓ ${t} fundamentals fetched")
        else:
            if verbose:
                print(f"    ✗ ${t} no fundamentals (Finnhub returned nothing)")
        # Polite throttle: 1 fetch = 3 Finnhub calls, so 1s gap between fetches
        # gives ~20 fetches/min which is well under the 60 limit.
        time.sleep(1.0)

    # Return the union of refreshed + already-fresh data so callers can use it.
    for r in rows:
        if r["ticker"] not in out and r["forward_pe"] is not None:
            out[r["ticker"]] = _row_to_market_data(r)

    return out


def fetch_market_data(
    tickers: list[str],
    db_path: Path = DB_PATH,
    use_cache: bool = True,
    verbose: bool = True,
    progress_cb=None,
) -> dict[str, MarketData]:
    """Fetch market data for a list of tickers, parallelized with caching.

    progress_cb(done: int, total: int) is called after each successful fetch.
    """
    init_market_data_table(db_path)
    if not tickers:
        return {}

    out: dict[str, MarketData] = {}
    now = time.time()
    finnhub_key = os.environ.get("FINNHUB_API_KEY") or None

    # Pull cached rows.
    cached: dict[str, MarketData] = {}
    if use_cache:
        with get_conn(db_path) as conn:
            placeholders = ",".join("?" * len(tickers))
            rows = conn.execute(
                f"SELECT * FROM market_data WHERE ticker IN ({placeholders})",
                tickers,
            ).fetchall()
        for r in rows:
            md = _row_to_market_data(r)
            if (now - md.fetched_at) < CACHE_TTL_SECONDS:
                cached[md.ticker] = md

    to_fetch = [t for t in tickers if t not in cached]
    if verbose:
        suffix = " (with Finnhub fundamentals)" if finnhub_key else ""
        print(f"  market data: {len(cached)} from cache, {len(to_fetch)} to fetch{suffix}")

    out.update(cached)

    if not to_fetch:
        return out

    done = len(cached)
    total = len(tickers)
    failures = 0

    # Concurrency: a few parallel HTTP fetches. Yahoo handles ~8 concurrent
    # cleanly; Finnhub free tier is 60/min so we keep concurrency modest.
    concurrency = min(MAX_CONCURRENT_FETCHES, len(to_fetch))
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(_fetch_one, t, finnhub_key): t for t in to_fetch}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                md = fut.result()
            except Exception:
                md = None
            if md:
                _save(md, db_path)
                # IMPORTANT: re-read from DB to pick up the COALESCE'd values.
                # If Finnhub rate-limited this fetch (so the new md has
                # forward_pe=None) but a previous run had real fundamentals,
                # the COALESCE in _save preserves them in the DB. We need to
                # return THAT merged view to the caller, not the rate-limited
                # snapshot we just constructed.
                with get_conn(db_path) as conn:
                    row = conn.execute(
                        "SELECT * FROM market_data WHERE ticker=?", (t,)
                    ).fetchone()
                if row:
                    out[t] = _row_to_market_data(row)
                else:
                    out[t] = md  # shouldn't happen since we just saved
            else:
                failures += 1
            done += 1
            if progress_cb:
                progress_cb(done, total)

    if verbose and failures:
        print(f"    market data: {failures} fetch failures out of {len(to_fetch)}")

    return out
