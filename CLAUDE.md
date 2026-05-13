# Stock Buzz — context for Claude Code

This file captures non-obvious project context: technical decisions, conventions,
preferences, and gotchas that aren't visible from reading the code alone. Read
this first when working on the project.

## What this project is

A personal stock-buzz dashboard. Pipeline scrapes social-media chatter for
ticker mentions, scores buzz, fetches live prices + fundamentals + earnings,
and uses Claude to produce ELI15 / standard / bull / bear summaries plus a
plain-English explainer of the fundamentals. Renders to a single-page Flask
app at `http://localhost:8765/`.

Repo: https://github.com/wosunkwo/stock-buzz

## Architecture

```
Sources       StockTwits    Apewisdom    Hacker News
                  │             │             │
                  └─────────────┼─────────────┘
                                ▼
Storage           SQLite (data/buzz.db) — posts, mentions, runs, summaries,
                  metrics_explanations, earnings_data, market_data, trending
                                │
                                ▼
Scoring           src/scoring.py — buzz score per ticker
                                │
                                ▼
Enrichment        market_data (Yahoo + Finnhub) → earnings (Finnhub) → AI
                                │
                                ▼
Frontend          Jinja2 → Flask serves output/report.html
                  Endpoints: /refresh, /status, /summarize/<t>,
                             /metrics-explain/<t>, /shutdown, /restart
```

### File map (`src/`)

| File | Role |
|---|---|
| `run_scrape.py` | Pipeline orchestrator. CLI entry point AND server-driven entry. Takes `progress_cb`. Reads `STOCK_BUZZ_FAVORITES` env. |
| `stocktwits_client.py` | Trending list + per-symbol message streams. Parallel. |
| `apewisdom_client.py` | Reddit-aggregate buzz scraper (10 subreddits, 24h velocity). |
| `hackernews_client.py` | Algolia HN search per candidate ticker, with ETF/short-ticker skiplist. |
| `ticker_extractor.py` | Regex + `KNOWN_TICKERS` filter. |
| `known_tickers.py` | Hand-curated ticker allowlist (~350 entries). |
| `scoring.py` | Buzz scoring; sample-post round-robin across sources. |
| `sectors.py` | Hand-curated `TICKER_TO_SECTOR` + `ALWAYS_SHOW_SECTORS`. |
| `market_data.py` | Yahoo chart + Finnhub fundamentals (parallel). `ensure_fundamentals()` for favorites. |
| `earnings.py` | Finnhub earnings + trusted-source-ranked news + SEC EDGAR link. |
| `summarizer.py` | Claude calls. Two-tier model. Parallel with bounded workers. |
| `metrics_explainer.py` | "Explain like I'm 15" prompt for fundamentals. |
| `report.py` | Jinja2 dashboard rendering. All the modal/sidebar JS lives here. |
| `server.py` | Flask. Background-thread refresh, status polling, on-demand endpoints. |
| `storage.py` | SQLite schema + connection helpers. |
| `config.py` | Loads `.env` via python-dotenv. Project paths. |

## Non-obvious technical decisions

### LLM provider and model defaults
- **Bedrock is the default daily driver**, not direct Anthropic API. Reason:
  user doesn't want out-of-pocket Anthropic invoicing. Both providers work
  via `STOCK_BUZZ_PROVIDER=bedrock|anthropic|none`. Pricing per token is
  identical; only the invoice changes.
- Bedrock model IDs use the `us.anthropic.<name>` cross-region inference
  profile format. Verify available IDs with
  `aws bedrock list-foundation-models --region us-east-1` — the SDK does
  not cleanly error on a stale ID.
- **Two-tier model:** bulk top-N uses Haiku 4.5 (cheap+fast), favorites use
  the configured `STOCK_BUZZ_MODEL` (default Sonnet 4.6). Implemented in
  `summarizer.summarize_top_tickers` via `bulk_model` parameter.
