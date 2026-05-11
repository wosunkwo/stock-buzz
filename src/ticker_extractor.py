"""Extract stock ticker mentions from Reddit post text.

Strategy:
1. Find candidates with two patterns: `$TICKER` (cashtag) and bare ALL-CAPS
   tokens 1-5 letters long.
2. Filter against KNOWN_TICKERS to drop false positives like "DD", "USA",
   "CEO", "YOLO", "HODL".
3. Cashtag matches get a bonus signal weight — they're more intentional than
   bare tokens.

Returns a per-post dict of {ticker: weight} so downstream scoring can use it.
"""
from __future__ import annotations

import re
from collections import defaultdict

from .known_tickers import KNOWN_TICKERS

# `$TSLA`, `$brk.b` — case insensitive, allow a single dot for share classes.
CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,5}(?:\.[A-Za-z])?)\b")

# Bare ALL-CAPS tokens 1-5 letters. Word boundaries to avoid matching inside
# words ("THE" inside "THEORY" wouldn't match anyway thanks to \b).
BARE_RE = re.compile(r"\b([A-Z]{1,5}(?:\.[A-Z])?)\b")

# Even with the known-ticker filter, a few tickers collide with very common
# English words. Require those to appear with a `$` prefix to count.
REQUIRES_CASHTAG = {"A", "I", "AI", "ALL", "AT", "BE", "BY", "CAN", "DO",
                    "FOR", "GO", "HE", "IT", "ON", "OR", "SO", "TO", "U",
                    "ARE", "HAS", "HAD", "WAS", "ANY", "OUT", "GET", "NEW",
                    "ONE", "TWO", "OUR", "OPEN", "REAL", "WELL", "GOOD",
                    "BIG", "LOW", "KEY", "EV", "FREE", "NICE", "NEXT",
                    "MAN", "SEE", "NOW", "LIFE", "EVER", "MAYBE",
                    "EPS", "CEO", "CFO", "COO", "ETF", "IPO", "DD",
                    "YOLO", "HODL", "FOMO", "USA", "USD", "GDP", "FED",
                    "SEC", "IRS", "FBI", "NYC", "LA", "SF",
                    "AM", "PM", "EST", "PST", "EOD", "EOW", "OK"}


def extract_tickers(text: str) -> dict[str, float]:
    """Return {ticker: weight} for tickers mentioned in `text`.

    Weight: 1.0 for bare mention, 1.5 for cashtag mention. If both forms
    appear the higher weight wins (we don't double-count).
    """
    if not text:
        return {}

    weights: dict[str, float] = defaultdict(float)

    # Cashtags first (higher signal).
    for m in CASHTAG_RE.findall(text):
        ticker = m.upper()
        if ticker in KNOWN_TICKERS:
            weights[ticker] = max(weights[ticker], 1.5)

    # Bare uppercase tokens.
    for m in BARE_RE.findall(text):
        ticker = m.upper()
        if ticker not in KNOWN_TICKERS:
            continue
        if ticker in REQUIRES_CASHTAG:
            continue  # Only count when prefixed with $ (handled above).
        weights[ticker] = max(weights[ticker], 1.0)

    return dict(weights)


def extract_from_post(title: str, selftext: str) -> dict[str, float]:
    """Extract tickers from a post's title + body, summing weights across both."""
    title_hits = extract_tickers(title)
    body_hits = extract_tickers(selftext)

    combined: dict[str, float] = defaultdict(float)
    # Title mentions are weighted 2x because titles are short and intentional.
    for t, w in title_hits.items():
        combined[t] += w * 2.0
    for t, w in body_hits.items():
        combined[t] += w
    return dict(combined)
