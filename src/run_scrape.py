"""End-to-end pipeline.

Sources:
  - StockTwits: trending tickers + per-symbol message streams
  - Apewisdom: aggregated mention counts across investing subreddits
  - Hacker News (Algolia): tech-leaning ticker coverage

  → ticker extraction
  → store posts and mentions
  → score buzz
  → fetch live market data for ALL ranked tickers
  → fetch earnings + trusted-source news for top N
  → Claude-summarize top N + favorites
  → render the dashboard

Run from CLI:
    python -m src.run_scrape

Or call `main(progress_cb=...)` from a server to stream progress.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from .apewisdom_client import fetch_apewisdom
from .earnings import fetch_for_tickers as fetch_earnings_for_tickers
from .hackernews_client import fetch_hackernews
from .known_tickers import KNOWN_TICKERS
from .market_data import ensure_fundamentals, fetch_market_data
from .metrics_explainer import explain_metrics
from .report import render_report
from .scoring import compute_buzz
from .stocktwits_client import fetch_trending_with_messages
from .storage import (
    finish_run,
    init_db,
    insert_mentions,
    insert_trending_snapshot,
    start_run,
    upsert_post,
)
from .summarizer import summarize_top_tickers
from .ticker_extractor import extract_from_post

# How many top-buzz tickers to fetch market data + AI summaries for. The rest
# still appear in the report but without prices/summaries (this controls cost).
TOP_N_FOR_SUMMARY = 30

# Earnings + news are fetched for the top N to keep us under Finnhub's free
# tier limit (60 req/min). Each ticker takes ~3 calls. With cache, repeat
# refreshes only fetch tickers whose 6h cache has expired.
TOP_N_FOR_EARNINGS = 50

# Progress callback signature: cb(phase: str, detail: str, fraction: float|None).
# fraction is 0.0–1.0 if known, else None.
ProgressCb = Callable[[str, str, Optional[float]], None]


def _noop_progress(phase: str, detail: str, fraction: Optional[float]) -> None:
    pass


def _ingest_stocktwits(run_id: int) -> tuple[int, int]:
    print("Fetching from StockTwits...")
    pairs = fetch_trending_with_messages(trending_limit=30, messages_per_symbol=30)

    trending_entries = [
        {"ticker": sym.symbol, "title": sym.title, "watchlist_count": sym.watchlist_count}
        for sym, _ in pairs
    ]
    insert_trending_snapshot(run_id, source="stocktwits", entries=trending_entries)

    posts_seen = 0
    mentions_seen = 0
    for sym, msgs in pairs:
        for m in msgs:
            posts_seen += 1
            upsert_post(
                source="stocktwits",
                source_id=str(m.id),
                channel=sym.symbol,
                title=None,
                body=m.body,
                author=m.username,
                sentiment=m.sentiment,
                user_followers=m.user_followers,
                created_utc=m.created_at_dt.timestamp(),
                permalink=f"https://stocktwits.com/{m.username}/message/{m.id}",
            )

            text_hits = extract_from_post("", m.body)
            api_hits = {s.upper(): 1.0 for s in m.symbols if s}
            api_hits[sym.symbol.upper()] = max(api_hits.get(sym.symbol.upper(), 0), 1.5)

            combined: dict[str, float] = {}
            for t, w in text_hits.items():
                combined[t] = max(combined.get(t, 0), w)
            for t, w in api_hits.items():
                combined[t] = max(combined.get(t, 0), w)

            mentions_seen += insert_mentions(
                run_id, source="stocktwits", source_id=str(m.id),
                ticker_weights=combined,
            )

    return posts_seen, mentions_seen


def _ingest_apewisdom(run_id: int) -> tuple[int, int]:
    """Ingest aggregated Reddit-buzz from Apewisdom.

    Apewisdom returns aggregate mention counts per ticker per subreddit
    rather than individual posts. We synthesize one "post" per
    (ticker, subreddit) pair, with body describing the aggregate signal.
    Mention weight scales with mention count and 24h velocity, so a ticker
    that just surged in mentions ranks higher than one that's been steady.
    """
    print("Fetching from Apewisdom (Reddit aggregate)...")
    entries = fetch_apewisdom()
    if not entries:
        print("  no Apewisdom data retrieved.")
        return 0, 0

    posts_seen = 0
    mentions_seen = 0
    import time as _time
    now = _time.time()

    for e in entries:
        # Drop noise: tickers we've never heard of are usually false positives
        # (Apewisdom catches some non-stock all-caps tokens).
        if e.ticker not in KNOWN_TICKERS:
            continue

        source_id = f"{e.filter_name}:{e.ticker}"
        # Synthesize post body so the modal sample-posts list shows useful
        # context for the user about *why* this ticker is on Apewisdom.
        body_parts = [
            f"{e.mentions} mentions on r/{e.filter_name}",
        ]
        if e.mentions_24h_ago is not None:
            growth = e.mention_growth_pct
            if growth is not None:
                arrow = "↑" if growth > 0 else "↓"
                body_parts.append(f"{arrow}{abs(growth):.0f}% from 24h ago ({e.mentions_24h_ago})")
        if e.rank_24h_ago is not None:
            delta = e.rank_delta
            if delta and abs(delta) >= 1:
                arrow = "↑" if delta > 0 else "↓"
                body_parts.append(f"rank {arrow}{abs(delta)} (was #{e.rank_24h_ago})")
        body = " · ".join(body_parts)

        upsert_post(
            source="reddit_aggregate",
            source_id=source_id,
            channel=e.filter_name,
            title=None,
            body=body,
            author=None,
            score=e.upvotes,         # using upvotes as a proxy "score"
            num_comments=e.mentions, # mention count → buzz scoring uses this
            created_utc=now,
            permalink=f"https://apewisdom.io/stocks/{e.ticker}/",
        )
        posts_seen += 1

        # Mention weight: log-scale the mention count so a ticker with 500
        # mentions doesn't completely drown one with 50. Boost by velocity:
        # a +100% growth ticker gets ~1.3x of an unchanged one.
        import math as _math
        base_weight = _math.log1p(max(e.mentions, 1))
        velocity = e.mention_growth_pct
        if velocity is not None and velocity > 0:
            base_weight *= 1.0 + min(velocity, 200) / 300.0  # cap at 1.67x
        mentions_seen += insert_mentions(
            run_id, source="reddit_aggregate", source_id=source_id,
            ticker_weights={e.ticker: base_weight},
        )

    return posts_seen, mentions_seen


def _ingest_hackernews(run_id: int, candidate_tickers: list[str],
                       name_hints: dict[str, str]) -> tuple[int, int]:
    """Ingest HN stories about each candidate ticker. Each HN story becomes
    its own post with title + URL + points + comment count, so the AI
    summarizer can use them as context."""
    print(f"Fetching from Hacker News for {len(candidate_tickers)} candidate tickers...")
    by_ticker = fetch_hackernews(candidate_tickers, name_hints=name_hints, verbose=False)
    if not by_ticker:
        print("  no HN coverage found for these tickers.")
        return 0, 0

    posts_seen = 0
    mentions_seen = 0
    for ticker, stories in by_ticker.items():
        for s in stories:
            source_id = s.object_id
            if not source_id:
                continue
            upsert_post(
                source="hackernews",
                source_id=source_id,
                channel="HN",
                title=s.title,
                body=s.title,  # HN stories don't have body text in search results
                author=s.author,
                score=s.points,
                num_comments=s.num_comments,
                created_utc=s.created_at_unix,
                permalink=s.display_url,  # external URL preferred, fall back to HN
            )
            posts_seen += 1

            # Weight by points + comments engagement.
            import math as _math
            weight = 1.5 + _math.log1p(s.points) / 4.0 + _math.log1p(s.num_comments) / 6.0
            mentions_seen += insert_mentions(
                run_id, source="hackernews", source_id=source_id,
                ticker_weights={ticker: weight},
            )

    return posts_seen, mentions_seen


def main(progress_cb: Optional[ProgressCb] = None) -> int:
    cb = progress_cb or _noop_progress

    # Favorites come in via env from the server's /refresh handler. Empty if
    # invoked from the CLI (which is fine — favorites are a UI concept).
    favorites_env = os.environ.get("STOCK_BUZZ_FAVORITES", "").strip()
    favorites: list[str] = (
        [f.strip().upper() for f in favorites_env.split(",") if f.strip()]
        if favorites_env else []
    )
    if favorites:
        print(f"Favorites (will always get AI summaries): {favorites}")

    cb("init", "Initializing database…", 0.02)
    print("Initializing database...")
    init_db()
    run_id = start_run()

    cb("scrape_stocktwits", "Fetching trending tickers from StockTwits…", 0.05)
    st_posts, st_mentions = _ingest_stocktwits(run_id)
    print(f"  StockTwits: {st_posts} messages, {st_mentions} mentions")
    cb("scrape_stocktwits", f"StockTwits: {st_posts} messages, {st_mentions} mentions", 0.20)

    cb("scrape_apewisdom", "Fetching aggregate Reddit buzz from Apewisdom…", 0.22)
    aw_posts, aw_mentions = _ingest_apewisdom(run_id)
    print(f"  Apewisdom:  {aw_posts} entries, {aw_mentions} mentions")
    cb("scrape_apewisdom", f"Apewisdom: {aw_posts} entries, {aw_mentions} mentions", 0.30)

    # Hacker News: only search for tickers that already have buzz from the
    # other two sources, plus favorites. This keeps HN searches tight and
    # avoids cluttering with tickers nobody else is talking about.
    cb("score", "Computing initial buzz to pick HN candidates…", 0.32)
    interim_buzz = compute_buzz()
    candidate_tickers = [t.ticker for t in interim_buzz[:50]]
    candidate_tickers.extend([f for f in favorites if f not in candidate_tickers])
    # Build name hints for short tickers from market data isn't available yet,
    # so use a small static fallback for the most common short ones we care about.
    name_hints = {
        "AI": "C3.ai",
        "U": "Unity",
        "F": "Ford",
        "C": "Citigroup",
        "V": "Visa",
        "T": "AT&T",
    }
    cb("scrape_hn", f"Searching Hacker News for {len(candidate_tickers)} tickers…", 0.34)
    hn_posts, hn_mentions = _ingest_hackernews(run_id, candidate_tickers, name_hints)
    print(f"  Hacker News: {hn_posts} stories, {hn_mentions} mentions")

    total_posts = st_posts + aw_posts + hn_posts
    total_mentions = st_mentions + aw_mentions + hn_mentions
    finish_run(run_id, posts_seen=total_posts, mentions_seen=total_mentions)

    cb("score", "Computing buzz scores…", 0.40)
    print("\nComputing final buzz scores...")
    buzz = compute_buzz()
    print(f"  scored {len(buzz)} unique tickers")

    # Inject any favorites that weren't buzzy enough to score. They get a
    # placeholder TickerBuzz so the rest of the pipeline (market data,
    # earnings, summaries, render) treats them like any other ticker.
    if favorites:
        from .scoring import TickerBuzz
        existing = {b.ticker for b in buzz}
        for fav in favorites:
            if fav in existing:
                continue
            buzz.append(TickerBuzz(
                ticker=fav, score=0.0, mention_count=0, distinct_posts=0,
                distinct_channels=0, distinct_sources=0,
                total_upvotes=0, total_comments=0, total_followers=0,
                bullish_count=0, bearish_count=0, trending_rank=None,
                sample_posts=[],
            ))
        print(f"  added {len(favorites) - len(existing & set(favorites))} un-buzzed favorites")

    if not buzz:
        cb("done", "No tickers to render.", 1.0)
        print("\nNo tickers to render. Done.")
        return 0

    # Market data: fetch for ALL ranked tickers (price + 52w + volume always;
    # P/E + market cap + earnings only when FINNHUB_API_KEY is set).
    all_tickers = [t.ticker for t in buzz]
    cb("market", f"Fetching market data for {len(all_tickers)} tickers…", 0.42)
    print(f"\nFetching market data for {len(all_tickers)} tickers...")

    def _market_progress(done, total):
        # Reserve [0.42, 0.55] for market-data fetching.
        frac = 0.42 + (0.55 - 0.42) * (done / max(total, 1))
        cb("market", f"Fetched market data for {done}/{total} tickers", frac)

    market_data = fetch_market_data(all_tickers, progress_cb=_market_progress)

    # Favorites get a guaranteed-fundamentals retry pass. The bulk fetch above
    # is parallel and can lose Finnhub calls to rate-limiting; this pass is
    # sequential with a 1s gap so favorites always end up with forward P/E,
    # margins, ROE, etc. populated (assuming Finnhub knows the ticker).
    if favorites:
        cb("market_favs", f"Ensuring fundamentals for {len(favorites)} favorites…", 0.50)
        print(f"\nEnsuring Finnhub fundamentals for {len(favorites)} favorites...")
        fav_md = ensure_fundamentals(favorites)
        # Merge the refreshed favorite data on top of bulk market_data.
        market_data.update(fav_md)

    # Include favorites in the earnings fetch list so favorites always have
    # earnings data, regardless of buzz rank.
    earnings_tickers = list({t.ticker for t in buzz[:TOP_N_FOR_EARNINGS]} | set(favorites))
    cb("earnings", f"Fetching earnings + news for {len(earnings_tickers)} tickers…", 0.52)
    print(f"\nFetching earnings + news for {len(earnings_tickers)} tickers...")
    earnings_data = fetch_earnings_for_tickers(earnings_tickers)

    summarize_count = TOP_N_FOR_SUMMARY + len([f for f in favorites if f not in {b.ticker for b in buzz[:TOP_N_FOR_SUMMARY]}])
    cb("summarize", f"Generating AI summaries for top {TOP_N_FOR_SUMMARY} + {len(favorites)} favorites…", 0.55)
    print(f"\nGenerating Claude summaries for top {TOP_N_FOR_SUMMARY} tickers + {len(favorites)} favorites...")

    # Wrap summarize_top_tickers with a per-ticker progress callback by patching
    # its verbose printf to also call our progress hook. Simplest path: count
    # summaries by polling the dict afterward isn't useful — instead we let
    # summarize_top_tickers do its thing and just send a high-level update at
    # the end. This keeps the summarizer module untouched.
    summaries = summarize_top_tickers(buzz, market_data, top_n=TOP_N_FOR_SUMMARY,
                                      progress_cb=cb, extra_tickers=favorites)

    # Auto-generate metrics-explainer for each favorite so the modal shows the
    # plain-English explanation immediately on open (no on-demand click). We
    # run these in parallel with bounded concurrency. Skips silently for
    # favorites without fundamentals (Finnhub didn't recognize the ticker).
    if favorites:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from .summarizer import _resolve_provider, _resolve_model, _make_client
        prov = _resolve_provider()
        if prov != "none":
            fav_model = _resolve_model(prov)  # uses STOCK_BUZZ_MODEL (favorite-tier)
            client = _make_client(prov)
            if client is not None:
                cb("explain_favs", f"Explaining metrics for {len(favorites)} favorites…", 0.96)
                print(f"\nGenerating metrics explainer for {len(favorites)} favorites...")
                eligible = [f for f in favorites
                            if f in market_data and market_data[f].forward_pe is not None]
                if eligible:
                    succeeded = 0
                    with ThreadPoolExecutor(max_workers=3) as ex:
                        futs = {
                            ex.submit(
                                explain_metrics,
                                market_data[f],
                                client=client,
                                model=fav_model,
                                provider=prov,
                            ): f for f in eligible
                        }
                        for fut in as_completed(futs):
                            f = futs[fut]
                            try:
                                exp = fut.result()
                                if exp:
                                    succeeded += 1
                                    print(f"    ✓ ${f} metrics explained")
                                else:
                                    print(f"    ✗ ${f} explainer returned nothing")
                            except Exception as e:
                                print(f"    ✗ ${f} explainer error: {type(e).__name__}: {str(e)[:100]}")
                    print(f"  metrics-explainer: {succeeded}/{len(eligible)} favorites done")
                else:
                    print(f"  metrics-explainer: no favorites have fundamentals to explain")

    if buzz:
        print("\nTop 15 buzzy tickers:")
        for i, t in enumerate(buzz[:15], 1):
            md = market_data.get(t.ticker)
            price_str = ""
            if md and md.price is not None:
                pct = md.percent_change
                pct_str = f" ({pct:+.2f}%)" if pct is not None else ""
                price_str = f"  ${md.price:.2f}{pct_str}"
            sum_str = " 🤖" if t.ticker in summaries else ""
            print(f"  {i:>2}. ${t.ticker:<6} score={t.score:<8} "
                  f"posts={t.distinct_posts:<3}{price_str}{sum_str}")

    cb("render", "Rendering dashboard…", 0.97)
    print("\nRendering report...")
    has_api_key = bool(
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("STOCK_BUZZ_PROVIDER", "").lower() == "bedrock"
        or os.environ.get("AWS_PROFILE")
    )
    path = render_report(
        tickers=buzz,
        market_data=market_data,
        summaries=summaries,
        earnings_data=earnings_data,
        total_posts=total_posts,
        total_mentions=total_mentions,
        has_api_key=has_api_key,
    )
    print(f"Report written to: {path}")
    cb("done", f"Done. {len(buzz)} tickers, {len(summaries)} AI summaries.", 1.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