- The frontend localStorage default is **Haiku 4.5** for the model picker
  (set in `report.py` JS). Don't change without asking — user explicitly
  picked this for cost.

### Bedrock + tool use + thinking incompatibility
The summarizer + metrics-explainer use **forced tool use** (`tool_choice`
of `tool` by name) for structured output, because the pinned `anthropic`
SDK (0.69.0) doesn't yet expose `output_config.format`. The Bedrock API
**rejects `thinking` when `tool_choice` forces tool use** with a 400. So
adaptive thinking is intentionally off in summarizer code paths. Don't
re-add it without first confirming the SDK version supports
`output_config.format`.

### Reddit access — use Apewisdom, not direct
Reddit's public JSON returns 403 from many networks (verified blocked from
the user's). Reddit API access form was submitted but never approved. Don't
suggest swapping back to direct Reddit unless the user explicitly says
"Reddit API got approved." Apewisdom (`apewisdom_client.py`) scrapes the
same investing subreddits and exposes aggregate mention counts + 24h
rank/mention velocity — it's the structured stand-in.

### Finnhub fundamentals: COALESCE preserves on rate-limit
`fetch_market_data` runs Yahoo + Finnhub calls in parallel. Finnhub free
tier is 60 calls/min, so on bulk runs some tickers get rate-limited and
the in-memory `MarketData` ends up with `forward_pe=None` even if a prior
run had real values. The DB write uses `COALESCE(excluded.X, market_data.X)`
to preserve existing values. **Critical:** after `_save()`, we re-read the
row from the DB so the *returned* `MarketData` reflects the merge, not the
rate-limited snapshot. Removing the read-back regresses favorites' fundamentals.

### Favorites get guaranteed-everything pass
Favorites (sent from browser localStorage via `/refresh` body) end up in
`STOCK_BUZZ_FAVORITES` env for the orchestrator. Three guarantees:
1. They're injected as zero-buzz `TickerBuzz` if not already in `compute_buzz()`
2. `ensure_fundamentals()` runs a sequential 1s-gap retry pass after the
   bulk parallel fetch — guarantees Finnhub data
3. `summarize_top_tickers(extra_tickers=favorites)` pulls them in even if
   they're outside top-N
4. `metrics_explainer.explain_metrics()` runs in a small parallel pass for
   each favorite at the end of the pipeline

If any of those break, favorites silently lose data the user expects.

### Buzz-score fingerprint for cache invalidation
`summarizer._fingerprint(buzz, model)` deliberately buckets noisy signals
(post count log-scaled, channel count bucketed by 3, sentiment by ratio
not raw count, trending presence not exact rank) and includes model tier
in the hash. Designed so small score reshuffles between adjacent runs
DON'T invalidate cache, but real chatter shifts (sentiment flip, ticker
joining/leaving trending) DO. Same pattern in `metrics_explainer._fingerprint`
(rounds floats to 1 decimal).

### Sample-posts diversity (round-robin)
`scoring.py` builds the 5-post sample for each ticker with **round-robin
across sources** (StockTwits → Apewisdom → HN). Without this, posts with
sentiment tags (StockTwits-only) sort first by score, drowning out the
other sources entirely.

### Server restart strategy
`/restart` cannot use `os.execv()` directly because Flask's socket on port
8765 doesn't release fast enough — bind fails. Instead, spawn a detached
`subprocess.Popen` with `sleep 1.5 && exec ...`, then SIGTERM ourselves.
The new process re-binds cleanly after we're gone.

## User UX preferences (confirmed across sessions)

- **Sectors grid layout** > stacked full-width. Too much scrolling otherwise.
- **Click-to-expand modal** for ticker detail > inline expansion.
- **Refresh must NOT block** the UI. Show progress banner, auto-reload on done.
- **Model picker dropdown** is per-refresh override; persists in localStorage;
  defaults to Haiku 4.5 for first-time users.
- **AI Chips/Memory/Software/Datacenter + Space + Quantum** sectors are
  pinned at top — render even when empty (placeholder text in those cards).
