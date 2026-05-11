# Stock Buzz

A personal dashboard that surfaces which stocks people are actively
discussing on social media, with live prices, fundamentals, earnings, and
Claude-generated plain-English explanations of what the numbers mean.

Pulls aggregated buzz from **StockTwits** (real-time finance chatter with
bullish/bearish sentiment), **Apewisdom** (Reddit aggregate across ~10
investing subreddits with mention velocity), and **Hacker News** (tech-leaning
stories that mention the ticker). Cross-references those tickers against
**Yahoo Finance** for prices and **Finnhub** for fundamentals + earnings +
trusted-source news.

The dashboard is a single-page web app served by a tiny Flask process running
on `localhost:8765`.

## Features

- 3-column responsive sector grid (AI Chips, Space, Quantum, Biotech, etc.)
  with always-pinned themes
- Click any ticker → modal with price, fundamentals, ELI15 + standard AI
  summary, bull/bear cases, top posts, earnings, trusted-source news
- ⭐ Favorites sidebar (browser localStorage) with guaranteed AI summaries +
  metrics explanations
- 📅 Upcoming earnings sidebar sorted by next report date
- 🔁 Background refresh button — non-blocking, live progress, page
  auto-reloads when done
- Switchable AI provider (Anthropic API or AWS Bedrock) and model picker
  (Opus / Sonnet / Haiku / no-AI)
- On-demand "Generate AI Summary" + "Explain these metrics like I'm 15"
  buttons in every modal
- Server stop / restart from the UI

## Prerequisites

- **Python 3.10+** (3.13 tested)
- One of the following for AI summaries (you can run without AI too):
  - **Anthropic API key** from https://console.anthropic.com (~$5 minimum credit)
  - **AWS Bedrock** access with Claude models enabled in the Bedrock console
- Optional but recommended: **Finnhub API key** (free) for fundamentals,
  market cap, P/E, earnings dates, and trusted-source news. Get one at
  https://finnhub.io/register.

## Setup (new machine)

```bash
git clone https://github.com/<your-username>/stock-buzz.git
cd stock-buzz
./setup.sh
```

`setup.sh` creates a Python venv, installs dependencies, and copies
`.env.example` to `.env`. Then edit `.env` to fill in your credentials:

```bash
# Required for AI summaries (pick ONE provider)
ANTHROPIC_API_KEY=sk-ant-api03-...        # if using direct Anthropic API
# OR
STOCK_BUZZ_PROVIDER=bedrock                # if using AWS Bedrock
AWS_PROFILE=your-aws-profile               # AWS profile must have Bedrock model access
AWS_REGION=us-east-1

# Optional but recommended
FINNHUB_API_KEY=your_finnhub_key_here

# Optional model override (defaults to claude-sonnet-4-6)
STOCK_BUZZ_MODEL=claude-sonnet-4-6
```

`.env` is gitignored — you'll never accidentally commit your keys.

## Running

```bash
source venv/bin/activate
python -m src.server
```

This starts the dashboard at http://localhost:8765/ and opens it in your
browser. Click **Refresh** to scrape and summarize. First run takes ~2-3
minutes for the full pipeline; subsequent refreshes hit the cache aggressively
and finish in ~30-60 seconds.

### Convenient shell aliases (optional)

If you'll use this regularly, add aliases to your `~/.zshrc` or `~/.bashrc`:

```bash
# Daily driver — Bedrock + Sonnet 4.6
alias stock-buzz-server='cd ~/path/to/stock-buzz && source venv/bin/activate && \
    STOCK_BUZZ_PROVIDER=bedrock AWS_PROFILE=YOUR_PROFILE AWS_REGION=us-east-1 \
    STOCK_BUZZ_MODEL=claude-sonnet-4-6 python -m src.server'

# Cheaper bulk via Haiku
alias stock-buzz-server-haiku='cd ~/path/to/stock-buzz && source venv/bin/activate && \
    STOCK_BUZZ_PROVIDER=bedrock AWS_PROFILE=YOUR_PROFILE AWS_REGION=us-east-1 \
    STOCK_BUZZ_MODEL=claude-haiku-4-5 python -m src.server'
```

Then `stock-buzz-server` from any terminal starts the dashboard.

## Cost

With Bedrock + Sonnet 4.6 (default):
- Per refresh: ~$0.005–$0.02 (most calls hit the cache after the first run)
- On-demand "Generate Summary" button: ~$0.005 (Haiku) / ~$0.02 (Sonnet) /
  ~$0.10 (Opus)
