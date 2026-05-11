"""Render the dashboard.

Layout:
- Top toolbar: title, search, sector jumplist, stats
- Top Movers ribbon: horizontally scrollable strip of top buzzy tickers
- Sector grid: 3-column responsive grid where each cell is a collapsible card
  containing compact ticker rows (symbol + price + %)
- Modal overlay: clicking any ticker opens a full-detail modal with the same
  ELI15/Standard tabs, bull/bear cases, market data, chart links, top posts.

Implementation notes:
- All rich per-ticker data is serialized once into a JSON blob in the page;
  the modal pulls from that on click. Keeps DOM small + fast initial paint.
- Search is purely client-side: matches ticker symbol or company name and
  hides non-matching rows live as the user types.
- ESC key, click-outside, and a close button all dismiss the modal.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from jinja2 import Template

from .config import BUZZ_WINDOW_HOURS, REPORT_PATH
from .earnings import EarningsData
from .market_data import MarketData
from .metrics_explainer import (
    METRIC_LABELS, _collect_metric_values,
    _fingerprint as _metrics_fingerprint,
    _get_cached as _get_cached_metrics_explanation,
)
from .scoring import TickerBuzz
from .sectors import group_by_sector
from .storage import latest_run_finished_at
from .summarizer import Summary

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stock Buzz — {{ generated_at }}</title>
<style>
  :root {
    --bg: #0f1419;
    --panel: #1a2029;
    --panel-2: #1f2630;
    --panel-hover: #232b37;
    --border: #2a3340;
    --border-hover: #3a4554;
    --fg: #e6e9ef;
    --muted: #8b95a5;
    --muted-strong: #b0bac9;
    --accent: #4ade80;
    --accent-dim: #22c55e;
    --bull: #22c55e;
    --bear: #ef4444;
    --warn: #f59e0b;
    --link: #60a5fa;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: var(--bg);
    color: var(--fg);
    line-height: 1.45;
    font-size: 14px;
  }

  /* === Top toolbar ============================================== */
  .toolbar {
    position: sticky; top: 0; z-index: 50;
    background: rgba(15, 20, 25, 0.96);
    backdrop-filter: blur(8px);
    border-bottom: 1px solid var(--border);
    padding: 10px 20px;
    display: flex;
    align-items: center;
    gap: 16px;
    flex-wrap: wrap;
  }
  .toolbar h1 {
    margin: 0; font-size: 18px; font-weight: 700;
    color: var(--accent); letter-spacing: 0.5px;
  }
  .toolbar h1 small {
    color: var(--muted); font-weight: 400; font-size: 12px;
    margin-left: 8px; letter-spacing: 0;
  }
  .toolbar .freshness {
    font-size: 11px; color: var(--muted); display: flex; flex-direction: column; gap: 1px; line-height: 1.2;
  }
  .toolbar .freshness .row { white-space: nowrap; }
  .toolbar .freshness b { color: var(--fg); font-weight: 500; }
  .toolbar .freshness .rel { color: var(--accent); }
  .toolbar .freshness .rel.stale { color: var(--warn); }
  .toolbar .freshness .rel.very-stale { color: var(--bear); }
  .toolbar-grow { flex: 1; }
  .toolbar input[type="search"] {
    background: var(--panel); border: 1px solid var(--border);
    color: var(--fg); padding: 6px 12px; border-radius: 6px;
    font-size: 13px; min-width: 220px; font-family: inherit;
  }
  .toolbar input[type="search"]:focus { outline: none; border-color: var(--accent); }
  .toolbar select {
    background: var(--panel); border: 1px solid var(--border);
    color: var(--fg); padding: 6px 10px; border-radius: 6px;
    font-size: 13px; font-family: inherit; cursor: pointer;
  }
  .toolbar .stat-pill {
    background: var(--panel); border: 1px solid var(--border);
    padding: 4px 10px; border-radius: 6px; font-size: 12px;
    color: var(--muted);
  }
  .toolbar .stat-pill strong { color: var(--accent); }
  .toolbar button.refresh-btn {
    background: var(--accent-dim); border: 1px solid var(--accent);
    color: #0a1410; padding: 6px 14px; border-radius: 6px;
    font-size: 13px; font-weight: 600; cursor: pointer;
    font-family: inherit;
    transition: background 0.15s, opacity 0.15s;
    display: flex; align-items: center; gap: 6px;
  }
  .toolbar button.refresh-btn:hover { background: var(--accent); }
  .toolbar button.refresh-btn:disabled { opacity: 0.6; cursor: progress; }

  /* Small overflow menu with Stop / Restart */
  .toolbar .menu-wrap { position: relative; }
  .toolbar button.icon-btn {
    background: var(--panel); border: 1px solid var(--border);
    color: var(--fg); padding: 6px 10px; border-radius: 6px;
    font-size: 14px; cursor: pointer; font-family: inherit;
  }
  .toolbar button.icon-btn:hover { background: var(--panel-hover); border-color: var(--accent); }
  .menu-dropdown {
    display: none;
    /* Position is set by JS when the menu opens (clamped to the viewport so
       the dropdown can never extend off-screen, regardless of where the
       trigger button ended up). Using `position: fixed` so the JS can use
       viewport coordinates directly. */
    position: fixed;
    background: var(--panel-2);
    border: 1px solid var(--border);
    border-radius: 6px;
    min-width: 220px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
    /* Above the sidebars (z-index 40) and the toolbar (50). */
    z-index: 70;
    padding: 4px;
  }
  .menu-dropdown.open { display: block; }
  .menu-dropdown button {
    display: block; width: 100%; text-align: left;
    background: transparent; border: none; color: var(--fg);
    padding: 8px 12px; font-size: 13px; font-family: inherit;
    cursor: pointer; border-radius: 4px;
  }
  .menu-dropdown button:hover { background: var(--panel-hover); }
  .menu-dropdown button.danger { color: var(--bear); }
  .menu-dropdown button.danger:hover { background: rgba(239, 68, 68, 0.08); }
  .menu-dropdown .menu-divider { height: 1px; background: var(--border); margin: 4px 0; }
  .menu-dropdown .menu-label {
    padding: 4px 12px; font-size: 10px; color: var(--muted);
    text-transform: uppercase; letter-spacing: 1px;
  }

  /* Full-screen overlay shown when the server can't be reached */
  .server-down-overlay {
    display: none;
    position: fixed; inset: 0;
    background: rgba(15, 20, 25, 0.92);
    backdrop-filter: blur(6px);
    z-index: 300;
    align-items: center; justify-content: center;
    padding: 30px;
  }
  .server-down-overlay.visible { display: flex; }
  .server-down-card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 30px 32px;
    max-width: 480px;
    text-align: center;
  }
  .server-down-card h2 { margin: 0 0 12px; color: var(--bear); font-size: 20px; }
  .server-down-card p { margin: 8px 0; color: var(--muted-strong); }
  .server-down-card code {
    display: inline-block; padding: 4px 10px; margin: 8px 0;
    background: var(--bg); border: 1px solid var(--border);
    border-radius: 4px; color: var(--accent);
    font-family: ui-monospace, monospace; font-size: 13px;
  }
  .server-down-card .reconnect-status { font-size: 12px; color: var(--muted); margin-top: 14px; }
  .toolbar button.refresh-btn .spinner {
    width: 12px; height: 12px;
    border: 2px solid rgba(10, 20, 16, 0.3);
    border-top-color: #0a1410;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    display: none;
  }
  .toolbar button.refresh-btn.running .spinner { display: inline-block; }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* Status banner shown while a refresh is in flight */
  .refresh-banner {
    display: none;
    position: fixed; top: 0; left: 0; right: 0;
    background: var(--panel-2);
    border-bottom: 2px solid var(--accent);
    padding: 10px 20px;
    z-index: 200;
    font-size: 13px;
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.4);
  }
  .refresh-banner.visible { display: block; }
  .refresh-banner .row {
    display: flex; align-items: center; gap: 12px;
    max-width: 1400px; margin: 0 auto;
  }
  .refresh-banner .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 1px; }
  .refresh-banner .detail { color: var(--fg); flex: 1; }
  .refresh-banner .pct { color: var(--accent); font-variant-numeric: tabular-nums; }
  .refresh-banner .progress {
    width: 100%; height: 3px; background: var(--border);
    margin-top: 6px; border-radius: 2px; overflow: hidden;
  }
  .refresh-banner .progress-bar {
    height: 100%; background: var(--accent);
    width: 0%; transition: width 0.4s ease-out;
  }
  .refresh-banner.error { border-bottom-color: var(--bear); }
  .refresh-banner.error .pct { color: var(--bear); }

  /* === Top Movers ribbon ======================================== */
  .ribbon-wrap {
    background: var(--bg);
    border-bottom: 1px solid var(--border);
    padding: 10px 20px 12px;
  }
  .ribbon-label {
    font-size: 11px; text-transform: uppercase; letter-spacing: 1.2px;
    color: var(--muted); margin-bottom: 6px;
  }
  .ribbon {
    display: flex; gap: 8px; overflow-x: auto;
    padding-bottom: 6px;
  }
  .ribbon::-webkit-scrollbar { height: 6px; }
  .ribbon::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
  .ribbon-chip {
    flex-shrink: 0;
    background: var(--panel); border: 1px solid var(--border);
    padding: 6px 12px; border-radius: 6px; cursor: pointer;
    display: flex; flex-direction: column; gap: 2px;
    min-width: 110px;
    transition: border-color 0.15s, background 0.15s;
  }
  .ribbon-chip:hover { border-color: var(--accent); background: var(--panel-hover); }
  .ribbon-chip .sym { font-weight: 700; color: var(--accent); font-size: 14px; }
  .ribbon-chip .pct { font-size: 12px; }
  .ribbon-chip .pct.up { color: var(--bull); }
  .ribbon-chip .pct.down { color: var(--bear); }
  .ribbon-chip .meta { font-size: 11px; color: var(--muted); }

  /* === Sector grid ============================================== */
  main {
    padding: 20px;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    gap: 14px;
    max-width: 1800px;
    margin: 0 auto;
  }
  .sector-card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
    display: flex; flex-direction: column;
  }
  .sector-header {
    padding: 12px 14px;
    background: var(--panel-2);
    border-bottom: 1px solid var(--border);
    cursor: pointer;
    user-select: none;
    display: flex; justify-content: space-between; align-items: center;
  }
  .sector-header:hover { background: var(--panel-hover); }
  .sector-header h2 {
    margin: 0; font-size: 13px; font-weight: 600;
    color: var(--fg); text-transform: uppercase; letter-spacing: 1px;
  }
  .sector-header .count {
    color: var(--accent); font-size: 12px;
    background: rgba(74, 222, 128, 0.1);
    padding: 2px 8px; border-radius: 10px;
  }
  .sector-header .toggle {
    margin-left: 8px; color: var(--muted); font-size: 12px;
    transition: transform 0.2s;
  }
  .sector-card.collapsed .sector-header .toggle { transform: rotate(-90deg); }
  .sector-card.collapsed .ticker-rows { display: none; }

  .ticker-rows {
    display: flex; flex-direction: column;
    max-height: 380px; overflow-y: auto;
  }
  .ticker-rows::-webkit-scrollbar { width: 6px; }
  .ticker-rows::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  .ticker-row {
    display: grid;
    grid-template-columns: auto auto 1fr auto auto auto;
    gap: 10px;
    align-items: center;
    padding: 8px 14px;
    border-bottom: 1px solid var(--border);
    cursor: pointer;
    transition: background 0.1s;
  }
  .ticker-row:last-child { border-bottom: none; }
  .ticker-row:hover { background: var(--panel-hover); }

  .ticker-row .symbol {
    font-weight: 700; color: var(--accent); font-size: 14px;
    min-width: 56px;
  }
  .ticker-row .name {
    color: var(--muted); font-size: 12px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .ticker-row .price {
    font-variant-numeric: tabular-nums; font-size: 13px;
    color: var(--fg);
  }
  .ticker-row .pct {
    font-variant-numeric: tabular-nums; font-size: 12px;
    min-width: 56px; text-align: right;
    padding: 2px 6px; border-radius: 3px;
  }
  .ticker-row .pct.up { color: var(--bull); background: rgba(34, 197, 94, 0.08); }
  .ticker-row .pct.down { color: var(--bear); background: rgba(239, 68, 68, 0.08); }
  .ticker-row .pct.flat { color: var(--muted); }

  .ticker-row .badges {
    display: flex; gap: 4px;
  }
  .ticker-row .badge {
    font-size: 10px; padding: 1px 5px; border-radius: 3px;
    background: var(--bg); border: 1px solid var(--border);
    color: var(--muted-strong);
  }
  .ticker-row .badge.ai { color: var(--accent); border-color: rgba(74, 222, 128, 0.3); }
  .ticker-row .badge.trending { color: var(--warn); border-color: rgba(245, 158, 11, 0.3); }

  .empty-state {
    padding: 30px; text-align: center; color: var(--muted);
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 10px;
    grid-column: 1 / -1;
  }
  .sector-empty {
    padding: 18px 14px; text-align: center;
    color: var(--muted); font-size: 12px; font-style: italic;
    line-height: 1.5;
  }

  /* === Modal ==================================================== */
  .modal-backdrop {
    display: none;
    position: fixed; inset: 0;
    background: rgba(0, 0, 0, 0.7);
    backdrop-filter: blur(4px);
    z-index: 100;
    align-items: flex-start; justify-content: center;
    padding: 40px 20px;
    overflow-y: auto;
  }
  .modal-backdrop.open { display: flex; }
  .modal {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    width: 100%; max-width: 900px;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
  }
  .modal-header {
    padding: 18px 24px; border-bottom: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: flex-start;
    gap: 16px; flex-wrap: wrap;
  }
  .modal-header .title-block { flex: 1; min-width: 200px; }
  .modal-header h2 {
    margin: 0; font-size: 28px; font-weight: 700; color: var(--accent);
    letter-spacing: 0.5px;
  }
  .modal-header h2 small {
    color: var(--muted); font-size: 14px; font-weight: 400;
    margin-left: 10px; letter-spacing: 0;
  }
  .modal-header .modal-meta {
    display: flex; gap: 8px; margin-top: 6px; flex-wrap: wrap;
    font-size: 12px;
  }
  .modal-header .modal-meta .pill {
    padding: 2px 8px; border-radius: 4px;
    background: var(--panel-2); border: 1px solid var(--border);
    color: var(--muted-strong);
  }
  .modal-header .modal-meta .pill.trending { color: var(--warn); border-color: rgba(245, 158, 11, 0.3); }
  .modal-close {
    background: var(--panel-2); border: 1px solid var(--border);
    color: var(--fg); width: 32px; height: 32px;
    border-radius: 6px; cursor: pointer; font-size: 18px;
    display: flex; align-items: center; justify-content: center;
    font-family: inherit;
  }
  .modal-close:hover { background: var(--panel-hover); border-color: var(--bear); }
  .modal-body { padding: 20px 24px; }

  .market-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
    gap: 10px; margin-bottom: 12px;
    padding: 12px;
    background: var(--panel-2);
    border-radius: 6px; border: 1px solid var(--border);
  }
  .market-cell .label {
    color: var(--muted); display: block;
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px;
  }
  .market-cell .value {
    color: var(--fg); font-weight: 600; font-size: 14px;
    margin-top: 2px;
  }
  .market-cell .value.up { color: var(--bull); }
  .market-cell .value.down { color: var(--bear); }
  .market-cell .value small { font-size: 12px; opacity: 0.85; }

  .chart-links { margin-bottom: 16px; display: flex; gap: 8px; flex-wrap: wrap; font-size: 12px; }
  .chart-links a {
    color: var(--link); text-decoration: none;
    border: 1px solid var(--border);
    padding: 4px 10px; border-radius: 4px;
    background: var(--panel-2);
    transition: border-color 0.15s;
  }
  .chart-links a:hover { border-color: var(--link); }
  .chart-links a.trusted::before { content: '★ '; color: var(--warn); }

  .buzz-stats {
    display: flex; gap: 14px; margin-bottom: 16px; flex-wrap: wrap;
    color: var(--muted); font-size: 13px;
  }
  .buzz-stats b { color: var(--fg); }
  .buzz-stats .bull { color: var(--bull); }
  .buzz-stats .bear { color: var(--bear); }

  /* Stock Metrics block (Plain English + Numbers) */
  .metrics-block {
    border-top: 1px dashed var(--border);
    padding-top: 12px; margin-top: 14px;
  }
  .metrics-block summary {
    list-style: none;
    cursor: pointer; user-select: none;
    display: flex; align-items: center; gap: 12px;
    padding: 4px 0;
    flex-wrap: wrap;
  }
  .metrics-block summary::-webkit-details-marker { display: none; }
  .metrics-block summary::before {
    content: '▸';
    color: var(--muted);
    font-size: 12px;
    transition: transform 0.15s;
    display: inline-block;
  }
  .metrics-block[open] summary::before { transform: rotate(90deg); }
  .metrics-block summary h4 {
    margin: 0; font-size: 12px; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.8px; font-weight: 600;
  }
  .metrics-block summary:hover h4 { color: var(--fg); }
  .metrics-block .metrics-summary-hint {
    font-size: 11px; color: var(--muted);
    font-style: italic;
  }
  .metrics-block .metrics-content {
    margin-top: 12px;
  }
  .metrics-block-header {
    display: flex; align-items: center; gap: 12px; margin-bottom: 10px;
    flex-wrap: wrap;
  }
  .metrics-grid {
    display: grid;
    grid-template-columns: 1.4fr 1fr;
    gap: 14px;
    align-items: start;
  }
  @media (max-width: 800px) { .metrics-grid { grid-template-columns: 1fr; } }
  .metrics-pane {
    background: var(--panel-2);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 12px 14px;
  }
  .metrics-pane h5 {
    margin: 0 0 8px; font-size: 11px; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.8px; font-weight: 600;
  }
  .metrics-overview { line-height: 1.65; font-size: 13px; }
  .metrics-overview.empty { color: var(--muted); font-style: italic; }
  .metrics-list {
    list-style: none; padding: 0; margin: 0;
    display: grid; grid-template-columns: 1fr; gap: 4px;
  }
  .metrics-list li {
    display: grid; grid-template-columns: 1fr auto;
    gap: 10px; align-items: baseline;
    font-size: 13px;
    padding: 6px 0;
    border-bottom: 1px dashed var(--border);
  }
  .metrics-list li:last-child { border-bottom: none; }
  .metrics-list .label { color: var(--muted-strong); }
  .metrics-list .value {
    font-variant-numeric: tabular-nums; font-weight: 600;
    color: var(--fg);
  }
  .metrics-list .value.up { color: var(--bull); }
  .metrics-list .value.down { color: var(--bear); }
  .metrics-list li.has-explanation { cursor: help; position: relative; }
  .metrics-list li.has-explanation:hover { background: rgba(74,222,128,0.04); }
  .metrics-list .per-metric-text {
    grid-column: 1 / -1;
    font-size: 12px; color: var(--muted-strong);
    line-height: 1.5; padding: 4px 0 6px;
    display: none;
  }
  .metrics-list li.expanded .per-metric-text { display: block; }
  .metrics-list .toggle-hint {
    font-size: 10px; color: var(--accent-dim); margin-left: 6px;
    user-select: none;
  }

  .summary-block { border-top: 1px dashed var(--border); padding-top: 14px; margin-top: 4px; }
  .summary-actions {
    display: flex; align-items: center; gap: 12px; margin-bottom: 10px;
  }
  .summary-gen-btn {
    background: var(--accent-dim); border: 1px solid var(--accent);
    color: #0a1410; padding: 6px 14px; border-radius: 6px;
    font-size: 12px; font-weight: 600; cursor: pointer;
    font-family: inherit;
    transition: background 0.15s, opacity 0.15s;
    text-transform: uppercase; letter-spacing: 0.6px;
  }
  .summary-gen-btn:hover { background: var(--accent); }
  .summary-gen-btn:disabled { opacity: 0.6; cursor: progress; }
  .summary-status {
    font-size: 12px; color: var(--muted); font-style: italic;
  }
  .summary-status.ok { color: var(--accent); font-style: normal; }
  .summary-status.err { color: var(--bear); font-style: normal; }
  .summary-tabs { padding-top: 0; margin-top: 0; }
  .tab-buttons { display: flex; gap: 4px; margin-bottom: 12px; }
  .tab-button {
    background: var(--panel-2); border: 1px solid var(--border);
    color: var(--muted); padding: 5px 14px; border-radius: 4px;
    cursor: pointer; font-size: 11px; font-family: inherit;
    text-transform: uppercase; letter-spacing: 0.8px;
  }
  .tab-button.active {
    color: var(--accent); border-color: var(--accent);
    background: rgba(74, 222, 128, 0.08);
  }
  .tab-content { display: none; }
  .tab-content.active { display: block; }

  .summary-section { margin-bottom: 14px; }
  .summary-section h4 {
    margin: 0 0 6px; font-size: 11px; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.8px; font-weight: 600;
  }
  .summary-section p { margin: 0; line-height: 1.6; }

  .bull-bear-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 16px; }
  .bull-bear-grid .case-card {
    padding: 12px 14px; border-radius: 6px;
    background: var(--panel-2); border: 1px solid var(--border);
  }
  .bull-bear-grid .case-card.bull { border-left: 3px solid var(--bull); }
  .bull-bear-grid .case-card.bear { border-left: 3px solid var(--bear); }
  .case { white-space: pre-line; line-height: 1.6; }
  @media (max-width: 720px) { .bull-bear-grid { grid-template-columns: 1fr; } }

  details.posts-details { margin-top: 12px; border-top: 1px dashed var(--border); padding-top: 10px; }
  details.posts-details summary {
    cursor: pointer; color: var(--accent-dim);
    font-size: 12px; user-select: none;
    text-transform: uppercase; letter-spacing: 0.8px;
  }
  details.posts-details summary:hover { color: var(--accent); }
  .post-list { list-style: none; padding: 10px 0 0; margin: 0; }
  .post-list li { padding: 8px 0; border-top: 1px dashed var(--border); }
  .post-list li:first-child { border-top: none; }
  .post-list a { color: var(--fg); text-decoration: none; font-size: 13px; }
  .post-list a:hover { color: var(--accent); text-decoration: underline; }
  .post-meta { color: var(--muted); font-size: 11px; margin-top: 2px; }
  .post-meta .pill {
    display: inline-block; padding: 1px 6px; border-radius: 3px;
    background: var(--bg); border: 1px solid var(--border); margin-right: 4px;
  }
  .post-meta .pill.bull { color: var(--bull); border-color: var(--bull); }
  .post-meta .pill.bear { color: var(--bear); border-color: var(--bear); }

  .summary-missing {
    color: var(--muted); font-style: italic;
    padding: 8px 0; font-size: 13px;
  }

  footer {
    padding: 24px 20px; color: var(--muted); font-size: 12px;
    text-align: center; max-width: 1800px; margin: 0 auto;
  }

  /* === Star (favorite) icon =================================== */
  .star-btn {
    background: transparent; border: none; cursor: pointer;
    color: var(--muted); padding: 0 4px; font-size: 14px;
    line-height: 1; transition: color 0.15s;
  }
  .star-btn:hover { color: var(--warn); }
  .star-btn.faved { color: var(--warn); }
  .ticker-row .star-btn { margin-right: 2px; }

  /* === Right-edge sidebars ==================================== */
  .sidebar {
    position: fixed; top: 64px; right: 0; bottom: 0;
    width: 28px;
    background: var(--panel-2);
    border-left: 1px solid var(--border);
    z-index: 40;
    transition: width 0.2s ease;
    overflow: hidden;
    display: flex; flex-direction: column;
  }
  .sidebar.expanded { width: 380px; box-shadow: -4px 0 18px rgba(0,0,0,0.35); }
  .sidebar.favorites { top: 64px; bottom: 50%; }
  .sidebar.earnings { top: 50%; bottom: 0; }
  .sidebar.expanded.favorites,
  .sidebar.expanded.earnings { top: 64px; bottom: 0; }
  .sidebar.expanded.favorites + .sidebar.earnings { display: none; }
  .sidebar.earnings.expanded ~ .sidebar.favorites { display: none; }

  .sidebar-tab {
    width: 100%; height: 100%;
    cursor: pointer; user-select: none;
    display: flex; align-items: flex-start; justify-content: center;
    padding-top: 14px;
    color: var(--muted-strong);
    font-size: 11px;
    letter-spacing: 2px;
    text-transform: uppercase;
    writing-mode: vertical-rl;
    transform: rotate(180deg);
    transition: color 0.15s, background 0.15s;
  }
  .sidebar-tab:hover { color: var(--accent); background: var(--panel-hover); }
  .sidebar.expanded .sidebar-tab { display: none; }

  .sidebar-content {
    display: none;
    flex: 1; overflow-y: auto;
    padding: 14px 16px 20px;
  }
  .sidebar.expanded .sidebar-content { display: block; }
  .sidebar-content::-webkit-scrollbar { width: 6px; }
  .sidebar-content::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  .sidebar-header {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 14px; padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
  }
  .sidebar-header h3 {
    margin: 0; font-size: 13px; font-weight: 600;
    color: var(--fg); text-transform: uppercase; letter-spacing: 1.5px;
  }
  .sidebar-close {
    background: var(--panel); border: 1px solid var(--border);
    color: var(--muted); padding: 4px 12px; border-radius: 4px;
    font-size: 16px; line-height: 1; cursor: pointer; font-family: inherit;
    font-weight: 600;
    transition: color 0.1s, border-color 0.1s, background 0.1s;
  }
  .sidebar-close:hover { color: var(--bear); border-color: var(--bear); background: rgba(239,68,68,0.08); }
  .sidebar-close::after { content: ' Close'; font-size: 11px; font-weight: 400; }

  /* Favorite mini-cards */
  .fav-card {
    padding: 10px 12px;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    margin-bottom: 8px;
    cursor: pointer;
    transition: border-color 0.15s, transform 0.1s;
  }
  .fav-card:hover { border-color: var(--accent); transform: translateY(-1px); }
  .fav-card .head {
    display: flex; justify-content: space-between; align-items: baseline;
  }
  .fav-card .sym { font-weight: 700; color: var(--accent); font-size: 14px; }
  .fav-card .pct {
    font-variant-numeric: tabular-nums; font-size: 13px;
    padding: 2px 6px; border-radius: 3px;
  }
  .fav-card .pct.up { color: var(--bull); }
  .fav-card .pct.down { color: var(--bear); }
  .fav-card .meta { font-size: 11px; color: var(--muted); margin-top: 4px; line-height: 1.4; }
  .fav-card .name { font-size: 11px; color: var(--muted-strong); margin-top: 2px; }
  .fav-card .unfav {
    float: right; background: transparent; border: none;
    color: var(--bear); cursor: pointer; font-size: 12px; padding: 0 4px;
    opacity: 0.6;
  }
  .fav-card .unfav:hover { opacity: 1; }
  .sidebar-empty {
    color: var(--muted); font-size: 12px; font-style: italic;
    text-align: center; padding: 20px 0;
  }
  .sidebar-hint { color: var(--muted); font-size: 11px; margin-top: 10px; line-height: 1.5; }

  /* Earnings cards */
  .earn-card {
    padding: 10px 12px;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    margin-bottom: 8px;
  }
  .earn-card .top-row {
    display: flex; justify-content: space-between; align-items: baseline;
    margin-bottom: 6px;
  }
  .earn-card .sym { font-weight: 700; color: var(--accent); font-size: 14px; cursor: pointer; }
  .earn-card .sym:hover { text-decoration: underline; }
  .earn-card .next-date {
    font-size: 11px; color: var(--warn);
    background: rgba(245, 158, 11, 0.08);
    padding: 2px 8px; border-radius: 3px;
    border: 1px solid rgba(245, 158, 11, 0.3);
    font-variant-numeric: tabular-nums;
  }
  .earn-card .next-date.tomorrow { color: var(--bear); border-color: rgba(239,68,68,0.4); background: rgba(239,68,68,0.08); }
  .earn-card .name { font-size: 11px; color: var(--muted); margin-bottom: 6px; }
  .earn-card .stat-row {
    display: flex; gap: 10px; flex-wrap: wrap;
    font-size: 11px; color: var(--muted-strong);
    margin-bottom: 6px;
  }
  .earn-card .stat-row b { color: var(--fg); font-variant-numeric: tabular-nums; }
  .earn-card .surprise {
    display: inline-block; padding: 1px 6px; border-radius: 3px;
    font-size: 10px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.5px;
    margin-left: 4px;
  }
  .earn-card .surprise.beat { color: var(--bull); background: rgba(34,197,94,0.1); border: 1px solid rgba(34,197,94,0.3); }
  .earn-card .surprise.meet { color: var(--muted-strong); background: var(--bg); border: 1px solid var(--border); }
  .earn-card .surprise.miss { color: var(--bear); background: rgba(239,68,68,0.1); border: 1px solid rgba(239,68,68,0.3); }
  .earn-card .links { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; padding-top: 6px; border-top: 1px dashed var(--border); }
  .earn-card .links a {
    color: var(--link); text-decoration: none;
    font-size: 11px; padding: 2px 6px; border-radius: 3px;
    background: var(--bg); border: 1px solid var(--border);
  }
  .earn-card .links a:hover { border-color: var(--link); }
  .earn-card .links .trusted::before { content: '★ '; color: var(--warn); }

  @media (max-width: 700px) {
    .toolbar input[type="search"] { min-width: 0; flex: 1; }
    main { grid-template-columns: 1fr; padding: 12px; gap: 10px; }
    .ticker-row { grid-template-columns: auto auto 1fr auto auto; }
    .ticker-row .name { display: none; }
  }
</style>
</head>
<body>

<div class="toolbar">
  <h1>Stock Buzz</h1>
  <div class="freshness" id="freshness"
       data-last-pull-utc="{{ last_pull_utc or '' }}"
       data-prices-as-of-utc="{{ prices_as_of_utc or '' }}">
    <div class="row" id="freshness-pull">
      Last data pull: <b>—</b>
    </div>
    <div class="row" id="freshness-prices">
      Prices as of: <b>—</b>
    </div>
  </div>
  <input type="search" id="search-box" placeholder="Filter ticker or name… (/)" autocomplete="off">
  <select id="sector-jump">
    <option value="">Jump to sector…</option>
    {% for sector_name in sectors.keys() %}
    <option value="sector-{{ loop.index }}">{{ sector_name }} ({{ sectors[sector_name]|length }})</option>
    {% endfor %}
  </select>
  <div class="toolbar-grow"></div>
  <span class="stat-pill"><strong>{{ total_tickers }}</strong> tickers</span>
  <span class="stat-pill"><strong>{{ total_mentions }}</strong> mentions</span>
  <span class="stat-pill"><strong>{{ summarized_count }}</strong> AI</span>
  <span class="stat-pill" id="provider-pill" title="Server's default provider/model. The picker below overrides it for the next refresh."
        style="display:none;"></span>
  <select id="model-picker" title="Model used by the next refresh">
    <option value="auto">Model: Auto (server default)</option>
    <option value="claude-opus-4-7">Opus 4.7 (best quality, ~$0.10/run)</option>
    <option value="claude-sonnet-4-6">Sonnet 4.6 (balanced, ~$0.02/run)</option>
    <option value="claude-haiku-4-5">Haiku 4.5 (cheapest, ~$0.005/run)</option>
    <option value="none">No AI (fastest, free)</option>
  </select>
  <button class="refresh-btn" id="refresh-btn" title="Re-run pipeline in the background">
    <span class="spinner"></span>
    <span class="label">Refresh</span>
  </button>
  <div class="menu-wrap">
    <button class="icon-btn" id="server-menu-btn" title="Server controls" aria-haspopup="true" aria-expanded="false">⋯</button>
    <div class="menu-dropdown" id="server-menu">
      <div class="menu-label">Server</div>
      <button id="menu-restart">↻ Restart server</button>
      <div class="menu-divider"></div>
      <button id="menu-shutdown" class="danger">⏻ Stop server</button>
    </div>
  </div>
</div>

<div class="server-down-overlay" id="server-down-overlay">
  <div class="server-down-card">
    <h2>Server stopped</h2>
    <p>The dashboard server isn't responding. You won't be able to refresh data until it's back up.</p>
    <p>To restart, open a terminal and run:</p>
    <code>stock-buzz-server</code>
    <p style="font-size: 12px; margin-top: 16px;">This page will auto-reconnect when the server comes back.</p>
    <div class="reconnect-status" id="reconnect-status">Trying to reconnect…</div>
  </div>
</div>

<div class="refresh-banner" id="refresh-banner">
  <div class="row">
    <span class="label" id="refresh-phase">Refreshing</span>
    <span class="detail" id="refresh-detail">Starting…</span>
    <span class="pct" id="refresh-pct">0%</span>
  </div>
  <div class="progress"><div class="progress-bar" id="refresh-progress-bar"></div></div>
</div>

<div class="ribbon-wrap">
  <div class="ribbon-label">Top Buzz</div>
  <div class="ribbon">
    {% for entry in top_movers %}
      {% set t = entry.buzz %}
      {% set md = entry.market %}
      <div class="ribbon-chip" data-ticker="{{ t.ticker }}">
        <span class="sym">${{ t.ticker }}</span>
        {% if md and md.price is not none %}
          {% set pct = md.percent_change %}
          <span class="pct {% if pct is not none and pct > 0 %}up{% elif pct is not none and pct < 0 %}down{% endif %}">
            ${{ '%.2f' % md.price }}{% if pct is not none %} ({{ '%+.1f' % pct }}%){% endif %}
          </span>
        {% endif %}
        <span class="meta">#{{ entry.global_rank }} · {{ t.distinct_posts }} posts</span>
      </div>
    {% endfor %}
  </div>
</div>

<main>
  {% if not sectors %}
    <div class="empty-state">No ticker mentions found. Try running the scrape again or expanding the source list.</div>
  {% endif %}

  {% for sector_name, ticker_entries in sectors.items() %}
  <section class="sector-card" data-sector="sector-{{ loop.index }}" id="sector-{{ loop.index }}">
    <div class="sector-header">
      <h2>{{ sector_name }}</h2>
      <span style="display: flex; align-items: center; gap: 8px;">
        <span class="count">{{ ticker_entries|length }}</span>
        <span class="toggle">▼</span>
      </span>
    </div>
    <div class="ticker-rows">
      {% if not ticker_entries %}
        <div class="sector-empty">No tickers in this sector are buzzing right now. This sector is pinned and always shown.</div>
      {% endif %}
      {% for entry in ticker_entries %}
      {% set t = entry.buzz %}
      {% set md = entry.market %}
      {% set has_summary = entry.summary is not none %}
      <div class="ticker-row" data-ticker="{{ t.ticker }}" data-name="{{ md.long_name if md and md.long_name else '' }}">
        <button class="star-btn" data-fav-toggle="{{ t.ticker }}" title="Favorite this ticker" aria-label="Favorite">☆</button>
        <span class="symbol">${{ t.ticker }}</span>
        <span class="name">{% if md and md.long_name %}{{ md.long_name }}{% endif %}</span>
        {% if md and md.price is not none %}
          <span class="price">${{ '%.2f' % md.price }}</span>
          {% set pct = md.percent_change %}
          <span class="pct {% if pct is not none and pct > 0 %}up{% elif pct is not none and pct < 0 %}down{% else %}flat{% endif %}">
            {% if pct is not none %}{{ '%+.2f' % pct }}%{% else %}—{% endif %}
          </span>
        {% else %}
          <span class="price">—</span>
          <span class="pct flat">—</span>
        {% endif %}
        <span class="badges">
          {% if t.trending_rank %}<span class="badge trending">#{{ t.trending_rank }}</span>{% endif %}
          {% if has_summary %}<span class="badge ai">AI</span>{% endif %}
        </span>
      </div>
      {% endfor %}
    </div>
  </section>
  {% endfor %}
</main>

<footer>
  Phase 2 prototype · buzz from public StockTwits API · market data from Yahoo public chart endpoint · summaries from Claude. Reddit pending API approval. Not financial advice.
</footer>

<!-- Right-edge sidebars (favorites + earnings) -->
<aside class="sidebar favorites" id="sidebar-favorites">
  <div class="sidebar-tab" data-toggle="favorites">★ Favorites</div>
  <div class="sidebar-content" id="favorites-content">
    <div class="sidebar-header">
      <h3>★ Favorites</h3>
      <button class="sidebar-close" data-close="favorites" aria-label="Close">×</button>
    </div>
    <div id="favorites-list"></div>
    <div class="sidebar-hint">
      Click the ☆ next to any ticker to add it here. Stored in this browser only.
    </div>
  </div>
</aside>

<aside class="sidebar earnings" id="sidebar-earnings">
  <div class="sidebar-tab" data-toggle="earnings">📅 Earnings</div>
  <div class="sidebar-content" id="earnings-content">
    <div class="sidebar-header">
      <h3>📅 Upcoming Earnings</h3>
      <button class="sidebar-close" data-close="earnings" aria-label="Close">×</button>
    </div>
    <div id="earnings-list"></div>
    <div class="sidebar-hint">
      Sorted by next earnings date — soonest first. Sources prefer SEC + AP/Reuters/Bloomberg/WSJ.
    </div>
  </div>
</aside>

<!-- Modal overlay -->
<div class="modal-backdrop" id="modal-backdrop">
  <div class="modal" id="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title"></div>
</div>

<script id="ticker-data" type="application/json">{{ ticker_data_json | safe }}</script>
<script>
(function() {
  const tickerData = JSON.parse(document.getElementById('ticker-data').textContent);

  // ---- Data freshness display -------------------------------------------
  // Show "Last data pull: 9:23 AM (2 min ago)" using the user's local TZ.
  // Updates every 30s so the relative time stays accurate.
  function fmtAbsoluteTime(d) {
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    const time = d.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
    if (sameDay) return time;
    // Different day: include short date
    const dateStr = d.toLocaleDateString([], {month: 'short', day: 'numeric'});
    return `${dateStr} ${time}`;
  }
  function fmtRelative(d) {
    const secs = Math.floor((Date.now() - d.getTime()) / 1000);
    if (secs < 5) return 'just now';
    if (secs < 60) return `${secs}s ago`;
    const mins = Math.floor(secs / 60);
    if (mins < 60) return `${mins} min ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ${mins % 60}m ago`;
    const days = Math.floor(hrs / 24);
    return `${days}d ago`;
  }
  function stalenessClass(d, warnMins, bearMins) {
    const mins = (Date.now() - d.getTime()) / 60000;
    if (mins >= bearMins) return 'very-stale';
    if (mins >= warnMins) return 'stale';
    return '';
  }
  function renderFreshness() {
    const wrap = document.getElementById('freshness');
    if (!wrap) return;
    const lastPull = wrap.dataset.lastPullUtc;
    const pricesAsOf = wrap.dataset.pricesAsOfUtc;
    const pullEl = document.getElementById('freshness-pull');
    const priceEl = document.getElementById('freshness-prices');
    if (lastPull) {
      const d = new Date(lastPull);
      const klass = stalenessClass(d, 15, 60);  // warn after 15min, bad after 1h
      pullEl.innerHTML = `Last data pull: <b>${fmtAbsoluteTime(d)}</b> <span class="rel ${klass}">(${fmtRelative(d)})</span>`;
    } else {
      pullEl.innerHTML = `Last data pull: <b>never</b>`;
    }
    if (pricesAsOf) {
      const d = new Date(pricesAsOf);
      const klass = stalenessClass(d, 10, 30);  // prices warn faster
      priceEl.innerHTML = `Prices as of: <b>${fmtAbsoluteTime(d)}</b> <span class="rel ${klass}">(${fmtRelative(d)})</span>`;
    } else {
      priceEl.innerHTML = `Prices as of: <b>—</b>`;
    }
  }
  renderFreshness();
  setInterval(renderFreshness, 30 * 1000);
  // -----------------------------------------------------------------------

  // ==========================================================================
  // Favorites — localStorage-backed set of ticker symbols. Star icons in the
  // sector grid toggle membership; the favorites sidebar lists them with
  // live price/% data pulled from `tickerData`.
  // ==========================================================================
  const FAV_KEY = 'stockBuzzFavorites';
  function loadFavs() {
    try {
      const raw = localStorage.getItem(FAV_KEY);
      return new Set(raw ? JSON.parse(raw) : []);
    } catch (e) { return new Set(); }
  }
  function saveFavs(set) {
    localStorage.setItem(FAV_KEY, JSON.stringify(Array.from(set)));
  }
  let favorites = loadFavs();

  function isFav(ticker) { return favorites.has(ticker); }
  function toggleFav(ticker) {
    if (favorites.has(ticker)) favorites.delete(ticker);
    else favorites.add(ticker);
    saveFavs(favorites);
    paintStars();
    renderFavorites();
  }

  function paintStars() {
    document.querySelectorAll('[data-fav-toggle]').forEach(btn => {
      const t = btn.dataset.favToggle;
      const faved = isFav(t);
      btn.classList.toggle('faved', faved);
      btn.textContent = faved ? '★' : '☆';
      btn.title = faved ? 'Remove from favorites' : 'Add to favorites';
    });
  }

  // Click-to-toggle, but stop propagation so we don't also open the modal.
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-fav-toggle]');
    if (!btn) return;
    e.stopPropagation();
    toggleFav(btn.dataset.favToggle);
  });

  function renderFavorites() {
    const list = document.getElementById('favorites-list');
    if (!list) return;
    if (favorites.size === 0) {
      list.innerHTML = '<div class="sidebar-empty">No favorites yet. Click ☆ next to any ticker.</div>';
      return;
    }
    // Build cards in the order tickers appear in tickerData (which is buzz-rank order).
    const ordered = Array.from(favorites)
      .map(t => tickerData[t])
      .filter(Boolean)
      .sort((a, b) => (a.global_rank || 9999) - (b.global_rank || 9999));
    // Tickers in localStorage that aren't in the current dataset still get a stub card.
    const seen = new Set(ordered.map(t => t.ticker));
    favorites.forEach(t => { if (!seen.has(t)) ordered.push({ticker: t, _stub: true}); });

    list.innerHTML = ordered.map(t => {
      if (t._stub) {
        return `<div class="fav-card" data-open-ticker="${t.ticker}">
          <div class="head">
            <span class="sym">$${t.ticker}</span>
            <button class="unfav" data-unfav="${t.ticker}" title="Remove">×</button>
          </div>
          <div class="meta">No live data — not in current scrape window.</div>
        </div>`;
      }
      const md = t.market;
      const pct = (md && md.percent_change != null) ? md.percent_change : null;
      const pctClass = pct == null ? '' : (pct > 0 ? 'up' : (pct < 0 ? 'down' : ''));
      const pctStr = pct == null ? '—' : `${pct > 0 ? '+' : ''}${pct.toFixed(2)}%`;
      const priceStr = md && md.price != null ? `$${md.price.toFixed(2)}` : '—';
      return `<div class="fav-card" data-open-ticker="${t.ticker}">
        <div class="head">
          <span class="sym">$${t.ticker}</span>
          <span><span class="pct ${pctClass}">${priceStr} ${pctStr}</span><button class="unfav" data-unfav="${t.ticker}" title="Remove">×</button></span>
        </div>
        ${md && md.long_name ? `<div class="name">${escapeHtml(md.long_name)}</div>` : ''}
        <div class="meta">
          #${t.global_rank} · ${t.distinct_posts} posts · score ${t.score}
          ${t.trending_rank ? ` · trending #${t.trending_rank}` : ''}
        </div>
      </div>`;
    }).join('');
  }

  // Click handlers on favorites list (open modal / unfav)
  document.getElementById('favorites-list').addEventListener('click', (e) => {
    const unfav = e.target.closest('[data-unfav]');
    if (unfav) {
      e.stopPropagation();
      toggleFav(unfav.dataset.unfav);
      return;
    }
    const card = e.target.closest('[data-open-ticker]');
    if (card) {
      const t = card.dataset.openTicker;
      if (tickerData[t]) {
        renderModal(tickerData[t]);
        document.getElementById('modal-backdrop').classList.add('open');
        document.body.style.overflow = 'hidden';
      }
    }
  });

  // ==========================================================================
  // Earnings sidebar — list every ticker in tickerData that has a next_date,
  // sorted by date ascending (soonest first).
  // ==========================================================================
  function renderEarnings() {
    const list = document.getElementById('earnings-list');
    if (!list) return;
    const today = new Date(); today.setHours(0,0,0,0);
    const todayStr = today.toISOString().slice(0, 10);

    const items = Object.values(tickerData).filter(t => {
      const e = t.earnings;
      return e && e.next_date && e.next_date >= todayStr;
    }).sort((a, b) => (a.earnings.next_date || '').localeCompare(b.earnings.next_date || ''));

    if (items.length === 0) {
      list.innerHTML = '<div class="sidebar-empty">No upcoming earnings dates available. Refresh to fetch the latest schedule.</div>';
      return;
    }

    list.innerHTML = items.map(t => {
      const e = t.earnings;
      const md = t.market;
      const daysUntil = Math.round((new Date(e.next_date) - today) / 86400000);
      const dateLabel = daysUntil === 0 ? 'today'
                      : daysUntil === 1 ? 'tomorrow'
                      : daysUntil <= 7 ? `${daysUntil}d (${new Date(e.next_date).toLocaleDateString([], {weekday: 'short', month: 'short', day: 'numeric'})})`
                      : new Date(e.next_date).toLocaleDateString([], {month: 'short', day: 'numeric'});
      const dateClass = daysUntil <= 1 ? 'tomorrow' : '';

      const surpriseHtml = e.last_surprise
        ? `<span class="surprise ${e.last_surprise}">${e.last_surprise}${e.last_surprise_pct != null ? ' ' + (e.last_surprise_pct > 0 ? '+' : '') + e.last_surprise_pct.toFixed(1) + '%' : ''}</span>`
        : '';

      const lastEpsHtml = e.last_actual_eps != null
        ? `<span>Last EPS: <b>$${e.last_actual_eps.toFixed(2)}</b>${e.last_estimate_eps != null ? ` <small>(est. $${e.last_estimate_eps.toFixed(2)})</small>` : ''}${surpriseHtml}</span>`
        : '';
      const nextEpsHtml = e.next_estimate_eps != null
        ? `<span>Est. EPS: <b>$${e.next_estimate_eps.toFixed(2)}</b></span>`
        : '';

      // Trusted news links — top 3 news items, prefer trusted-source ones first
      // (already sorted server-side). Plus the SEC EDGAR link, always.
      const newsLinks = (e.news || [])
        .slice(0, 3)
        .map(n => `<a href="${escapeHtml(n.url)}" target="_blank" rel="noopener" title="${escapeHtml(n.source)}: ${escapeHtml(n.headline)}" class="${n.is_trusted ? 'trusted' : ''}">${escapeHtml(n.source)}</a>`)
        .join('');
      const secLink = e.sec_edgar_url
        ? `<a href="${escapeHtml(e.sec_edgar_url)}" target="_blank" rel="noopener" class="trusted" title="SEC EDGAR — official 10-K/10-Q filings">SEC Filings</a>`
        : '';

      return `<div class="earn-card">
        <div class="top-row">
          <span class="sym" data-open-ticker="${t.ticker}">$${t.ticker}</span>
          <span class="next-date ${dateClass}">${dateLabel}</span>
        </div>
        ${md && md.long_name ? `<div class="name">${escapeHtml(md.long_name)}</div>` : ''}
        <div class="stat-row">${lastEpsHtml} ${nextEpsHtml}</div>
        <div class="links">${secLink}${newsLinks}</div>
      </div>`;
    }).join('');
  }

  document.getElementById('earnings-list').addEventListener('click', (e) => {
    const sym = e.target.closest('[data-open-ticker]');
    if (sym) {
      const t = sym.dataset.openTicker;
      if (tickerData[t]) {
        renderModal(tickerData[t]);
        document.getElementById('modal-backdrop').classList.add('open');
        document.body.style.overflow = 'hidden';
      }
    }
  });

  // ==========================================================================
  // Sidebar tabs: collapsed thin strip on the right edge → clicked = expanded
  // ==========================================================================
  function setSidebar(name, expanded) {
    document.querySelectorAll('.sidebar').forEach(s => {
      const isMatch = s.id === `sidebar-${name}`;
      if (isMatch) s.classList.toggle('expanded', expanded);
      else s.classList.remove('expanded');  // only one open at a time
    });
  }
  document.querySelectorAll('[data-toggle]').forEach(tab => {
    tab.addEventListener('click', (e) => {
      e.stopPropagation();
      setSidebar(tab.dataset.toggle, true);
    });
  });
  document.querySelectorAll('[data-close]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      setSidebar(btn.dataset.close, false);
    });
  });
  // ESC closes any open sidebar
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      document.querySelectorAll('.sidebar.expanded').forEach(s => s.classList.remove('expanded'));
    }
  });
  // Click-outside-to-close: any click that isn't inside an expanded sidebar,
  // the modal, or the server menu collapses any open sidebar. The
  // stopPropagation calls above prevent the tab itself / inner clicks from
  // triggering this.
  document.addEventListener('click', (e) => {
    const expanded = document.querySelector('.sidebar.expanded');
    if (!expanded) return;
    if (expanded.contains(e.target)) return;
    // Don't close if the click landed on the modal or its content (the modal
    // overlay sits above the sidebar; we want the sidebar to stay open while
    // the user reads modal contents).
    if (e.target.closest('.modal-backdrop, .modal')) return;
    expanded.classList.remove('expanded');
  });

  // Initial paint of favorites + earnings (these reference escapeHtml /
  // renderModal which are defined later in this IIFE; function declarations
  // are hoisted so this works).
  paintStars();
  renderFavorites();
  renderEarnings();
  // -----------------------------------------------------------------------
  const backdrop = document.getElementById('modal-backdrop');
  const modal = document.getElementById('modal');

  // ---- Modal rendering ----
  function fmt(n, decimals=2) {
    if (n === null || n === undefined) return '—';
    return n.toLocaleString('en-US', {minimumFractionDigits: decimals, maximumFractionDigits: decimals});
  }
  function escapeHtml(s) {
    if (!s) return '';
    return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  function renderModal(t) {
    const md = t.market;
    const s = t.summary;
    const pct = md && md.percent_change != null ? md.percent_change : null;
    const pctClass = pct == null ? 'flat' : (pct > 0 ? 'up' : (pct < 0 ? 'down' : 'flat'));

    let marketHtml = '';
    if (md && md.price != null) {
      const cells = [];
      cells.push(`<div class="market-cell"><span class="label">Price</span><span class="value ${pctClass}">$${fmt(md.price)}${pct != null ? ` <small>(${pct > 0 ? '+' : ''}${fmt(pct)}%)</small>` : ''}</span></div>`);
      if (md.previous_close) cells.push(`<div class="market-cell"><span class="label">Prev Close</span><span class="value">$${fmt(md.previous_close)}</span></div>`);
      if (md.day_low && md.day_high) cells.push(`<div class="market-cell"><span class="label">Day Range</span><span class="value">$${fmt(md.day_low)} – $${fmt(md.day_high)}</span></div>`);
      if (md.week52_low && md.week52_high) cells.push(`<div class="market-cell"><span class="label">52w Range</span><span class="value">$${fmt(md.week52_low)} – $${fmt(md.week52_high)}</span></div>`);
      if (md.volume) cells.push(`<div class="market-cell"><span class="label">Volume</span><span class="value">${md.volume.toLocaleString()}</span></div>`);
      if (md.exchange) cells.push(`<div class="market-cell"><span class="label">Exchange</span><span class="value">${escapeHtml(md.exchange)}</span></div>`);

      // Fundamentals (only present when FINNHUB_API_KEY is configured)
      if (md.market_cap != null) {
        const mcap = md.market_cap; // in millions USD
        const mcapStr = mcap >= 1000 ? `$${fmt(mcap/1000, 2)}B` : `$${fmt(mcap, 0)}M`;
        cells.push(`<div class="market-cell"><span class="label">Market Cap</span><span class="value">${mcapStr}</span></div>`);
      }
      if (md.pe_ratio != null) cells.push(`<div class="market-cell"><span class="label">P/E (TTM)</span><span class="value">${fmt(md.pe_ratio, 2)}</span></div>`);
      if (md.eps != null) cells.push(`<div class="market-cell"><span class="label">EPS (TTM)</span><span class="value">${fmt(md.eps, 2)}</span></div>`);
      if (md.dividend_yield != null) cells.push(`<div class="market-cell"><span class="label">Div Yield</span><span class="value">${fmt(md.dividend_yield * 100, 2)}%</span></div>`);
      if (md.beta != null) cells.push(`<div class="market-cell"><span class="label">Beta</span><span class="value">${fmt(md.beta, 2)}</span></div>`);
      if (md.industry) cells.push(`<div class="market-cell"><span class="label">Industry</span><span class="value">${escapeHtml(md.industry)}</span></div>`);
      if (md.next_earnings_date) cells.push(`<div class="market-cell"><span class="label">Next Earnings</span><span class="value">${escapeHtml(md.next_earnings_date)}</span></div>`);
      if (md.prev_earnings_date) cells.push(`<div class="market-cell"><span class="label">Prev Earnings</span><span class="value">${escapeHtml(md.prev_earnings_date)}</span></div>`);

      marketHtml = `<div class="market-row">${cells.join('')}</div>`;
    }

    const sym = t.ticker;
    const chartLinks = `<div class="chart-links">
      <a href="https://robinhood.com/stocks/${sym}" target="_blank" rel="noopener">Robinhood</a>
      <a href="https://finance.yahoo.com/quote/${sym}" target="_blank" rel="noopener">Yahoo Finance</a>
      <a href="https://www.tradingview.com/symbols/${sym}/" target="_blank" rel="noopener">TradingView</a>
      <a href="https://stocktwits.com/symbol/${sym}" target="_blank" rel="noopener">StockTwits</a>
      <a href="https://www.google.com/finance/quote/${sym}:NASDAQ" target="_blank" rel="noopener">Google Finance</a>
    </div>`;

    const buzzStats = `<div class="buzz-stats">
      <span><b>${t.distinct_posts}</b> posts</span>
      <span><b>${t.distinct_channels}</b> channels</span>
      <span><b>${t.distinct_sources}</b> source${t.distinct_sources === 1 ? '' : 's'}</span>
      ${(t.bullish_count || t.bearish_count) ? `<span><span class="bull">▲ ${t.bullish_count}</span> / <span class="bear">▼ ${t.bearish_count}</span></span>` : ''}
      ${t.total_followers ? `<span><b>${t.total_followers.toLocaleString()}</b> follower-reach</span>` : ''}
      <span>buzz score: <b>${t.score}</b></span>
    </div>`;

    // Stock Metrics block (always shown if we have any fundamentals).
    // Two columns: "Plain English" (AI explanation, on-demand) + "Numbers".
    const metricsHtml = renderMetricsBlockAt(t);

    // Earnings block (only shown when we have earnings data for this ticker)
    let earningsHtml = '';
    if (t.earnings) {
      const e = t.earnings;
      const surpriseHtml = e.last_surprise
        ? `<span class="surprise ${e.last_surprise}">${e.last_surprise}${e.last_surprise_pct != null ? ' ' + (e.last_surprise_pct > 0 ? '+' : '') + e.last_surprise_pct.toFixed(1) + '%' : ''}</span>`
        : '';
      const lastEpsHtml = e.last_actual_eps != null
        ? `<div class="market-cell"><span class="label">Last EPS</span><span class="value">$${e.last_actual_eps.toFixed(2)}${e.last_estimate_eps != null ? ` <small>(est. $${e.last_estimate_eps.toFixed(2)})</small>` : ''} ${surpriseHtml}</span></div>`
        : '';
      const nextEpsHtml = e.next_estimate_eps != null
        ? `<div class="market-cell"><span class="label">Next EPS estimate</span><span class="value">$${e.next_estimate_eps.toFixed(2)}</span></div>`
        : '';
      const nextDateHtml = e.next_date
        ? `<div class="market-cell"><span class="label">Next Earnings</span><span class="value">${escapeHtml(e.next_date)}</span></div>`
        : '';
      const lastPeriodHtml = e.last_period
        ? `<div class="market-cell"><span class="label">Last Period</span><span class="value">${escapeHtml(e.last_period)}</span></div>`
        : '';

      const newsLinks = (e.news || []).slice(0, 4).map(n =>
        `<a href="${escapeHtml(n.url)}" target="_blank" rel="noopener" class="${n.is_trusted ? 'trusted' : ''}" title="${escapeHtml(n.headline)}">${escapeHtml(n.source)}</a>`
      ).join('');
      const secLink = e.sec_edgar_url
        ? `<a href="${escapeHtml(e.sec_edgar_url)}" target="_blank" rel="noopener" class="trusted" title="SEC EDGAR — official 10-K/10-Q filings">SEC Filings</a>`
        : '';

      const cells = [nextDateHtml, nextEpsHtml, lastPeriodHtml, lastEpsHtml].filter(Boolean).join('');
      if (cells || secLink || newsLinks) {
        earningsHtml = `<div class="market-row" style="margin-top: 12px;">${cells}</div>
          <div class="chart-links">${secLink}${newsLinks}</div>`;
      }
    }

    // Summary block: always rendered with a generate/regenerate action button
    // at the top. Body is the existing tabs (when summary exists) or an
    // empty-state prompt (when not).
    function renderSummaryBody(summary) {
      if (!summary) {
        return `<div class="summary-missing">No AI summary yet for this ticker. Click "Generate AI Summary" above to create one.</div>`;
      }
      return `<div class="summary-tabs">
        <div class="tab-buttons">
          <button class="tab-button active" data-tab="eli15">ELI15</button>
          <button class="tab-button" data-tab="standard">Standard</button>
        </div>
        <div class="tab-content active" data-tab="eli15">
          <div class="summary-section">
            <h4>What's going on (plain English)</h4>
            <p>${escapeHtml(summary.eli15)}</p>
          </div>
          <div class="bull-bear-grid">
            <div class="case-card bull"><h4>▲ Bull Case</h4><div class="case">${escapeHtml(summary.bull_case)}</div></div>
            <div class="case-card bear"><h4>▼ Bear Case</h4><div class="case">${escapeHtml(summary.bear_case)}</div></div>
          </div>
        </div>
        <div class="tab-content" data-tab="standard">
          <div class="summary-section">
            <h4>Summary</h4>
            <p>${escapeHtml(summary.standard)}</p>
          </div>
          <div class="bull-bear-grid">
            <div class="case-card bull"><h4>▲ Bull Case</h4><div class="case">${escapeHtml(summary.bull_case)}</div></div>
            <div class="case-card bear"><h4>▼ Bear Case</h4><div class="case">${escapeHtml(summary.bear_case)}</div></div>
          </div>
        </div>
      </div>`;
    }

    const summaryActionLabel = s ? '↻ Regenerate Summary' : '✨ Generate AI Summary';
    const summaryHtml = `<div class="summary-block" data-summary-ticker="${t.ticker}">
      <div class="summary-actions">
        <button class="summary-gen-btn" data-summary-action="generate"
                title="${s ? 'Regenerate using the current model selection.' : 'Run AI summarization for this ticker on demand.'}">
          ${summaryActionLabel}
        </button>
        <span class="summary-status" data-summary-status></span>
      </div>
      <div class="summary-body" data-summary-body>
        ${renderSummaryBody(s)}
      </div>
    </div>`;

    let postsHtml = '';
    if (t.sample_posts && t.sample_posts.length) {
      // Label sources for the user — internal source IDs are not friendly.
      const SOURCE_LABEL = {
        stocktwits: 'StockTwits',
        reddit_aggregate: 'Reddit (Apewisdom)',
        hackernews: 'Hacker News',
      };
      const items = t.sample_posts.map(p => {
        const sentimentPill = p.sentiment === 'Bullish'
          ? '<span class="pill bull">bullish</span>'
          : (p.sentiment === 'Bearish' ? '<span class="pill bear">bearish</span>' : '');
        const srcLabel = SOURCE_LABEL[p.source] || p.source;
        // Channel formatting differs by source: subreddit → r/, StockTwits → $.
        let chanLabel = '';
        if (p.channel) {
          if (p.source === 'reddit_aggregate') chanLabel = ' · r/' + p.channel;
          else if (p.source === 'stocktwits') chanLabel = ' · $' + p.channel;
          else if (p.source === 'hackernews') chanLabel = '';  // channel is just "HN", redundant
          else chanLabel = ' · ' + escapeHtml(p.channel);
        }
        const sourceCh = `${escapeHtml(srcLabel)}${chanLabel}`;
        // Stats label varies by source:
        //   stocktwits: followers
        //   reddit_aggregate: mentions (stored in num_comments)
        //   hackernews: points + comments
        let stats = '';
        if (p.source === 'stocktwits') {
          stats = p.followers ? `${p.followers.toLocaleString()} followers` : '';
        } else if (p.source === 'reddit_aggregate') {
          stats = p.num_comments ? `${p.num_comments.toLocaleString()} mentions` : '';
        } else if (p.source === 'hackernews') {
          const parts = [];
          if (p.score) parts.push(`${p.score} ↑`);
          if (p.num_comments) parts.push(`${p.num_comments} 💬`);
          stats = parts.join(' · ');
        } else {
          // Fallback: best-effort generic display
          const parts = [];
          if (p.score) parts.push(`${p.score} score`);
          if (p.num_comments) parts.push(`${p.num_comments} comments`);
          stats = parts.join(' · ');
        }
        const link = p.url
          ? `<a href="${escapeHtml(p.url)}" target="_blank" rel="noopener">${escapeHtml(p.text)}</a>`
          : `<span>${escapeHtml(p.text)}</span>`;
        return `<li>${link}<div class="post-meta"><span class="pill">${sourceCh}</span>${sentimentPill}${stats ? ' ' + stats : ''}</div></li>`;
      }).join('');
      postsHtml = `<details class="posts-details" open><summary>Top posts (${t.sample_posts.length})</summary><ul class="post-list">${items}</ul></details>`;
    }

    const trendingPill = t.trending_rank ? `<span class="pill trending">trending #${t.trending_rank}</span>` : '';
    const sectorPill = t.sector ? `<span class="pill">${escapeHtml(t.sector)}</span>` : '';
    const rankPill = `<span class="pill">rank #${t.global_rank}</span>`;

    modal.innerHTML = `
      <div class="modal-header">
        <div class="title-block">
          <h2 id="modal-title">$${sym}${md && md.long_name ? ` <small>${escapeHtml(md.long_name)}</small>` : ''}</h2>
          <div class="modal-meta">${rankPill}${sectorPill}${trendingPill}</div>
        </div>
        <button class="modal-close" id="modal-close" aria-label="Close">×</button>
      </div>
      <div class="modal-body">
        ${marketHtml}
        ${chartLinks}
        ${metricsHtml}
        ${earningsHtml}
        ${buzzStats}
        ${summaryHtml}
        ${postsHtml}
      </div>
    `;

    // wire up close + tabs
    modal.querySelector('#modal-close').addEventListener('click', closeModal);
    modal.querySelectorAll('.tab-button').forEach(btn => {
      btn.addEventListener('click', () => {
        const tab = btn.dataset.tab;
        modal.querySelectorAll('.tab-button').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
        modal.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('active', c.dataset.tab === tab));
      });
    });

    // Wire up the on-demand "Generate / Regenerate" summary button.
    const sumBtn = modal.querySelector('[data-summary-action="generate"]');
    if (sumBtn) {
      sumBtn.addEventListener('click', () => onDemandSummarize(t));
    }

    // Wire up the metrics-explainer button + per-metric expand toggles.
    wireMetricsHandlers(t);
  }

  function wireMetricsHandlers(t) {
    const block = modal.querySelector(`.metrics-block[data-metrics-ticker="${t.ticker}"]`);
    if (!block) return;
    const explainBtn = block.querySelector('[data-metrics-action="explain"]');
    if (explainBtn) {
      explainBtn.addEventListener('click', () => onDemandExplainMetrics(t));
    }
    // Click a metric row → reveal its plain-English explanation inline.
    block.querySelectorAll('.metrics-list li.has-explanation').forEach(li => {
      li.addEventListener('click', () => li.classList.toggle('expanded'));
    });
  }

  function renderMetricsBlockAt(t) {
    const m = t.metrics;
    if (!m || !m.values || Object.keys(m.values).length === 0) {
      return '';  // no fundamentals to show — silently omit the block
    }
    const exp = m.explanation;
    const overviewHtml = exp && exp.overview
      ? `<div class="metrics-overview">${escapeHtml(exp.overview)}</div>`
      : `<div class="metrics-overview empty">Click "Generate plain-English explanation" to have Claude explain these numbers as if you were 15.</div>`;

    // Build the Numbers column. Each row is clickable IF we have a per-metric
    // explanation for it (toggles inline expansion of that one sentence).
    const perMetric = (exp && exp.per_metric) || {};
    const valueColorClass = (key, raw) => {
      // For percent-y growth/return fields, color positive green / negative red.
      if (raw == null) return '';
      if (['return_13w', 'return_52w', 'revenue_growth_yoy', 'eps_growth_yoy'].includes(key)) {
        return raw > 0 ? 'up' : (raw < 0 ? 'down' : '');
      }
      return '';
    };
    const rows = Object.keys(m.values).map(key => {
      const label = m.labels[key] || key;
      const value = m.values[key];
      const raw = m.raw ? m.raw[key] : null;
      const explanation = perMetric[key];
      const klass = valueColorClass(key, raw);
      const expandHint = explanation ? '<span class="toggle-hint">(click)</span>' : '';
      const expandText = explanation
        ? `<div class="per-metric-text">${escapeHtml(explanation)}</div>`
        : '';
      return `<li class="${explanation ? 'has-explanation' : ''}">
        <span class="label">${escapeHtml(label)}${expandHint}</span>
        <span class="value ${klass}">${escapeHtml(value)}</span>
        ${expandText}
      </li>`;
    }).join('');

    const actionLabel = exp ? '↻ Regenerate explanation' : '✨ Generate plain-English explanation';
    const numMetrics = Object.keys(m.values).length;
    const hint = exp
      ? '— click to view explanation + numbers'
      : `— ${numMetrics} fundamentals + on-demand AI explainer`;

    // Use <details> so it's collapsible by default. We open it only when an
    // explanation is already cached — otherwise the empty-state isn't worth
    // the screen space until the user explicitly asks for it.
    const openAttr = exp ? 'open' : '';
    return `<details class="metrics-block" data-metrics-ticker="${t.ticker}" ${openAttr}>
      <summary>
        <h4>📊 Stock Metrics</h4>
        <span class="metrics-summary-hint">${hint}</span>
      </summary>
      <div class="metrics-content">
        <div class="metrics-block-header">
          <button class="summary-gen-btn" data-metrics-action="explain"
                  title="${exp ? 'Regenerate the plain-English explanation.' : 'Have Claude explain each metric as if you were 15.'}">
            ${actionLabel}
          </button>
          <span class="summary-status" data-metrics-status></span>
        </div>
        <div class="metrics-grid">
          <div class="metrics-pane" data-metrics-pane="overview">
            <h5>Plain English</h5>
            ${overviewHtml}
          </div>
          <div class="metrics-pane">
            <h5>Numbers</h5>
            <ul class="metrics-list">${rows}</ul>
          </div>
        </div>
      </div>
    </details>`;
  }

  async function onDemandExplainMetrics(t) {
    const block = modal.querySelector(`.metrics-block[data-metrics-ticker="${t.ticker}"]`);
    if (!block) return;
    const btn = block.querySelector('[data-metrics-action="explain"]');
    const status = block.querySelector('[data-metrics-status]');
    if (!btn || !status) return;

    if (location.protocol === 'file:') {
      status.className = 'summary-status err';
      status.textContent = 'Server required.';
      return;
    }

    const picker = document.getElementById('model-picker');
    const choice = picker ? picker.value : 'auto';
    const force_refresh = !!(t.metrics && t.metrics.explanation);

    btn.disabled = true;
    const originalLabel = btn.textContent.trim();
    btn.textContent = '⏳ Generating…';
    status.className = 'summary-status';
    status.textContent = force_refresh ? 'Regenerating…' : 'Calling Claude (~15s)…';

    try {
      const resp = await fetch(`/metrics-explain/${encodeURIComponent(t.ticker)}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          model: choice === 'none' ? 'auto' : choice,
          force_refresh,
        }),
      });
      const data = await resp.json();
      if (!resp.ok || !data.overview) {
        const msg = data.error || `HTTP ${resp.status}`;
        status.className = 'summary-status err';
        status.textContent = `Failed: ${msg}`;
        btn.textContent = originalLabel;
        btn.disabled = false;
        return;
      }
      // Update in-memory tickerData and re-render the metrics block in place.
      if (!t.metrics) {
        t.metrics = {labels: data.metric_labels || {}, values: data.metric_values || {}, raw: {}};
      }
      t.metrics.explanation = {
        overview: data.overview,
        per_metric: data.per_metric || {},
        model: data.model,
      };
      block.outerHTML = renderMetricsBlockAt(t);
      // Re-wire handlers on the new block.
      wireMetricsHandlers(t);
    } catch (e) {
      status.className = 'summary-status err';
      status.textContent = `Network error: ${e.message}`;
      btn.textContent = originalLabel;
      btn.disabled = false;
    }
  }

  async function onDemandSummarize(t) {
    const block = modal.querySelector(`.summary-block[data-summary-ticker="${t.ticker}"]`);
    if (!block) return;
    const btn = block.querySelector('[data-summary-action="generate"]');
    const status = block.querySelector('[data-summary-status]');
    const bodyEl = block.querySelector('[data-summary-body]');
    if (!btn || !status || !bodyEl) return;

    if (location.protocol === 'file:') {
      status.className = 'summary-status err';
      status.textContent = 'Server required — open via http://localhost:8765/.';
      return;
    }

    // Pick up the user's model preference from the toolbar dropdown so the
    // on-demand summary uses the same model they'd get from a Refresh.
    const picker = document.getElementById('model-picker');
    const choice = picker ? picker.value : 'auto';
    const force_refresh = !!t.summary;  // if a summary already exists, button is "Regenerate"

    btn.disabled = true;
    const originalLabel = btn.textContent.trim();
    btn.textContent = '⏳ Generating…';
    status.className = 'summary-status';
    status.textContent = force_refresh ? 'Regenerating…' : 'Calling Claude…';

    try {
      const resp = await fetch(`/summarize/${encodeURIComponent(t.ticker)}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          model: choice === 'none' ? 'auto' : choice,
          force_refresh,
        }),
      });
      const data = await resp.json();
      if (!resp.ok || !data.summary) {
        const msg = data.error || `HTTP ${resp.status}`;
        status.className = 'summary-status err';
        status.textContent = `Failed: ${msg}`;
        btn.textContent = originalLabel;
        btn.disabled = false;
        return;
      }
      // Update the in-memory tickerData so reopens see the new summary.
      t.summary = data.summary;
      // Re-render only the summary body so we keep the button row.
      bodyEl.innerHTML = renderSummaryBodyAt(data.summary);
      // Re-wire tab buttons (the new HTML has them).
      bodyEl.querySelectorAll('.tab-button').forEach(b => {
        b.addEventListener('click', () => {
          const tab = b.dataset.tab;
          bodyEl.querySelectorAll('.tab-button').forEach(x => x.classList.toggle('active', x.dataset.tab === tab));
          bodyEl.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('active', c.dataset.tab === tab));
        });
      });
      // Update button label to "Regenerate" for next click.
      btn.textContent = '↻ Regenerate Summary';
      btn.disabled = false;
      status.className = 'summary-status ok';
      const elapsed = data.elapsed_ms ? ` in ${(data.elapsed_ms / 1000).toFixed(1)}s` : '';
      const cacheNote = data.cached ? ' (cached)' : '';
      const modelShort = (data.model || '').split('.').pop() || '';
      status.textContent = `Done${elapsed}${cacheNote} · ${modelShort}`;
      // Auto-clear the status after a few seconds so it doesn't linger.
      setTimeout(() => { if (status.textContent.startsWith('Done')) status.textContent = ''; }, 6000);
    } catch (e) {
      status.className = 'summary-status err';
      status.textContent = `Network error: ${e.message}`;
      btn.textContent = originalLabel;
      btn.disabled = false;
    }
  }

  // Standalone version of renderSummaryBody for use after on-demand updates.
  // The original lives inside renderModal as a closure; this one mirrors it
  // since after-update calls happen outside that scope.
  function renderSummaryBodyAt(summary) {
    if (!summary) {
      return `<div class="summary-missing">No AI summary yet for this ticker. Click "Generate AI Summary" above to create one.</div>`;
    }
    return `<div class="summary-tabs">
      <div class="tab-buttons">
        <button class="tab-button active" data-tab="eli15">ELI15</button>
        <button class="tab-button" data-tab="standard">Standard</button>
      </div>
      <div class="tab-content active" data-tab="eli15">
        <div class="summary-section">
          <h4>What's going on (plain English)</h4>
          <p>${escapeHtml(summary.eli15)}</p>
        </div>
        <div class="bull-bear-grid">
          <div class="case-card bull"><h4>▲ Bull Case</h4><div class="case">${escapeHtml(summary.bull_case)}</div></div>
          <div class="case-card bear"><h4>▼ Bear Case</h4><div class="case">${escapeHtml(summary.bear_case)}</div></div>
        </div>
      </div>
      <div class="tab-content" data-tab="standard">
        <div class="summary-section">
          <h4>Summary</h4>
          <p>${escapeHtml(summary.standard)}</p>
        </div>
        <div class="bull-bear-grid">
          <div class="case-card bull"><h4>▲ Bull Case</h4><div class="case">${escapeHtml(summary.bull_case)}</div></div>
          <div class="case-card bear"><h4>▼ Bear Case</h4><div class="case">${escapeHtml(summary.bear_case)}</div></div>
        </div>
      </div>
    </div>`;
  }

  function openModal(ticker) {
    const t = tickerData[ticker];
    if (!t) return;
    renderModal(t);
    backdrop.classList.add('open');
    document.body.style.overflow = 'hidden';
  }
  function closeModal() {
    backdrop.classList.remove('open');
    document.body.style.overflow = '';
  }

  // backdrop click + ESC + open triggers
  backdrop.addEventListener('click', e => { if (e.target === backdrop) closeModal(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

  document.querySelectorAll('[data-ticker]').forEach(el => {
    el.addEventListener('click', (e) => {
      // Don't open the modal if the click landed on the star button —
      // favoriting shouldn't side-effect into "also open the modal".
      if (e.target.closest('[data-fav-toggle]')) return;
      openModal(el.dataset.ticker);
    });
  });

  // Sector collapse
  document.querySelectorAll('.sector-header').forEach(h => {
    h.addEventListener('click', () => h.parentElement.classList.toggle('collapsed'));
  });

  // Sector jump
  document.getElementById('sector-jump').addEventListener('change', e => {
    if (e.target.value) {
      const el = document.getElementById(e.target.value);
      if (el) el.scrollIntoView({behavior: 'smooth', block: 'start'});
      e.target.value = '';
    }
  });

  // Search filter
  const searchBox = document.getElementById('search-box');
  searchBox.addEventListener('input', () => {
    const q = searchBox.value.trim().toLowerCase();
    document.querySelectorAll('.sector-card').forEach(card => {
      let visible = 0;
      card.querySelectorAll('.ticker-row').forEach(row => {
        const ticker = (row.dataset.ticker || '').toLowerCase();
        const name = (row.dataset.name || '').toLowerCase();
        const match = !q || ticker.includes(q) || name.includes(q);
        row.style.display = match ? '' : 'none';
        if (match) visible++;
      });
      card.style.display = (q && visible === 0) ? 'none' : '';
    });
  });
  // "/" focuses search
  document.addEventListener('keydown', e => {
    if (e.key === '/' && document.activeElement !== searchBox) {
      e.preventDefault();
      searchBox.focus();
    }
  });

  // ==========================================================================
  // Server controls — Stop / Restart menu, and auto-reconnect overlay
  // ==========================================================================
  const menuBtn = document.getElementById('server-menu-btn');
  const menu = document.getElementById('server-menu');
  const overlay = document.getElementById('server-down-overlay');
  const reconnectStatus = document.getElementById('reconnect-status');

  function toggleMenu(show) {
    const open = show !== undefined ? show : !menu.classList.contains('open');
    menu.classList.toggle('open', open);
    menuBtn.setAttribute('aria-expanded', String(open));
    if (open) positionMenu();
  }
  function positionMenu() {
    // Anchor the dropdown to the trigger button, but clamp inside the viewport.
    // Reserve 12px of breathing room from each edge, plus account for the
    // 28px sidebar tabs on the right.
    const btnRect = menuBtn.getBoundingClientRect();
    const viewportW = window.innerWidth;
    const viewportH = window.innerHeight;
    const reservedRight = 28 + 12;  // sidebar strip + buffer
    const reservedLeft = 12;
    const reservedBottom = 12;

    // Menu is `display: block` (via .open class) so we can measure it.
    const menuRect = menu.getBoundingClientRect();
    const menuW = menuRect.width;
    const menuH = menuRect.height;

    // Default: align right edge of menu to right edge of button.
    let left = btnRect.right - menuW;
    // Clamp to viewport.
    const maxLeft = viewportW - menuW - reservedRight;
    if (left > maxLeft) left = maxLeft;
    if (left < reservedLeft) left = reservedLeft;

    // Default: place below the button. Flip above if it would overflow.
    let top = btnRect.bottom + 4;
    if (top + menuH > viewportH - reservedBottom) {
      top = btnRect.top - menuH - 4;
      if (top < 12) top = 12;
    }

    menu.style.left = left + 'px';
    menu.style.top = top + 'px';
  }
  // Reposition if the window resizes / scrolls while open.
  window.addEventListener('resize', () => { if (menu.classList.contains('open')) positionMenu(); });
  window.addEventListener('scroll', () => { if (menu.classList.contains('open')) positionMenu(); }, {passive: true});
  menuBtn.addEventListener('click', e => {
    e.stopPropagation();
    toggleMenu();
  });
  document.addEventListener('click', e => {
    if (!menu.contains(e.target) && e.target !== menuBtn) toggleMenu(false);
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') toggleMenu(false); });

  document.getElementById('menu-restart').addEventListener('click', async () => {
    toggleMenu(false);
    if (!confirm('Restart the dashboard server? In-flight refreshes must finish first.')) return;
    try {
      const resp = await fetch('/restart', {method: 'POST'});
      const data = await resp.json();
      if (!data.ok) {
        alert(data.error || 'Could not restart.');
        return;
      }
      // Server is restarting — show the overlay and start the reconnect loop.
      showServerDown('Server is restarting — this usually takes about 2 seconds…');
      startReconnectPolling({reloadOnReconnect: true});
    } catch (e) {
      // The connection drops before the response arrives ~half the time on
      // restart. That's fine — just go straight to the reconnect overlay.
      showServerDown('Server is restarting — this usually takes about 2 seconds…');
      startReconnectPolling({reloadOnReconnect: true});
    }
  });

  document.getElementById('menu-shutdown').addEventListener('click', async () => {
    toggleMenu(false);
    if (!confirm('Stop the dashboard server? You will need to run "stock-buzz-server" in a terminal to restart it.')) return;
    try {
      const resp = await fetch('/shutdown', {method: 'POST'});
      const data = await resp.json();
      if (!data.ok) {
        alert(data.error || 'Could not stop server.');
        return;
      }
    } catch (e) {
      // Fine — the server may have already gone down before responding.
    }
    showServerDown('Server stopped.');
    startReconnectPolling({reloadOnReconnect: true});
  });

  function showServerDown(msg) {
    if (msg) reconnectStatus.textContent = msg;
    overlay.classList.add('visible');
  }
  function hideServerDown() {
    overlay.classList.remove('visible');
  }

  let reconnectTimer = null;
  function startReconnectPolling({reloadOnReconnect = false} = {}) {
    if (reconnectTimer) return;
    let attempts = 0;
    reconnectTimer = setInterval(async () => {
      attempts++;
      reconnectStatus.textContent = `Trying to reconnect (attempt ${attempts})…`;
      try {
        const r = await fetch('/status', {cache: 'no-store'});
        if (r.ok) {
          clearInterval(reconnectTimer);
          reconnectTimer = null;
          reconnectStatus.textContent = 'Reconnected.';
          if (reloadOnReconnect) {
            setTimeout(() => location.reload(), 400);
          } else {
            setTimeout(hideServerDown, 600);
          }
        }
      } catch (e) {
        // keep trying
      }
    }, 1000);
  }

  // Detect server going away during normal use: if any /status fetch fails
  // a couple times in a row, surface the overlay.
  let consecutiveFailures = 0;
  setInterval(async () => {
    try {
      const r = await fetch('/status', {cache: 'no-store'});
      if (r.ok) {
        consecutiveFailures = 0;
        return;
      }
      consecutiveFailures++;
    } catch (e) {
      consecutiveFailures++;
    }
    if (consecutiveFailures >= 3 && !overlay.classList.contains('visible')) {
      showServerDown('Lost connection to server.');
      startReconnectPolling({reloadOnReconnect: true});
    }
  }, 4000);

  // ==========================================================================
  // Refresh — non-blocking background pipeline run.
  // The page polls /status while a run is in flight. When the server reports
  // last_finished_at > pageLoadedAt, we reload to pick up the new HTML.
  // If the server isn't running (page opened directly off the filesystem),
  // the refresh button degrades gracefully into a "open via server" hint.
  // ==========================================================================
  const refreshBtn = document.getElementById('refresh-btn');
  const banner = document.getElementById('refresh-banner');
  const bannerPhase = document.getElementById('refresh-phase');
  const bannerDetail = document.getElementById('refresh-detail');
  const bannerPct = document.getElementById('refresh-pct');
  const bannerBar = document.getElementById('refresh-progress-bar');

  // Page-load reference timestamp for detecting "newer-than-this-page" finish.
  const pageLoadedAt = Date.now() / 1000;
  let pollTimer = null;
  let serverAvailable = (location.protocol === 'http:' || location.protocol === 'https:');

  // Restore picker selection from localStorage and persist on change.
  // Default for first-time visitors: Haiku 4.5 (fastest + cheapest model that
  // still produces decent summaries). Power users can switch via the dropdown
  // and that choice persists across reloads.
  const DEFAULT_MODEL = 'claude-haiku-4-5';
  const picker = document.getElementById('model-picker');
  if (picker) {
    const saved = localStorage.getItem('stockBuzzModel');
    let target = saved || DEFAULT_MODEL;
    const opt = Array.from(picker.options).find(o => o.value === target);
    if (opt) {
      picker.value = target;
    }
    // If we just applied the default for a first-time user, persist it so
    // the choice is consistent across pages (e.g. /summarize uses it too).
    if (!saved) localStorage.setItem('stockBuzzModel', picker.value);
    picker.addEventListener('change', () => {
      localStorage.setItem('stockBuzzModel', picker.value);
    });
  }

  if (!serverAvailable) {
    // Opened as file:// — server can't be contacted. Repurpose the button.
    refreshBtn.title = 'Refresh requires the server. Run: python -m src.server';
    refreshBtn.addEventListener('click', () => {
      alert(
        'Refresh requires the local server.\\n\\n' +
        'Stop reading this dashboard from a file path, and instead start the server:\\n\\n' +
        '  python -m src.server\\n\\n' +
        'Then visit http://localhost:8765/'
      );
    });
  } else {
    refreshBtn.addEventListener('click', startRefresh);

    // If the server is in the middle of a refresh when the page loads, jump
    // straight into polling so the user sees the in-progress banner.
    fetch('/status', {cache: 'no-store'}).then(r => r.json()).then(s => {
      if (s.running) {
        showBanner(s);
        setRunning(true);
        startPolling();
      }
    }).catch(() => {});

    // Show what provider/model the refresh button will use, so cost is never
    // a surprise. Hidden by default; only shown if /config responds.
    fetch('/config', {cache: 'no-store'}).then(r => r.json()).then(c => {
      const pill = document.getElementById('provider-pill');
      if (!pill) return;
      // Display short model names (claude-sonnet-4-6 → sonnet-4-6).
      const shortModel = (c.model || '').replace(/^.*claude-/, '').replace(/-\d{8}.*$/, '');
      const providerLabel = c.provider === 'bedrock' ? 'Bedrock' :
                            c.provider === 'anthropic' ? 'Anthropic' :
                            c.provider === 'none' ? 'No AI' : c.provider;
      const finnhubBadge = c.finnhub_configured ? ' · 📊 Fundamentals' : '';
      pill.textContent = `${providerLabel} · ${shortModel || c.model || ''}${finnhubBadge}`;
      pill.title = `Refresh will use:\\nProvider: ${c.provider}\\nModel: ${c.model}` +
                   (c.aws_profile ? `\\nAWS profile: ${c.aws_profile}\\nRegion: ${c.aws_region}` : '') +
                   `\\nFinnhub fundamentals: ${c.finnhub_configured ? 'enabled' : 'disabled (set FINNHUB_API_KEY in .env)'}`;
      pill.style.display = '';
    }).catch(() => {});
  }

  async function startRefresh() {
    setRunning(true);
    showBanner({phase: 'starting', detail: 'Starting…', fraction: 0.01});
    const picker = document.getElementById('model-picker');
    const choice = picker ? picker.value : 'auto';
    // "none" → tell server to skip AI; otherwise pass model name as override.
    const body = (choice === 'none')
      ? {provider: 'none'}
      : {model: choice};
    // Always send favorites so the server can guarantee AI summaries for them
    // even if they're not in the top-N by buzz.
    body.favorites = Array.from(favorites);
    try {
      const resp = await fetch('/refresh', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      });
      const data = await resp.json();
      if (resp.status === 400) {
        bannerError('Bad request: ' + (data.error || 'unknown'));
        return;
      }
      // started=false means another run was already in flight — that's fine,
      // we still want to poll.
      showBanner(data);
      startPolling();
    } catch (e) {
      bannerError('Could not reach server: ' + e.message);
    }
  }

  function setRunning(running) {
    refreshBtn.disabled = running;
    refreshBtn.classList.toggle('running', running);
    refreshBtn.querySelector('.label').textContent = running ? 'Refreshing…' : 'Refresh';
  }

  function showBanner(s) {
    banner.classList.add('visible');
    banner.classList.remove('error');
    bannerPhase.textContent = (s.phase || 'refreshing').replace(/_/g, ' ');
    bannerDetail.textContent = s.detail || '';
    const pct = Math.round(((s.fraction || 0) * 100));
    bannerPct.textContent = pct + '%';
    bannerBar.style.width = Math.max(2, pct) + '%';
  }

  function bannerError(msg) {
    banner.classList.add('visible', 'error');
    bannerPhase.textContent = 'error';
    bannerDetail.textContent = msg;
    bannerPct.textContent = '!';
    bannerBar.style.width = '100%';
    setRunning(false);
    if (pollTimer) clearInterval(pollTimer);
  }

  function startPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      try {
        const resp = await fetch('/status', {cache: 'no-store'});
        const s = await resp.json();
        showBanner(s);
        if (!s.running) {
          clearInterval(pollTimer);
          pollTimer = null;
          if (s.phase === 'error') {
            bannerError(s.detail || 'Refresh failed.');
            return;
          }
          // Pipeline finished. If it finished after this page loaded, reload
          // to pick up the new HTML; otherwise just hide the banner.
          if (s.last_finished_at && s.last_finished_at > pageLoadedAt) {
            bannerDetail.textContent = 'Refresh complete — reloading…';
            setTimeout(() => location.reload(), 600);
          } else {
            setRunning(false);
            setTimeout(() => banner.classList.remove('visible'), 1500);
          }
        }
      } catch (e) {
        bannerError('Lost connection to server.');
      }
    }, 1000);
  }
})();
</script>

</body>
</html>
"""


