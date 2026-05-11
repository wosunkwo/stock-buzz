"""Tiny local Flask server that serves the dashboard and exposes a refresh endpoint.

Run with:
    python -m src.server [--port 8765] [--host 127.0.0.1] [--no-browser]

Endpoints:
  GET  /                — serves the latest report HTML (auto-renders one if missing)
  POST /refresh         — kicks off a pipeline run in a background thread.
                          Single-flight: a second POST while running returns the
                          current status without starting a new run.
  GET  /status          — returns JSON with current pipeline state, suitable
                          for client-side polling.
  GET  /report.html     — alias for /

The pipeline runs in a daemon thread; the page polls /status, and once the
state hits "idle" with a `last_finished_at` later than the page's load time,
the JS reloads to show fresh data.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file

from .config import PROJECT_ROOT, REPORT_PATH


# Load .env before any other module reads env vars.
load_dotenv(PROJECT_ROOT / ".env")


app = Flask(__name__, static_folder=None)


# ---- shared run state (single-flight) ----
_state_lock = threading.Lock()
_state = {
    "running": False,
    "phase": None,           # e.g. "scrape_stocktwits", "summarize"
    "detail": None,          # human-readable line
    "fraction": 0.0,         # 0.0 - 1.0
    "started_at": 0.0,
    "last_finished_at": 0.0,
    "last_error": None,
}


def _set_state(**updates):
    with _state_lock:
        _state.update(updates)


def _progress_cb(phase: str, detail: str, fraction):
    _set_state(phase=phase, detail=detail, fraction=fraction or 0.0)


def _run_pipeline_in_background(model_override: str | None = None,
                                provider_override: str | None = None,
                                favorites: list[str] | None = None):
    # Apply env overrides scoped to this run only. We restore them after.
    from .run_scrape import main as run_main
    saved = {}
    keys_to_save = ("STOCK_BUZZ_MODEL", "STOCK_BUZZ_PROVIDER", "STOCK_BUZZ_FAVORITES")
    for k in keys_to_save:
        saved[k] = os.environ.get(k)
    try:
        if model_override:
            os.environ["STOCK_BUZZ_MODEL"] = model_override
        if provider_override:
            os.environ["STOCK_BUZZ_PROVIDER"] = provider_override
        if favorites:
            # Comma-separated, uppercased. Validation already done in /refresh.
            os.environ["STOCK_BUZZ_FAVORITES"] = ",".join(favorites)

        _set_state(running=True, phase="starting", detail="Starting pipeline…",
                   fraction=0.01, started_at=time.time(), last_error=None)
        run_main(progress_cb=_progress_cb)
        _set_state(running=False, last_finished_at=time.time(),
                   phase="done", detail="Refresh complete.", fraction=1.0)
    except Exception as e:  # surface to UI; don't crash the server
        _set_state(running=False, last_finished_at=time.time(),
                   phase="error", detail=f"Pipeline failed: {e}",
                   last_error=str(e), fraction=0.0)
    finally:
        # Restore previous env so subsequent default-provider /config calls
        # reflect the server's startup config, not the last override.
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@app.get("/")
@app.get("/report.html")
def serve_report():
    if not Path(REPORT_PATH).exists():
        # No report yet — kick off a first run synchronously enough that the
        # user gets a useful page on first hit. We do it in a thread and wait
        # briefly; if it takes long the user sees a placeholder.
        with _state_lock:
            already_running = _state["running"]
        if not already_running:
            threading.Thread(target=_run_pipeline_in_background, daemon=True).start()
        return (
            "<!DOCTYPE html><html><head><title>Stock Buzz — first run</title>"
            "<style>body{font-family:sans-serif;background:#0f1419;color:#e6e9ef;"
            "padding:40px;text-align:center;}a{color:#4ade80}</style></head>"
            "<body><h1>Generating dashboard for the first time…</h1>"
            "<p>This takes ~3 minutes. The page will reload automatically.</p>"
            "<script>setTimeout(()=>location.reload(),5000)</script>"
            "</body></html>",
            200,
            {"Content-Type": "text/html; charset=utf-8"},
        )
    # Disable cache so refresh-after-pipeline always shows the new file.
    return send_file(REPORT_PATH, mimetype="text/html",
                     last_modified=Path(REPORT_PATH).stat().st_mtime,
                     max_age=0)


_ALLOWED_MODELS = {
    "claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5",
    "claude-opus-4-6", "auto",
}
_ALLOWED_PROVIDERS = {"anthropic", "bedrock", "none", "auto"}


@app.post("/refresh")
def refresh():
    with _state_lock:
        if _state["running"]:
            return jsonify({"started": False, **_snapshot_state()}), 200

    body = request.get_json(silent=True) or {}
    model = (body.get("model") or "").strip().lower()
    provider = (body.get("provider") or "").strip().lower()
    raw_favs = body.get("favorites") or []
    if not isinstance(raw_favs, list):
        return jsonify({"error": "favorites must be an array of ticker strings"}), 400
    # Sanitize: ticker symbols are alphanumeric (plus a single dot for share
    # classes like BRK.B). Cap the list at 50 to prevent abuse.
    import re
    fav_re = re.compile(r"^[A-Z0-9]{1,5}(\.[A-Z])?$")
    favorites = []
    for f in raw_favs[:50]:
        if isinstance(f, str) and fav_re.match(f.strip().upper()):
            favorites.append(f.strip().upper())

    # "auto" or empty means: don't override; use the server's startup defaults.
    if model in ("auto", ""):
        model_override = None
    elif model in _ALLOWED_MODELS:
        model_override = model
    else:
        return jsonify({"error": f"unknown model: {model}",
                        "allowed": sorted(_ALLOWED_MODELS)}), 400

    if provider in ("auto", ""):
        provider_override = None
    elif provider in _ALLOWED_PROVIDERS:
        provider_override = provider
    else:
        return jsonify({"error": f"unknown provider: {provider}",
                        "allowed": sorted(_ALLOWED_PROVIDERS)}), 400

    threading.Thread(
        target=_run_pipeline_in_background,
        kwargs={
            "model_override": model_override,
            "provider_override": provider_override,
            "favorites": favorites,
        },
        daemon=True,
    ).start()
    time.sleep(0.05)
    return jsonify({"started": True, "applied_model": model_override or "auto",
                    "applied_provider": provider_override or "auto",
                    "applied_favorites": favorites,
                    **_snapshot_state()}), 202


@app.get("/status")
def status():
    return jsonify(_snapshot_state())


@app.post("/shutdown")
def shutdown():
    """Cleanly stop the Flask server. Returns immediately; actual shutdown
    happens in a background thread to give the response time to flush."""
    with _state_lock:
        if _state["running"]:
            return jsonify({"ok": False, "error": "Refresh in progress — wait for it to finish before stopping."}), 409

    def _kill_self():
        time.sleep(0.3)  # let the response flush
        # SIGTERM gives the server (and its threads) a chance to clean up.
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_kill_self, daemon=True).start()
    return jsonify({"ok": True, "message": "Server shutting down."}), 200


@app.post("/restart")
def restart():
    """Re-exec the current Python process. Preserves env vars (including
    STOCK_BUZZ_PROVIDER, AWS_PROFILE, etc.) from the original invocation."""
    with _state_lock:
        if _state["running"]:
            return jsonify({"ok": False, "error": "Refresh in progress — wait for it to finish before restarting."}), 409

    def _restart_self():
        # Strategy: spawn a detached child that waits a beat for our socket
        # to release, then execs the new server. We then exit cleanly so the
        # port is freed before the child binds.
        time.sleep(0.3)  # let the response flush
        try:
            args = [sys.executable, "-m", "src.server"] + sys.argv[1:]
            # start_new_session=True detaches from the parent so killing the
            # parent doesn't propagate. The child sleeps ~1.5s before exec —
            # plenty of time for our socket to release.
            subprocess.Popen(
                ["sh", "-c", f"sleep 1.5 && exec {' '.join(repr(a) for a in args)}"],
                cwd=str(PROJECT_ROOT),
                env=os.environ.copy(),
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass
        # Tear ourselves down — the new server will be up in ~2 seconds total.
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_restart_self, daemon=True).start()
    return jsonify({"ok": True, "message": "Server restarting…"}), 200


@app.post("/summarize/<ticker>")
def summarize_one(ticker: str):
    """On-demand summarization for a single ticker. Synchronous (~5-15s).

    Body: optional {"model": "claude-opus-4-7" | ... | "auto"}.
    Returns the summary JSON if successful, with `cached: true|false` set.
    """
    import re
    import time as _time
    if not re.fullmatch(r"[A-Za-z0-9]{1,5}(\.[A-Za-z])?", ticker):
        return jsonify({"error": "invalid ticker format"}), 400
    ticker = ticker.upper()

    body = request.get_json(silent=True) or {}
    model = (body.get("model") or "").strip().lower()
    force_refresh = bool(body.get("force_refresh"))

    # Load env-defined provider/model + apply override.
    from .summarizer import (
        _resolve_provider, _resolve_model, _make_client,
        summarize_ticker, _fingerprint, _get_cached,
    )
    from .scoring import compute_buzz, TickerBuzz
    from .market_data import fetch_market_data

    provider = _resolve_provider()
    if provider == "none":
        return jsonify({"error": "no LLM provider configured (STOCK_BUZZ_PROVIDER=none)"}), 503

    if model in ("", "auto"):
        resolved_model = _resolve_model(provider)
    elif model in {"claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"}:
        resolved_model = _resolve_model(provider, override=model)
    else:
        return jsonify({"error": f"unknown model: {model}"}), 400

    # Find the TickerBuzz entry. Most tickers will be in the latest scrape;
    # if not, we synthesize a placeholder so the LLM still has something to
    # work with (the model gets buzz=0 but still has the company info).
    buzz_list = compute_buzz()
    buzz_lookup = {b.ticker: b for b in buzz_list}
    if ticker in buzz_lookup:
        buzz = buzz_lookup[ticker]
    else:
        buzz = TickerBuzz(
            ticker=ticker, score=0.0, mention_count=0, distinct_posts=0,
            distinct_channels=0, distinct_sources=0,
            total_upvotes=0, total_comments=0, total_followers=0,
            bullish_count=0, bearish_count=0, trending_rank=None,
            sample_posts=[],
        )

    # Pull market data (5-min cache, so very fast for already-fetched tickers).
    md_lookup = fetch_market_data([ticker], verbose=False)
    market = md_lookup.get(ticker)

    client = _make_client(provider)
    if client is None:
        return jsonify({"error": "could not initialize LLM client"}), 503

    t0 = _time.time()
    # If the cache already has a fresh entry under this fingerprint AND the
    # caller did not request force_refresh, just return that.
    fp = _fingerprint(buzz, resolved_model)
    if not force_refresh:
        cached = _get_cached(ticker, fp)
        if cached:
            return jsonify({
                "ticker": ticker,
                "cached": True,
                "elapsed_ms": int((_time.time() - t0) * 1000),
                "model": resolved_model,
                "summary": {
                    "eli15": cached.eli15,
                    "standard": cached.standard,
                    "bull_case": cached.bull_case,
                    "bear_case": cached.bear_case,
                },
            })

    summary = summarize_ticker(
        buzz, market, client=client,
        model=resolved_model, provider=provider,
        use_cache=False,  # force a fresh call when this endpoint is hit
    )
    if not summary:
        return jsonify({
            "ticker": ticker, "cached": False,
            "elapsed_ms": int((_time.time() - t0) * 1000),
            "error": "summarization failed",
        }), 502

    return jsonify({
        "ticker": ticker,
        "cached": False,
        "elapsed_ms": int((_time.time() - t0) * 1000),
        "model": resolved_model,
        "summary": {
            "eli15": summary.eli15,
            "standard": summary.standard,
            "bull_case": summary.bull_case,
            "bear_case": summary.bear_case,
        },
    })


@app.post("/metrics-explain/<ticker>")
def metrics_explain(ticker: str):
    """On-demand metrics explanation for a single ticker. Synchronous (~10-30s).

    Body: optional {"model": "...", "force_refresh": bool}.
    Returns {"overview": "...", "per_metric": {...}, "metric_labels": {...},
             "metric_values": {...}, "model": "...", "cached": bool}.
    """
    import re
    import time as _time
    if not re.fullmatch(r"[A-Za-z0-9]{1,5}(\.[A-Za-z])?", ticker):
        return jsonify({"error": "invalid ticker format"}), 400
    ticker = ticker.upper()

    body = request.get_json(silent=True) or {}
    model = (body.get("model") or "").strip().lower()
    force_refresh = bool(body.get("force_refresh"))

    from .summarizer import _resolve_provider, _resolve_model, _make_client
    from .market_data import fetch_market_data
    from .metrics_explainer import (
        explain_metrics, METRIC_LABELS,
        _format_metric_value, _collect_metric_values,
    )

    provider = _resolve_provider()
    if provider == "none":
        return jsonify({"error": "no LLM provider configured"}), 503

    if model in ("", "auto"):
        resolved_model = _resolve_model(provider)
    elif model in {"claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"}:
        resolved_model = _resolve_model(provider, override=model)
    else:
        return jsonify({"error": f"unknown model: {model}"}), 400

    md_lookup = fetch_market_data([ticker], verbose=False)
    md = md_lookup.get(ticker)
    if md is None:
        return jsonify({"error": "no market data available for this ticker"}), 404

    metric_values = _collect_metric_values(md)
    if not metric_values:
        return jsonify({"error": "no fundamental metrics available — set FINNHUB_API_KEY?"}), 404

    client = _make_client(provider)
    if client is None:
        return jsonify({"error": "could not initialize LLM client"}), 503

    t0 = _time.time()
    exp = explain_metrics(md, client=client, model=resolved_model,
                          provider=provider, force_refresh=force_refresh)
    if not exp:
        return jsonify({
            "ticker": ticker, "cached": False,
            "elapsed_ms": int((_time.time() - t0) * 1000),
            "error": "metrics explanation failed",
        }), 502

    cache_hit = (exp.fetched_at < t0 - 0.1)  # if it was already in cache, fetched_at predates the call
    return jsonify({
        "ticker": ticker,
        "cached": cache_hit and not force_refresh,
        "elapsed_ms": int((_time.time() - t0) * 1000),
        "model": exp.model or resolved_model,
        "overview": exp.overview,
        "per_metric": exp.per_metric,
        "metric_labels": {k: lbl for k, (lbl, _) in metric_values.items()},
        "metric_values": {k: val for k, (_, val) in metric_values.items()},
    })


@app.get("/config")
def config():
    """Returns the provider/model the next refresh will use. UI displays this so
    you always know what the refresh button will bill you for."""
    from .summarizer import _resolve_provider, _resolve_model
    provider = _resolve_provider()
    model = _resolve_model(provider)
    aws_profile = (
        os.environ.get("AWS_PROFILE")
        or os.environ.get("STOCK_BUZZ_AWS_PROFILE")
        if provider == "bedrock" else None
    )
    aws_region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("STOCK_BUZZ_AWS_REGION", "us-east-1")
        if provider == "bedrock" else None
    )
    return jsonify({
        "provider": provider,
        "model": model,
        "aws_profile": aws_profile,
        "aws_region": aws_region,
        "finnhub_configured": bool(os.environ.get("FINNHUB_API_KEY")),
    })


def _snapshot_state() -> dict:
    with _state_lock:
        return dict(_state)


def main():
    parser = argparse.ArgumentParser(description="Stock Buzz local dashboard server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't auto-open a browser tab.")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}/"
    print(f"Stock Buzz server listening on {url}")
    if not args.no_browser:
        # Open in a tiny background thread so the server starts first.
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    # debug=False so we don't double-import + the auto-reload doesn't kill
    # in-flight pipeline threads.
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