- Bedrock and Anthropic API charge identical per-token rates for Claude

If cost matters more than max quality, set `STOCK_BUZZ_MODEL=claude-haiku-4-5`
in `.env` for ~5x savings.

## Configuration reference

All knobs live in `.env` (loaded automatically) or as shell exports.

| Variable | Default | What it does |
|---|---|---|
| `STOCK_BUZZ_PROVIDER` | auto-detect | `anthropic`, `bedrock`, or `none` (skip AI) |
| `STOCK_BUZZ_MODEL` | `claude-sonnet-4-6` | Claude model alias for favorites + on-demand calls |
| `ANTHROPIC_API_KEY` | unset | Required for direct Anthropic API |
| `AWS_PROFILE` / `AWS_REGION` | unset | Required for Bedrock |
| `FINNHUB_API_KEY` | unset | Unlocks fundamentals + earnings + news |

## Architecture

```
┌────────────────────────────────────────────────────────────────┐
│ Sources         StockTwits   Apewisdom   Hacker News          │
│                    │            │            │                 │
│                    └─────┬──────┴────────────┘                 │
│                          ▼                                     │
│ Scoring          Buzz score (recency × engagement × breadth)   │
│                          │                                     │
│                          ▼                                     │
│ Enrichment    ┌── market_data ──┬── earnings ── news ──┐      │
│               │  (Yahoo + Finnhub)│  (Finnhub)         │      │
│               └────────┬──────────┴────────────────────┘      │
│                        ▼                                       │
│ AI            Claude summaries (top-N + favorites, parallel)   │
│ Layer         Metrics explainer (favorites)                    │
│                        │                                       │
│                        ▼                                       │
│ Storage       SQLite (data/buzz.db) — scraped posts, mentions, │
│               run snapshots, summaries, metrics-explanations   │
│                        │                                       │
│                        ▼                                       │
│ Frontend      Jinja2-rendered HTML served by Flask             │
│               • sector grid + modal + sidebars                 │
│               • on-demand summarize / explain endpoints        │
└────────────────────────────────────────────────────────────────┘
```

Source files in `src/`:
- `run_scrape.py` — pipeline orchestrator
- `stocktwits_client.py`, `apewisdom_client.py`, `hackernews_client.py` — data sources
- `ticker_extractor.py`, `known_tickers.py` — extract ticker mentions from text
- `scoring.py` — buzz score
- `market_data.py` — Yahoo + Finnhub fundamentals
- `earnings.py` — earnings + trusted news
- `summarizer.py` — Claude summaries (provider-agnostic)
- `metrics_explainer.py` — plain-English metrics explanation
- `sectors.py` — ticker-to-sector mapping
- `report.py` — Jinja2 dashboard rendering
- `server.py` — Flask server (serves report, exposes refresh/summarize/explain endpoints)
- `storage.py` — SQLite schema + helpers
- `config.py` — paths and constants

## Troubleshooting

**"AWS Bedrock client could not be created"** — your `AWS_PROFILE` likely
isn't authenticating. Run `aws sts get-caller-identity --profile YOUR_PROFILE`
and confirm it returns your identity. Also check Bedrock model access in the
AWS console.

**Refresh button hangs at "Searching Hacker News"** — HN's Algolia API
occasionally times out. Pipeline will eventually fall through; HN is
best-effort.

**Modals show no fundamentals** — check `FINNHUB_API_KEY` is set in `.env`,
then click Refresh. Free tier is 60 req/min; the pipeline has rate-limit
handling but the bulk fetch can lose a few tickers on busy days. Favorites
get a guaranteed retry pass and should always populate.

**Port 8765 already in use** — kill any old server with
`lsof -ti:8765 | xargs kill`, or pass `--port 8766`.

**Reddit data not showing up directly** — Reddit's official API access is
gated. We use Apewisdom (which scrapes 10 investing subreddits and exposes
aggregated mentions) as a structured stand-in.

## Privacy & data

- All data is stored locally in SQLite (`data/buzz.db`) — never sent anywhere
- API keys live in `.env` only; never logged or transmitted except to the
  intended provider (Anthropic/Bedrock/Finnhub)
- The dashboard is bound to `127.0.0.1` only — not accessible from other
  machines on your network unless you change the host

## Not financial advice

This is a personal research tool. The AI summaries are generated by an LLM
and may contain inaccuracies, missing context, or simply be wrong. Don't
make trading decisions based solely on this dashboard.

## License

MIT — see `LICENSE`.