def _entry_to_json(entry: dict, sector_name: str) -> dict:
    """Compact a per-ticker entry for the in-page JSON blob."""
    t: TickerBuzz = entry["buzz"]
    md: Optional[MarketData] = entry["market"]
    s: Optional[Summary] = entry["summary"]
    ed: Optional[EarningsData] = entry.get("earnings")

    market_d = None
    if md:
        market_d = {
            "price": md.price,
            "previous_close": md.previous_close,
            "day_high": md.day_high,
            "day_low": md.day_low,
            "week52_high": md.week52_high,
            "week52_low": md.week52_low,
            "volume": md.volume,
            "currency": md.currency,
            "exchange": md.exchange,
            "long_name": md.long_name,
            "percent_change": md.percent_change,
            "market_cap": md.market_cap,
            "pe_ratio": md.pe_ratio,
            "eps": md.eps,
            "dividend_yield": md.dividend_yield,
            "beta": md.beta,
            "industry": md.industry,
            "next_earnings_date": md.next_earnings_date,
            "prev_earnings_date": md.prev_earnings_date,
        }
    summary_d = None
    if s:
        summary_d = {
            "eli15": s.eli15,
            "standard": s.standard,
            "bull_case": s.bull_case,
            "bear_case": s.bear_case,
        }
    # Stock-metrics block: always include the {label, value} list so the modal
    # can render the "Numbers" section even without an AI explanation. Then
    # ALSO include the explanation if one is already cached (fresh enough to
    # show immediately on modal open).
    metrics_block = None
    if md:
        metric_values = _collect_metric_values(md)
        if metric_values:
            cached_exp = _get_cached_metrics_explanation(t.ticker, _metrics_fingerprint(md))
            metrics_block = {
                "labels": {k: lbl for k, (lbl, _) in metric_values.items()},
                "values": {k: val for k, (_, val) in metric_values.items()},
                "raw": {k: getattr(md, k, None) for k in metric_values.keys()},
                "explanation": (
                    {
                        "overview": cached_exp.overview,
                        "per_metric": cached_exp.per_metric,
                        "model": cached_exp.model,
                    } if cached_exp else None
                ),
            }

    earnings_d = None
    if ed:
        earnings_d = {
            "last_actual_eps": ed.last_actual_eps,
            "last_estimate_eps": ed.last_estimate_eps,
            "last_period": ed.last_period,
            "last_surprise": ed.last_surprise,
            "last_surprise_pct": ed.last_surprise_pct,
            "next_estimate_eps": ed.next_estimate_eps,
            "next_date": ed.next_date or (md.next_earnings_date if md else None),
            "sec_edgar_url": ed.sec_edgar_url,
            "news": [{"source": n.source, "headline": n.headline, "url": n.url,
                      "datetime": n.datetime, "is_trusted": n.is_trusted}
                     for n in (ed.news or [])],
        }
    return {
        "ticker": t.ticker,
        "score": t.score,
        "global_rank": entry["global_rank"],
        "sector": sector_name,
        "distinct_posts": t.distinct_posts,
        "distinct_channels": t.distinct_channels,
        "distinct_sources": t.distinct_sources,
        "total_upvotes": t.total_upvotes,
        "total_comments": t.total_comments,
        "total_followers": t.total_followers,
        "bullish_count": t.bullish_count,
        "bearish_count": t.bearish_count,
        "trending_rank": t.trending_rank,
        "sample_posts": t.sample_posts or [],
        "market": market_d,
        "summary": summary_d,
        "earnings": earnings_d,
        "metrics": metrics_block,
    }