- **Star ☆ click on a ticker row should NOT open the modal**. Favoriting
  and viewing detail are separate concerns. `e.target.closest()` check in
  the row handler enforces this.
- **Stock Metrics block is collapsible** (`<details>`); auto-opens when an
  AI explanation is cached, stays collapsed when not.
- **Favorites get auto-generated AI summary AND auto-generated metrics
  explainer** on every refresh (using the favorite-tier model).
- **Server controls**: stop and restart from UI, but no in-UI start
  (impossible — page can't reach a stopped server). Server-stopped overlay
  tells user to run `stock-buzz-server` in a terminal.

## How to collaborate on this project

- **State concrete dollar costs** when proposing changes that touch the LLM.
  Per-call costs vary 20x across Opus/Sonnet/Haiku — surface that.
- **Don't auto-downgrade model** when user mentions cost. Offer options.
- **Verify before recommending** — running pipeline ≠ working pipeline. Pull
  a sample from the rendered HTML or DB to confirm changes actually
  reached the user-visible surface. Several past bugs (sample-posts
  diversity, COALESCE-but-return-stale, server-side env not picked up
  after `.env` edit) were "code looked right but state was wrong."
- **Pipelines run async via the Flask `/refresh` endpoint.** When testing
  changes, prefer triggering through `/refresh` rather than running
  `python -m src.run_scrape` directly — the server's `os.environ` may
  diverge from your shell.
- **The orchestrator is the long pole** in the pipeline (~90s with cache
  hits, ~3min cold). Optimization wins compound here — most other phases
  are <30s.

## Configuration

All knobs in `.env` (loaded by `python-dotenv` in `src/config.py`):

| Variable | Required? | Notes |
|---|---|---|
| `STOCK_BUZZ_PROVIDER` | no | `anthropic`/`bedrock`/`none`. Auto-detects if unset. |
| `STOCK_BUZZ_MODEL` | no | Favorite-tier model. Default `claude-sonnet-4-6`. |
| `ANTHROPIC_API_KEY` | if `anthropic` | Direct API key. |
| `AWS_PROFILE` / `AWS_REGION` | if `bedrock` | User's profile must have Bedrock model access. |
| `FINNHUB_API_KEY` | optional but recommended | Free at finnhub.io. Unlocks fundamentals + earnings. |
| `STOCK_BUZZ_FAVORITES` | set by server | Comma-separated tickers; passed via env to pipeline. |

## Current status (last session: 2026-05-11)

Shipped to public GitHub repo. All Phase 1-2 features done, plus a long
list of UX polish (favorites, earnings sidebar, metrics explainer,
on-demand summarize, server controls, perf optimizations).

## Pending roadmap (offered, not done)

- **Daily history view** — was in original spec ("daily overview retained
  2 weeks"). Compare runs across days: who entered/left top 30, sentiment
  swings, biggest movers. SQLite already stores per-run snapshots, just
  unsurfaced.
- **Alerts on favorites** — earnings <3 days, big drops, buzz spikes.
  Requires the dashboard running continuously (loops back to deployment).
- **Bot/spam filtering** — current scoring weighs every mention equally.
  Account age + post velocity filters on StockTwits would help.
- **Phase 3 deployment** — Postgres replacement for SQLite, JWT auth,
  Docker, AWS deploy. Was the next logical step but user paused to
  iterate on features instead.
- **Short interest + expense ratio** — Finnhub free tier doesn't include.
  Would need paid plan or a different provider.

## Quick start (when returning to this project)

```bash
cd ~/path/to/stock-buzz
source venv/bin/activate
# OR if no venv yet:
./setup.sh

# Daily-driver server (Bedrock + Sonnet 4.6):
python -m src.server
# Then open http://127.0.0.1:8765
```

If the user has shell aliases set up (`stock-buzz-server` etc.), those
override env vars. They're typically defined in `~/.zshrc` and not part
of the repo.
