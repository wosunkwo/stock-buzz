"""Buzz scoring: aggregate mentions over a rolling window into a per-ticker score.

Inputs into the score:
- mention count (with recency decay)
- breadth (distinct sources/channels — cross-platform > single-platform)
- engagement (reddit upvotes + comments; stocktwits user followers)
- bullish/bearish ratio from StockTwits sentiment-tagged messages
- platform-trending boost (StockTwits "trending" list ranking)

Phase 1 formula (intentionally simple; tune later):

    base       = sum_over_posts( mention_weight * recency_decay )
    engagement = log1p(reddit_upvotes + reddit_comments + stocktwits_followers / 50)
    breadth    = number_of_distinct_channels
    score      = base * (1 + engagement / 5) * (1 + 0.25 * breadth) * (1 + trending_boost)
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from .config import BUZZ_WINDOW_HOURS, DB_PATH
from .storage import get_conn


@dataclass
class TickerBuzz:
    ticker: str
    score: float
    mention_count: int
    distinct_posts: int
    distinct_channels: int
    distinct_sources: int
    total_upvotes: int
    total_comments: int
    total_followers: int
    bullish_count: int
    bearish_count: int
    trending_rank: int | None  # best (lowest) rank seen on a trending list, or None
    sample_posts: list[dict] = field(default_factory=list)


def _recency_decay(post_age_hours: float, half_life_hours: float = 12.0) -> float:
    return 0.5 ** (post_age_hours / half_life_hours)


def compute_buzz(window_hours: int = BUZZ_WINDOW_HOURS,
                 db_path: Path = DB_PATH) -> list[TickerBuzz]:
    cutoff = time.time() - timedelta(hours=window_hours).total_seconds()

    with get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT m.ticker, m.weight,
                   p.source, p.source_id, p.channel, p.title, p.body,
                   p.score AS post_score, p.num_comments,
                   p.sentiment, p.user_followers,
                   p.permalink, p.created_utc
            FROM mentions m
            JOIN posts p ON p.source = m.source AND p.source_id = m.source_id
            WHERE p.created_utc >= ?
            """,
            (cutoff,),
        ).fetchall()

        # Best (lowest) trending rank in this window per ticker.
        trending_rows = conn.execute(
            """
            SELECT ticker, MIN(rank) AS best_rank
            FROM trending
            WHERE captured_at >= datetime(?, 'unixepoch')
            GROUP BY ticker
            """,
            (cutoff,),
        ).fetchall()
    trending_best: dict[str, int] = {r["ticker"]: r["best_rank"] for r in trending_rows}

    by_ticker: dict[str, dict] = {}

    for r in rows:
        t = r["ticker"]
        b = by_ticker.setdefault(
            t,
            {
                "weighted_sum": 0.0,
                "mention_count": 0,
                "post_keys": set(),
                "channels": set(),
                "sources": set(),
                "total_upvotes": 0,
                "total_comments": 0,
                "total_followers": 0,
                "bullish": 0,
                "bearish": 0,
                "posts": {},  # (source, source_id) -> dict
            },
        )

        age_hours = (time.time() - r["created_utc"]) / 3600.0
        decay = _recency_decay(age_hours)
        b["weighted_sum"] += r["weight"] * decay
        b["mention_count"] += 1

        post_key = (r["source"], r["source_id"])
        b["post_keys"].add(post_key)
        if r["channel"]:
            b["channels"].add(f"{r['source']}:{r['channel']}")
        b["sources"].add(r["source"])

        if post_key not in b["posts"]:
            display_text = r["title"] or (r["body"] or "")[:140]
            b["posts"][post_key] = {
                "source": r["source"],
                "channel": r["channel"],
                "text": display_text,
                # All current sources store a fully-qualified URL in permalink
                # (StockTwits messages, Apewisdom ticker pages, HN article URLs).
                "url": r["permalink"] or "",
                "score": r["post_score"] or 0,
                "num_comments": r["num_comments"] or 0,
                "sentiment": r["sentiment"],
                "followers": r["user_followers"] or 0,
            }
            b["total_upvotes"] += r["post_score"] or 0
            b["total_comments"] += r["num_comments"] or 0
            b["total_followers"] += r["user_followers"] or 0
            if r["sentiment"] == "Bullish":
                b["bullish"] += 1
            elif r["sentiment"] == "Bearish":
                b["bearish"] += 1

    results: list[TickerBuzz] = []
    for ticker, b in by_ticker.items():
        engagement_raw = b["total_upvotes"] + b["total_comments"] + b["total_followers"] / 50.0
        engagement = math.log1p(max(0.0, engagement_raw))
        breadth = len(b["channels"])

        # Trending boost: rank 1 gives ~30% boost, rank 30 gives ~3%, otherwise 0.
        rank = trending_best.get(ticker)
        trending_boost = max(0.0, (31 - rank) / 100.0) if rank else 0.0

        score = b["weighted_sum"] * (1 + engagement / 5.0) * (1 + 0.25 * breadth) * (1 + trending_boost)

        # Build a sample that's diversified across sources, so the modal's
        # "top posts" list shows a mix of StockTwits + Apewisdom + Hacker News
        # rather than only one source. Within each source, rank by an
        # appropriate "engagement" metric since each source's score/followers
        # mean different things:
        #   - stocktwits: prefer messages with explicit Bull/Bear sentiment,
        #     then by user follower count
        #   - reddit_aggregate (apewisdom): rank by mention count
        #     (stored as `num_comments`)
        #   - hackernews: rank by points + comments (both real)
        def _rank_within_source(p: dict) -> float:
            src = (p.get("source") or "").lower()
            if src == "stocktwits":
                base = p.get("followers") or 0
                return base + (1_000_000 if p.get("sentiment") else 0)
            if src == "reddit_aggregate":
                return float(p.get("num_comments") or 0)  # mentions
            if src == "hackernews":
                return float((p.get("score") or 0) + (p.get("num_comments") or 0))
            # Unknown sources fall back to score
            return float(p.get("score") or 0)

        # Group by source, sort each group by its own rank key.
        by_source: dict[str, list[dict]] = {}
        for p in b["posts"].values():
            by_source.setdefault((p.get("source") or "?").lower(), []).append(p)
        for src in by_source:
            by_source[src].sort(key=_rank_within_source, reverse=True)

        # Round-robin pick: take the top from each source in order, cycling
        # until we have 5 (or run out). This guarantees that if all 3 sources
        # have posts for this ticker, all 3 appear in the sample. We use a
        # stable preference order so display is deterministic across runs.
        SOURCE_PREFERENCE = ["stocktwits", "reddit_aggregate", "hackernews"]
        active_sources = [s for s in SOURCE_PREFERENCE if s in by_source]
        # Add any sources we didn't predict.
        for s in by_source:
            if s not in active_sources:
                active_sources.append(s)

        all_posts: list[dict] = []
        idx = {s: 0 for s in active_sources}
        while len(all_posts) < 5:
            advanced = False
            for s in active_sources:
                lst = by_source.get(s, [])
                if idx[s] < len(lst):
                    all_posts.append(lst[idx[s]])
                    idx[s] += 1
                    advanced = True
                    if len(all_posts) >= 5:
                        break
            if not advanced:
                break

        results.append(
            TickerBuzz(
                ticker=ticker,
                score=round(score, 2),
                mention_count=b["mention_count"],
                distinct_posts=len(b["post_keys"]),
                distinct_channels=breadth,
                distinct_sources=len(b["sources"]),
                total_upvotes=b["total_upvotes"],
                total_comments=b["total_comments"],
                total_followers=b["total_followers"],
                bullish_count=b["bullish"],
                bearish_count=b["bearish"],
                trending_rank=rank,
                sample_posts=all_posts[:5],
            )
        )

    results.sort(key=lambda r: r.score, reverse=True)
    return results
