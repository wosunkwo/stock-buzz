"""Generate plain-English explanations of stock fundamental metrics.

Audience: a 15-year-old. The Claude prompt is tuned to:
- Define each metric in plain words BEFORE giving the value
- Tell the user whether THIS company's number looks good/bad/normal
  for its sector/peers, with concrete reference points
- Avoid jargon like "TTM", "yoy", "forward" without explanation
- Be honest when a metric is genuinely meaningless for the ticker
  (e.g. P/E for a company that's not profitable)

Cached for 6h per (ticker, fingerprint-of-metric-set) — metrics don't move
minute-to-minute, so re-explaining the same numbers wastes tokens.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import DB_PATH
from .market_data import MarketData
from .storage import get_conn

EXPLAINER_TTL_SECONDS = 6 * 60 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics_explanations (
    ticker TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    overview TEXT,
    per_metric_json TEXT,
    model TEXT,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (ticker, fingerprint)
);
"""


@dataclass
class MetricsExplanation:
    ticker: str
    overview: str                   # plain-English summary paragraph
    per_metric: dict[str, str]      # {metric_key: plain English explanation}
    model: str
    fetched_at: float


SYSTEM_PROMPT = """You are explaining stock fundamental metrics to a curious \
15-year-old who has never invested before. They're smart but don't know jargon.

For each ticker the user gives you, produce TWO things:

1. **overview** — A 3-5 sentence plain-English narrative paragraph that:
   - Says what the company actually does (in one short clause)
   - Walks through what these numbers tell us about the company's health
   - Calls out anything that looks unusually good or bad
   - Mentions if any metric is missing or doesn't apply (e.g. ETFs don't have P/E)
   - NEVER uses jargon like "TTM", "YoY", "EV/EBITDA" without immediately explaining

2. **per_metric** — One short sentence per metric. The sentence should:
   - Define the metric in plain words ("Forward P/E means the stock costs 22 \
times what experts expect the company to earn next year per share")
   - State what THIS company's number means in context ("That's roughly average — \
the S&P 500 trades around 20x")
   - Be ONE sentence (max ~30 words)

Reference points to use when relevant:
- S&P 500 average trailing P/E ≈ 25
- Healthy net margin: 10%+ is good, 20%+ is excellent, software is often 30%+
- Healthy gross margin: above 40% is solid, software is often 70%+
- ROE: above 15% is solid, above 25% is excellent
- Debt-to-equity: under 1.0 is conservative, 1-2 is normal, above 2 is leveraged
- Beta: 1.0 = moves with market, >1.5 = much more volatile, <0.5 = much less
- PEG below 1.0 typically signals "growth at a reasonable price"
- 52-week return: compare to the S&P 500 (which is up roughly 10% per year on average)

Hard rules:
- Do NOT make up numbers. Only reference numbers the user provides.
- Do NOT give buy/sell advice. Just explain the numbers.
- For metrics with null/missing values, omit them from per_metric entirely \
(don't write "this is missing").
- Keep the overview accessible — read it back to yourself imagining you're 15 \
and don't yet know what "earnings" means."""


# Map of internal metric key → human-readable label that appears in the prompt
# AND in the UI. Keep this small and curated.
METRIC_LABELS: dict[str, str] = {
    "market_cap": "Market Cap",
    "pe_ratio": "Trailing P/E",
    "forward_pe": "Forward P/E",
    "peg_ratio": "PEG Ratio",
    "pb_ratio": "P/B Ratio",
    "ev_ebitda": "EV/EBITDA",
    "eps": "EPS (TTM)",
    "dividend_yield": "Dividend Yield",
    "beta": "Beta",
    "gross_margin": "Gross Margin",
    "operating_margin": "Operating Margin",
    "net_margin": "Net Margin",
    "roe": "Return on Equity (ROE)",
    "roa": "Return on Assets (ROA)",
    "debt_to_equity": "Debt-to-Equity",
    "revenue_growth_yoy": "Revenue Growth (YoY)",
    "eps_growth_yoy": "EPS Growth (YoY)",
    "return_13w": "13-Week Return",
    "return_52w": "52-Week Return",
}


def _format_metric_value(key: str, value: float) -> str:
    """Format the numeric value the way it shows in the UI, so the LLM and the
    UI agree on what "this" number actually is."""
    if key == "market_cap":
        # in millions USD — convert to $X.XB if >= 1000
        return f"${value / 1000:.2f}B" if value >= 1000 else f"${value:.0f}M"
    if key == "dividend_yield":
        return f"{value * 100:.2f}%"
    if key in ("gross_margin", "operating_margin", "net_margin",
              "roe", "roa", "revenue_growth_yoy", "eps_growth_yoy",
              "return_13w", "return_52w"):
        return f"{value:.2f}%"
    if key in ("eps",):
        return f"${value:.2f}"
    return f"{value:.2f}"


def _collect_metric_values(md: MarketData) -> dict[str, tuple[str, str]]:
    """Returns {key: (label, formatted_value)} for every populated metric."""
    out: dict[str, tuple[str, str]] = {}
    for key, label in METRIC_LABELS.items():
        v = getattr(md, key, None)
        if v is None:
            continue
        out[key] = (label, _format_metric_value(key, v))
    return out