def render_report(
    tickers: list[TickerBuzz],
    market_data: dict[str, MarketData],
    summaries: dict[str, Summary],
    total_posts: int,
    total_mentions: int,
    has_api_key: bool,
    earnings_data: Optional[dict[str, EarningsData]] = None,
    output_path: Path = REPORT_PATH,
    window_hours: int = BUZZ_WINDOW_HOURS,
    top_movers_n: int = 12,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Timestamps for the freshness indicator. We surface two:
    #   last_pull_utc   — when the most recent pipeline run completed
    #   prices_as_of_utc — oldest market_data.fetched_at among displayed tickers,
    #     since cached prices may be a few minutes older than the pipeline run.
    last_pull_utc = latest_run_finished_at()
    fetched_times = [md.fetched_at for md in market_data.values() if md and md.fetched_at]
    prices_as_of_utc = (
        datetime.fromtimestamp(min(fetched_times), tz=timezone.utc).isoformat()
        if fetched_times else None
    )

    # Stamp each entry with its global rank, build the sectorized view, AND
    # build the flat per-ticker JSON for client-side modal rendering.
    earnings_data = earnings_data or {}
    enriched = []
    for i, t in enumerate(tickers, 1):
        enriched.append({
            "buzz": t,
            "market": market_data.get(t.ticker),
            "summary": summaries.get(t.ticker),
            "earnings": earnings_data.get(t.ticker),
            "global_rank": i,
        })

    sectors = group_by_sector(enriched, key=lambda e: e["buzz"].ticker)

    ticker_data = {}
    for sector_name, items in sectors.items():
        for entry in items:
            ticker_data[entry["buzz"].ticker] = _entry_to_json(entry, sector_name)

    top_movers = enriched[:top_movers_n]

    html = Template(TEMPLATE).render(
        sectors=sectors,
        top_movers=top_movers,
        total_tickers=len(tickers),
        total_posts=total_posts,
        total_mentions=total_mentions,
        summarized_count=len(summaries),
        has_api_key=has_api_key,
        window_hours=window_hours,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        last_pull_utc=last_pull_utc,
        prices_as_of_utc=prices_as_of_utc,
        ticker_data_json=json.dumps(ticker_data, default=str),
    )
    output_path.write_text(html)
    return output_path
