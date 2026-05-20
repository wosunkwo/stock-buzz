"""Per-ticker AI summarization.

Supports three LLM provider paths, selected by env var:
- STOCK_BUZZ_PROVIDER=anthropic  → direct Anthropic API
- STOCK_BUZZ_PROVIDER=bedrock    → AWS Bedrock (uses your AWS credentials)
- STOCK_BUZZ_PROVIDER=gemini     → Google Gemini direct API (uses GOOGLE_API_KEY)

For each top buzzy ticker, ask the LLM to produce four sections:
- ELI15: explain like the reader is 15 — what the company does and why people are buzzing
- standard: a normal financial-news-style summary
- bull_case: why bulls think this goes up
- bear_case: why bears think this goes down

Design choices:
- Default model: claude-opus-4-7 (best quality). Override via STOCK_BUZZ_MODEL.
- Structured output: forced tool use with a single tool. Works on the pinned
  anthropic SDK version (0.69.0) which doesn't yet expose output_config.format.
- Prompt caching: system prompt is cached so repeated runs hit the cache.
- SQLite cache: if we summarized a ticker in the last 30 minutes with the same
  buzz fingerprint, return the cached summary.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import DB_PATH
from .scoring import TickerBuzz
from .market_data import MarketData
from .sectors import get_sector
from .storage import get_conn

SUMMARY_TTL_SECONDS = 90 * 60  # 90 min: re-summarize only if buzz signature changed OR 1.5h elapsed

SCHEMA = """
CREATE TABLE IF NOT EXISTS summaries (
    ticker TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    eli15 TEXT,
    standard TEXT,
    bull_case TEXT,
    bear_case TEXT,
    model TEXT,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (ticker, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_summaries_ticker ON summaries(ticker);
"""


@dataclass
class Summary:
    ticker: str
    eli15: str
    standard: str
    bull_case: str
    bear_case: str
    model: str
    fetched_at: float


SYSTEM_PROMPT = """You are a financial summarization assistant for a personal stock dashboard.

For each ticker the user gives you, produce four short pieces:

1. eli15 — Explain to a 15-year-old: what does this company do, in plain words, and \
why are people on social media talking about it RIGHT NOW based on the messages provided. \
2-4 sentences. No jargon.

2. standard — A normal financial-news-style summary of the company and the current \
buzz. 3-5 sentences. Concrete and specific. Cite recent moves (price action, earnings, \
news) only if directly visible in the inputs — do NOT invent dates, numbers, or events.

3. bull_case — The strongest reasons bulls might think this goes UP from here. 3-5 \
bullet points. Be specific to the company; avoid generic "the company has good fundamentals" \
filler.

4. bear_case — The strongest reasons bears might think this goes DOWN from here. 3-5 \
bullet points. Same standard.

Important rules:
- Do NOT make up facts. If you don't know something, leave it out.
- The bull and bear cases should be the strongest version of EACH side — steelman both.
- Avoid hedging words ("might", "possibly", "could potentially") in the cases — make the \
arguments cleanly.
- This is a personal dashboard, NOT financial advice. The user understands this.
- Keep total output tight: under ~250 words per section."""


# We force structured output by giving Claude a single tool with this schema and
# requiring it to call that tool. Works on all SDK versions; output_config.format
# is newer and not yet in the pinned SDK release.
SUMMARY_TOOL = {
    "name": "submit_summary",
    "description": "Submit the four required summary sections for the ticker.",
    "input_schema": {
        "type": "object",
        "properties": {
            "eli15": {"type": "string", "description": "Explain like the reader is 15 — 2-4 sentences."},
            "standard": {"type": "string", "description": "Standard financial summary — 3-5 sentences."},
            "bull_case": {"type": "string", "description": "Strongest bull case — 3-5 bullet points joined with newlines, each starting with '- '."},
            "bear_case": {"type": "string", "description": "Strongest bear case — 3-5 bullet points joined with newlines, each starting with '- '."},
        },
        "required": ["eli15", "standard", "bull_case", "bear_case"],
    },
}


def init_summaries_table(db_path: Path = DB_PATH) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)


def _fingerprint(buzz: TickerBuzz, model: str = "") -> str:
    """Stable hash of buzz signal — changes when the underlying chatter
    shifts MEANINGFULLY. We deliberately avoid bucketing the score because
    small score reshuffles between adjacent runs would invalidate the cache
    even when the chatter itself is the same.

    We bucket the variable signals coarsely so the fingerprint flips only on
    real changes:
      - posts: bucketed by 25%-of-current step (5 → 6 doesn't invalidate;
        20 → 35 does)
      - sentiment: only if total bull+bear count crosses a threshold
      - channels: bucketed (1 chan vs 5 vs 15 are different signals; 5 → 6
        is the same)
      - trending_rank: only the boolean "in trending top-30 or not" matters
        for the summary content, since the modal text is the same regardless
        of exact rank.
    """
    h = hashlib.sha256()

    # Coarse post count: log scale, ~10 buckets across 1-1000 posts.
    posts_bucket = 0 if buzz.distinct_posts == 0 else int(round((buzz.distinct_posts) ** 0.5))
    # Coarse channel count.
    chan_bucket = 0 if buzz.distinct_channels == 0 else min(buzz.distinct_channels // 3, 5)
    # Sentiment: bucket as (lots-of-bulls, lots-of-bears, mixed, no-sentiment).
    bulls, bears = buzz.bullish_count, buzz.bearish_count
    if bulls + bears == 0:
        sent = "none"
    elif bulls > bears * 2:
        sent = "bull"
    elif bears > bulls * 2:
        sent = "bear"
    else:
        sent = "mixed"
    in_trending = buzz.trending_rank is not None and buzz.trending_rank <= 30

    # Reduce model ID to a coarse tier so switching between e.g.
    # "claude-haiku-4-5" and the dated/aliased version doesn't invalidate.
    model_tier = ""
    if model:
        m = model.lower()
        if "opus" in m: model_tier = "opus"
        elif "sonnet" in m: model_tier = "sonnet"
        elif "haiku" in m: model_tier = "haiku"
        elif "gemini" in m: model_tier = m  # full name: 2.0-flash ≠ 2.5-flash ≠ 2.5-pro
        else: model_tier = "other"

    h.update(
        f"{buzz.ticker}|p{posts_bucket}|c{chan_bucket}|s{sent}|t{int(in_trending)}|m{model_tier}".encode()
    )
    return h.hexdigest()[:16]


def _get_cached(ticker: str, fingerprint: str, db_path: Path = DB_PATH) -> Optional[Summary]:
    with get_conn(db_path) as conn:
        row = conn.execute(
            """SELECT * FROM summaries WHERE ticker=? AND fingerprint=?""",
            (ticker, fingerprint),
        ).fetchone()
    if not row:
        return None
    if (time.time() - row["fetched_at"]) > SUMMARY_TTL_SECONDS:
        return None
    return Summary(
        ticker=row["ticker"],
        eli15=row["eli15"] or "",
        standard=row["standard"] or "",
        bull_case=row["bull_case"] or "",
        bear_case=row["bear_case"] or "",
        model=row["model"] or "",
        fetched_at=row["fetched_at"],
    )


def _save(summary: Summary, fingerprint: str, db_path: Path = DB_PATH) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO summaries(ticker, fingerprint, eli15, standard, bull_case,
                                  bear_case, model, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, fingerprint) DO UPDATE SET
                eli15=excluded.eli15, standard=excluded.standard,
                bull_case=excluded.bull_case, bear_case=excluded.bear_case,
                model=excluded.model, fetched_at=excluded.fetched_at
            """,
            (summary.ticker, fingerprint, summary.eli15, summary.standard,
             summary.bull_case, summary.bear_case, summary.model, summary.fetched_at),
        )


# --- Provider configuration ----------------------------------------------------

# Default direct-API model. Override per-call or via STOCK_BUZZ_MODEL env var.
DEFAULT_DIRECT_MODEL = "claude-opus-4-7"

# Gemini defaults — used when provider=gemini and no STOCK_BUZZ_MODEL override.
GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"       # favorites tier
GEMINI_DEFAULT_BULK_MODEL = "gemini-2.0-flash"  # bulk top-N tier

# Map direct-API model IDs to their Bedrock equivalents. Bedrock uses
# "cross-region inference profiles" prefixed with `us.` for the same model
# in us-east-1/us-west-2 routing. If the user passes a value not in this map,
# we use it verbatim — letting them paste a Bedrock model ID directly if they
# want.
DIRECT_TO_BEDROCK_MODEL = {
    "claude-opus-4-7": "us.anthropic.claude-opus-4-7",
    "claude-opus-4-6": "us.anthropic.claude-opus-4-6-v1",
    "claude-sonnet-4-6": "us.anthropic.claude-sonnet-4-6",
    "claude-haiku-4-5": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
}


def _resolve_provider() -> str:
    """Return 'bedrock', 'anthropic', or 'none' based on env config."""
    provider = os.environ.get("STOCK_BUZZ_PROVIDER", "").strip().lower()
    if provider in ("bedrock", "anthropic", "gemini", "none"):
        return provider
    # Default: prefer direct API if a key is present; else try Bedrock.
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "bedrock"


def _resolve_model(provider: str, override: Optional[str] = None) -> str:
    """Translate the requested model name into the right ID for the chosen provider."""
    if provider == "gemini":
        # Gemini model names pass through verbatim. Use Gemini-specific default
        # so an unset STOCK_BUZZ_MODEL doesn't return a Claude model name.
        return override or os.environ.get("STOCK_BUZZ_MODEL") or GEMINI_DEFAULT_MODEL
    base = override or os.environ.get("STOCK_BUZZ_MODEL") or DEFAULT_DIRECT_MODEL
    if provider == "bedrock":
        # If the caller already gave us a Bedrock-shaped ID (contains a dot or
        # `:` version), trust it. Otherwise look up the translation.
        if "." in base or ":" in base:
            return base
        return DIRECT_TO_BEDROCK_MODEL.get(base, base)
    return base


def _make_client(provider: str):
    """Construct the SDK client for the chosen provider. Returns None on failure."""
    if provider == "gemini":
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            print("  GOOGLE_API_KEY not set — skipping summaries.")
            return None
        try:
            from google import genai
            return genai.Client(api_key=api_key)
        except ImportError:
            print("  google-genai not installed — run: pip install google-genai")
            return None

    try:
        import anthropic
    except ImportError:
        return None

    if provider == "bedrock":
        try:
            from anthropic import AnthropicBedrock
        except ImportError:
            print("  anthropic[bedrock] extras not installed — install boto3 + retry.")
            return None
        kwargs = {}
        # Honor explicit profile/region via env. The SDK will use boto3's default
        # credential chain (~/.aws/config, env vars, instance role) if not given.
        profile = os.environ.get("AWS_PROFILE") or os.environ.get("STOCK_BUZZ_AWS_PROFILE")
        region = os.environ.get("AWS_REGION") or os.environ.get("STOCK_BUZZ_AWS_REGION", "us-east-1")
        if profile:
            kwargs["aws_profile"] = profile
        kwargs["aws_region"] = region
        return AnthropicBedrock(**kwargs)

    # Direct Anthropic API.
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    return anthropic.Anthropic()


# -------------------------------------------------------------------------------


def _build_user_message(buzz: TickerBuzz, market: Optional[MarketData]) -> str:
    """Construct the variable per-ticker user message."""
    lines: list[str] = []
    lines.append(f"Ticker: ${buzz.ticker}")
    lines.append(f"Sector: {get_sector(buzz.ticker)}")
    if market and market.long_name:
        lines.append(f"Company: {market.long_name}")
    if market and market.exchange:
        lines.append(f"Exchange: {market.exchange}")

    lines.append("")
    lines.append("Market snapshot:")
    if market:
        if market.price is not None:
            pct = market.percent_change
            pct_str = f" ({pct:+.2f}% today)" if pct is not None else ""
            lines.append(f"  Price: ${market.price:.2f}{pct_str}")
        if market.day_low and market.day_high:
            lines.append(f"  Day range: ${market.day_low:.2f} - ${market.day_high:.2f}")
        if market.week52_low and market.week52_high:
            lines.append(f"  52-week range: ${market.week52_low:.2f} - ${market.week52_high:.2f}")
        if market.volume:
            lines.append(f"  Volume today: {market.volume:,}")
    else:
        lines.append("  (live market data unavailable)")

    lines.append("")
    lines.append("Buzz signal (last ~24 hours of social posts):")
    lines.append(f"  {buzz.distinct_posts} distinct posts across {buzz.distinct_channels} channels")
    if buzz.bullish_count or buzz.bearish_count:
        lines.append(f"  Sentiment-tagged messages: {buzz.bullish_count} bullish, {buzz.bearish_count} bearish")
    if buzz.trending_rank:
        lines.append(f"  Currently #{buzz.trending_rank} on platform trending list")
    lines.append(f"  Total platform engagement: {buzz.total_upvotes:,} upvotes, "
                 f"{buzz.total_comments:,} comments, {buzz.total_followers:,} follower-reach")

    if buzz.sample_posts:
        lines.append("")
        lines.append("Sample of recent posts (text only — these are what people are actually saying):")
        for p in buzz.sample_posts[:8]:
            sentiment_str = f" [{p['sentiment']}]" if p.get('sentiment') else ""
            channel_str = f" r/{p['channel']}" if p.get('channel') and p.get('source') == 'reddit' else (
                f" ${p['channel']}" if p.get('channel') else ""
            )
            text = (p.get('text') or '').replace('\n', ' ')[:280]
            lines.append(f"  -{channel_str}{sentiment_str}: {text}")

    lines.append("")
    lines.append("Now produce the JSON output with the four required fields.")
    return "\n".join(lines)


def _call_gemini_summary(client, model_name: str, buzz: TickerBuzz,
                         market: Optional[MarketData]) -> Optional[dict]:
    """Call Gemini API for a summary. Retries on 429 rate-limit with backoff."""
    import json
    from google.genai import types
    user_msg = _build_user_message(buzz, market)
    user_msg += ("\n\nReturn a JSON object with exactly these four string keys: "
                 "eli15, standard, bull_case, bear_case.")
    for attempt in range(4):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=user_msg,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    response_mime_type="application/json",
                ),
            )
            result = json.loads(response.text)
            time.sleep(4)  # pace to stay under 15 RPM free-tier limit
            return result
        except Exception as e:
            if "429" in str(e) and attempt < 3:
                wait = (2 ** attempt) * 10  # 10s, 20s, 40s
                print(f"    {buzz.ticker}: Gemini 429 — retrying in {wait}s (attempt {attempt + 1}/3)")
                time.sleep(wait)
                continue
            print(f"    {buzz.ticker}: Gemini summary failed: {type(e).__name__}: {str(e)[:200]}")
            return None