def _fingerprint(md: MarketData) -> str:
    """Hash of the metric set so we re-explain only when the underlying
    metrics actually change (not on every refresh)."""
    pairs = sorted(
        (k, getattr(md, k, None))
        for k in METRIC_LABELS.keys()
    )
    h = hashlib.sha256()
    h.update(md.ticker.encode())
    for k, v in pairs:
        # Round percents/ratios to 1 decimal so trivial drift doesn't invalidate
        if v is None:
            h.update(f"{k}=None;".encode())
        elif isinstance(v, float):
            h.update(f"{k}={round(v, 1)};".encode())
        else:
            h.update(f"{k}={v};".encode())
    return h.hexdigest()[:16]


def _make_user_message(md: MarketData) -> str:
    metrics = _collect_metric_values(md)
    lines = [f"Ticker: ${md.ticker}"]
    if md.long_name:
        lines.append(f"Company: {md.long_name}")
    if md.industry:
        lines.append(f"Industry: {md.industry}")
    if md.exchange:
        lines.append(f"Exchange: {md.exchange}")
    lines.append("")
    lines.append("Metrics to explain:")
    for key, (label, value) in metrics.items():
        lines.append(f"  {label} ({key}): {value}")
    lines.append("")
    lines.append(
        "Now produce the JSON output: an `overview` paragraph and a `per_metric` "
        "object whose keys EXACTLY match the metric keys above (the snake_case "
        "ones in parentheses). Skip any metric you can't meaningfully explain."
    )
    return "\n".join(lines)


SUMMARY_TOOL = {
    "name": "submit_metrics_explanation",
    "description": "Submit the plain-English explanation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "overview": {
                "type": "string",
                "description": "3-5 sentence narrative paragraph explaining what these numbers say about the company.",
            },
            "per_metric": {
                "type": "object",
                "description": "Map of metric_key → one-sentence plain-English explanation. Keys must match the snake_case metric keys provided.",
                "additionalProperties": {"type": "string"},
            },
        },
        "required": ["overview", "per_metric"],
    },
}


def init_explanations_table(db_path: Path = DB_PATH) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)


def _get_cached(ticker: str, fingerprint: str,
                db_path: Path = DB_PATH) -> Optional[MetricsExplanation]:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM metrics_explanations WHERE ticker=? AND fingerprint=?",
            (ticker, fingerprint),
        ).fetchone()
    if not row:
        return None
    if (time.time() - (row["fetched_at"] or 0)) > EXPLAINER_TTL_SECONDS:
        return None
    try:
        per_metric = json.loads(row["per_metric_json"]) if row["per_metric_json"] else {}
    except (ValueError, TypeError):
        per_metric = {}
    return MetricsExplanation(
        ticker=row["ticker"],
        overview=row["overview"] or "",
        per_metric=per_metric,
        model=row["model"] or "",
        fetched_at=row["fetched_at"],
    )


def _save(exp: MetricsExplanation, fingerprint: str, db_path: Path = DB_PATH) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO metrics_explanations(
                ticker, fingerprint, overview, per_metric_json, model, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, fingerprint) DO UPDATE SET
                overview=excluded.overview,
                per_metric_json=excluded.per_metric_json,
                model=excluded.model,
                fetched_at=excluded.fetched_at
            """,
            (exp.ticker, fingerprint, exp.overview,
             json.dumps(exp.per_metric), exp.model, exp.fetched_at),
        )


def explain_metrics(
    md: MarketData,
    client=None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    db_path: Path = DB_PATH,
    use_cache: bool = True,
    force_refresh: bool = False,
) -> Optional[MetricsExplanation]:
    """Generate (or fetch from cache) the metrics explanation for one ticker.

    Returns None if no LLM provider is available or the call fails.
    """
    init_explanations_table(db_path)

    metrics = _collect_metric_values(md)
    if not metrics:
        # Nothing to explain.
        return None

    # Lazy imports to keep this module's deps minimal at import time.
    from .summarizer import _resolve_provider, _resolve_model, _make_client

    if provider is None:
        provider = _resolve_provider()
    if provider == "none":
        return None
    resolved_model = _resolve_model(provider, override=model)
    fingerprint = _fingerprint(md)

    if use_cache and not force_refresh:
        cached = _get_cached(md.ticker, fingerprint, db_path)
        if cached:
            return cached

    if client is None:
        client = _make_client(provider)
        if client is None:
            return None

    try:
        # Same shape as summarizer: forced-tool-use to get structured output
        # without needing a newer SDK feature.
        sys_arg = (
            SYSTEM_PROMPT if provider == "bedrock"
            else [{"type": "text", "text": SYSTEM_PROMPT,
                   "cache_control": {"type": "ephemeral"}}]
        )
        response = client.messages.create(
            model=resolved_model,
            max_tokens=2500,
            system=sys_arg,
            tools=[SUMMARY_TOOL],
            tool_choice={"type": "tool", "name": "submit_metrics_explanation"},
            messages=[{"role": "user", "content": _make_user_message(md)}],
        )
    except Exception as e:
        print(f"    {md.ticker}: explain_metrics failed: {type(e).__name__}: {str(e)[:200]}")
        return None

    tool_block = next(
        (b for b in response.content if getattr(b, "type", None) == "tool_use"
         and getattr(b, "name", None) == "submit_metrics_explanation"),
        None,
    )
    if not tool_block:
        return None
    data = tool_block.input or {}
    per_metric = data.get("per_metric") or {}
    if not isinstance(per_metric, dict):
        per_metric = {}

    exp = MetricsExplanation(
        ticker=md.ticker,
        overview=str(data.get("overview", "")),
        per_metric={str(k): str(v) for k, v in per_metric.items()},
        model=f"{provider}:{resolved_model}",
        fetched_at=time.time(),
    )
    _save(exp, fingerprint, db_path)
    return exp
