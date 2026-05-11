"""SQLite storage for posts/messages, mentions, and run snapshots.

Schema:
- runs: one row per scrape run
- posts: every social post we've seen, keyed by (source, source_id).
         Source is 'reddit' or 'stocktwits'. Reddit fields like score/comments
         become null for stocktwits; stocktwits-specific fields like user_followers
         are stored in extras_json for flexibility.
- mentions: many-to-many — which tickers appeared in which post (and weight)
- trending: snapshot of platform-reported trending lists (e.g. StockTwits trending)

Design note: we use a single posts table with a `source` column rather than
separate tables because downstream scoring treats them as the same kind of
"signal". Adding new sources later (Bluesky, Twitter) only requires another
fetcher, not a schema change.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    posts_seen INTEGER DEFAULT 0,
    mentions_seen INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS posts (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    channel TEXT,            -- subreddit name, or stocktwits symbol the message stream came from
    title TEXT,              -- reddit only
    body TEXT,
    author TEXT,
    score INTEGER,           -- reddit upvotes; null for stocktwits
    num_comments INTEGER,    -- reddit only
    sentiment TEXT,          -- stocktwits 'Bullish'/'Bearish'; null otherwise
    user_followers INTEGER,  -- stocktwits only
    created_utc REAL NOT NULL,
    permalink TEXT,
    extras_json TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_utc);
CREATE INDEX IF NOT EXISTS idx_posts_source ON posts(source);

CREATE TABLE IF NOT EXISTS mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    weight REAL NOT NULL,
    FOREIGN KEY(run_id) REFERENCES runs(id)
);

CREATE INDEX IF NOT EXISTS idx_mentions_ticker ON mentions(ticker);
CREATE INDEX IF NOT EXISTS idx_mentions_run ON mentions(run_id);

CREATE TABLE IF NOT EXISTS trending (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    rank INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    title TEXT,
    watchlist_count INTEGER,
    captured_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES runs(id)
);

CREATE INDEX IF NOT EXISTS idx_trending_ticker ON trending(ticker);
CREATE INDEX IF NOT EXISTS idx_trending_run ON trending(run_id);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn(db_path: Path = DB_PATH):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path = DB_PATH) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)


def start_run(db_path: Path = DB_PATH) -> int:
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO runs(started_at) VALUES(?)", (_now_iso(),)
        )
        return cur.lastrowid


def finish_run(run_id: int, posts_seen: int, mentions_seen: int,
               db_path: Path = DB_PATH) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE runs SET finished_at=?, posts_seen=?, mentions_seen=? WHERE id=?",
            (_now_iso(), posts_seen, mentions_seen, run_id),
        )


def upsert_post(
    *,
    source: str,
    source_id: str,
    channel: str | None,
    body: str,
    created_utc: float,
    title: str | None = None,
    author: str | None = None,
    score: int | None = None,
    num_comments: int | None = None,
    sentiment: str | None = None,
    user_followers: int | None = None,
    permalink: str | None = None,
    extras: dict | None = None,
    db_path: Path = DB_PATH,
) -> None:
    now = _now_iso()
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO posts (
                source, source_id, channel, title, body, author, score,
                num_comments, sentiment, user_followers, created_utc, permalink,
                extras_json, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, source_id) DO UPDATE SET
                score = COALESCE(excluded.score, posts.score),
                num_comments = COALESCE(excluded.num_comments, posts.num_comments),
                sentiment = COALESCE(excluded.sentiment, posts.sentiment),
                user_followers = COALESCE(excluded.user_followers, posts.user_followers),
                last_seen_at = excluded.last_seen_at
            """,
            (
                source, source_id, channel, title, body, author, score,
                num_comments, sentiment, user_followers, created_utc, permalink,
                json.dumps(extras) if extras else None, now, now,
            ),
        )


def insert_mentions(
    run_id: int, source: str, source_id: str,
    ticker_weights: dict[str, float], db_path: Path = DB_PATH,
) -> int:
    if not ticker_weights:
        return 0
    rows = [(run_id, source, source_id, ticker, weight)
            for ticker, weight in ticker_weights.items()]
    with get_conn(db_path) as conn:
        conn.executemany(
            "INSERT INTO mentions(run_id, source, source_id, ticker, weight) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def latest_run_finished_at(db_path: Path = DB_PATH) -> str | None:
    """ISO-8601 UTC timestamp of the most recent run that finished, or None
    if no completed run exists."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT finished_at FROM runs WHERE finished_at IS NOT NULL "
            "ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
    return row["finished_at"] if row else None


def insert_trending_snapshot(
    run_id: int,
    source: str,
    entries: list[dict],
    db_path: Path = DB_PATH,
) -> int:
    """entries: [{'ticker': 'NVDA', 'title': '...', 'watchlist_count': 12345}, ...] in trending order."""
    if not entries:
        return 0
    now = _now_iso()
    rows = [
        (run_id, source, i + 1, e["ticker"], e.get("title"), e.get("watchlist_count"), now)
        for i, e in enumerate(entries)
    ]
    with get_conn(db_path) as conn:
        conn.executemany(
            """INSERT INTO trending(run_id, source, rank, ticker, title, watchlist_count, captured_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
    return len(rows)