def summarize_ticker(
    buzz: TickerBuzz,
    market: Optional[MarketData],
    client=None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    db_path: Path = DB_PATH,
    use_cache: bool = True,
) -> Optional[Summary]:
    """Summarize one ticker. Returns None if the call fails or no creds available."""
    init_summaries_table(db_path)

    if provider is None:
        provider = _resolve_provider()
    resolved_model = _resolve_model(provider, override=model)
    # Fingerprint must include the model so a Haiku summary doesn't get
    # served when we asked for Sonnet (and vice versa).
    fingerprint = _fingerprint(buzz, resolved_model)

    if use_cache:
        cached = _get_cached(buzz.ticker, fingerprint, db_path)
        if cached:
            return cached

    if client is None:
        client = _make_client(provider)
        if client is None:
            return None

    if provider == "gemini":
        data = _call_gemini_summary(client, resolved_model, buzz, market)
        if data is None:
            return None
        summary = Summary(
            ticker=buzz.ticker,
            eli15=data.get("eli15", ""),
            standard=data.get("standard", ""),
            bull_case=data.get("bull_case", ""),
            bear_case=data.get("bear_case", ""),
            model=f"gemini:{resolved_model}",
            fetched_at=time.time(),
        )
        _save(summary, fingerprint, db_path)
        return summary

    try:
        # We don't enable adaptive thinking here because the API rejects thinking
        # when tool_choice forces tool use, and we need forced tool use to get
        # structured output on this SDK version (output_config.format isn't yet
        # available in anthropic 0.69.0). The summarization task is short enough
        # that direct generation produces high-quality results.
        # Bedrock doesn't yet support `cache_control` on messages — pass plain
        # system string in that case. Direct API gets the cached prefix.
        if provider == "bedrock":
            system_arg = SYSTEM_PROMPT
        else:
            system_arg = [
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        response = client.messages.create(
            model=resolved_model,
            max_tokens=2000,
            system=system_arg,
            tools=[SUMMARY_TOOL],
            tool_choice={"type": "tool", "name": "submit_summary"},
            messages=[{"role": "user", "content": _build_user_message(buzz, market)}],
        )
    except Exception as e:
        print(f"    {buzz.ticker}: summarize failed: {type(e).__name__}: {str(e)[:200]}")
        return None

    tool_block = next(
        (b for b in response.content if getattr(b, "type", None) == "tool_use"
         and getattr(b, "name", None) == "submit_summary"),
        None,
    )
    if not tool_block:
        return None
    data = tool_block.input or {}

    summary = Summary(
        ticker=buzz.ticker,
        eli15=data.get("eli15", ""),
        standard=data.get("standard", ""),
        bull_case=data.get("bull_case", ""),
        bear_case=data.get("bear_case", ""),
        model=f"{provider}:{resolved_model}",
        fetched_at=time.time(),
    )
    _save(summary, fingerprint, db_path)
    return summary


# Default model used for the bulk top-N (cheap + fast). Favorites get the
# user's configured/override model instead, since those are the tickers the
# user cares about most.
DEFAULT_BULK_MODEL = "claude-haiku-4-5"


def summarize_top_tickers(
    buzz_list: list[TickerBuzz],
    market_data: dict[str, MarketData],
    top_n: int = 30,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    db_path: Path = DB_PATH,
    verbose: bool = True,
    progress_cb=None,
    extra_tickers: Optional[list[str]] = None,
    bulk_model: Optional[str] = None,
    max_concurrent: int = 5,
) -> dict[str, Summary]:
    """Summarize the top-N tickers + favorites in parallel.

    Two-tier model selection:
    - **Favorites** (`extra_tickers`): use `model` (the user's configured /
      override choice — Sonnet 4.6 by default for the server alias).
    - **Bulk top-N**: use `bulk_model` (default Haiku 4.5 — ~3x faster and
      ~5x cheaper than Sonnet, with summary quality that's still very good
      for the unfocused buzz top-N).

    The user can disable the two-tier behavior by passing `bulk_model=model`,
    in which case all summaries use the same model.

    Concurrency: up to `max_concurrent` simultaneous LLM calls. Bedrock
    handles ~5-10 concurrent reasonably; Anthropic and Gemini direct APIs are fine too.
    """
    if provider is None:
        provider = _resolve_provider()

    if provider == "none":
        if verbose:
            print("  STOCK_BUZZ_PROVIDER=none — skipping summaries.")
        return {}

    init_summaries_table(db_path)

    favorite_resolved = _resolve_model(provider, override=model)
    _bulk_default = GEMINI_DEFAULT_BULK_MODEL if provider == "gemini" else DEFAULT_BULK_MODEL
    bulk_resolved = _resolve_model(provider, override=bulk_model or _bulk_default)

    client = _make_client(provider)
    if client is None:
        if verbose:
            if provider == "anthropic":
                print("  ANTHROPIC_API_KEY not set — skipping summaries.")
            elif provider == "gemini":
                print("  GOOGLE_API_KEY not set or google-generativeai not installed — skipping summaries.")
            else:
                print("  AWS Bedrock client could not be created — skipping summaries.")
        return {}

    # Build target list with model assignment per ticker.
    favorite_set: set[str] = set(extra_tickers or [])
    target_buzz: list[TickerBuzz] = list(buzz_list[:top_n])
    if extra_tickers:
        already = {b.ticker for b in target_buzz}
        full_index = {b.ticker: b for b in buzz_list}
        for t in extra_tickers:
            if t in already:
                continue
            if t in full_index:
                target_buzz.append(full_index[t])
                already.add(t)

    # Per-ticker (model, role) — model determines fingerprint & API call.
    work_items: list[tuple[TickerBuzz, str, str]] = []  # (buzz, model, label)
    for b in target_buzz:
        if b.ticker in favorite_set:
            work_items.append((b, favorite_resolved, "fav"))
        else:
            work_items.append((b, bulk_resolved, "bulk"))

    if verbose:
        n_fav = sum(1 for _, _, lbl in work_items if lbl == "fav")
        n_bulk = len(work_items) - n_fav
        print(f"  using provider={provider} bulk={bulk_resolved} ({n_bulk} tickers) "
              f"favorite={favorite_resolved} ({n_fav} tickers)")

    # First pass: cache lookup for everything (cheap, sequential).
    out: dict[str, Summary] = {}
    cache_hits = 0
    misses: list[tuple[TickerBuzz, str]] = []
    for buzz, mdl, _ in work_items:
        cached = _get_cached(buzz.ticker, _fingerprint(buzz, mdl), db_path)
        if cached:
            out[buzz.ticker] = cached
            cache_hits += 1
        else:
            misses.append((buzz, mdl))

    total = len(work_items)
    progress_lo, progress_hi = 0.55, 0.95

    # Initial progress update reflecting cache hits.
    if progress_cb:
        frac_after_cache = progress_lo + (progress_hi - progress_lo) * (cache_hits / max(total, 1))
        progress_cb(
            "summarize",
            f"Cache hits: {cache_hits}/{total}. Calling LLM for the rest…",
            frac_after_cache,
        )

    if not misses:
        if verbose:
            print(f"  summaries: {cache_hits} from cache, 0 new API calls")
        return out

    # Second pass: parallel API calls for cache misses.
    api_calls = 0
    api_failures = 0
    completed = cache_hits
    lock = threading.Lock()

    def _do_one(buzz: TickerBuzz, mdl: str) -> Optional[Summary]:
        market = market_data.get(buzz.ticker)
        return summarize_ticker(
            buzz, market, client=client,
            model=mdl, provider=provider, db_path=db_path,
            use_cache=False,  # we already checked
        )

    with ThreadPoolExecutor(max_workers=max_concurrent) as ex:
        futs = {ex.submit(_do_one, buzz, mdl): (buzz, mdl) for buzz, mdl in misses}
        for fut in as_completed(futs):
            buzz, mdl = futs[fut]
            try:
                summary = fut.result()
            except Exception as e:
                summary = None
                if verbose:
                    print(f"    ✗ ${buzz.ticker} exception: {type(e).__name__}: {str(e)[:100]}")
            with lock:
                completed += 1
                if summary:
                    out[buzz.ticker] = summary
                    api_calls += 1
                    if verbose:
                        print(f"    ✓ ${buzz.ticker} summarized ({mdl.split('.')[-1] if '.' in mdl else mdl})")
                else:
                    api_failures += 1
                    if verbose:
                        print(f"    ✗ ${buzz.ticker} summary failed")
                if progress_cb:
                    frac = progress_lo + (progress_hi - progress_lo) * (completed / max(total, 1))
                    progress_cb(
                        "summarize",
                        f"Summarized {completed}/{total} tickers ({cache_hits} cached, {api_calls} new, {api_failures} failed)",
                        frac,
                    )

    if verbose:
        print(f"  summaries: {cache_hits} from cache, {api_calls} new API calls, {api_failures} failed")
    return out
