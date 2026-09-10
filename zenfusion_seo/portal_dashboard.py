#!/usr/bin/env python3
"""
portal_dashboard.py — Generate a comprehensive client SEO dashboard HTML page.

Reads an inventory of Zenfusion audit HTML reports from a brand subdirectory
inside --audits-dir, extracts headline KPI metrics from key reports via regex,
and emits a single self-contained, noindex/nofollow HTML dashboard file
that works standalone on GitHub Pages.

Usage:
    python3 portal_dashboard.py --brand softpro \
        --audits-dir /tmp/zenfusion-audits \
        --output /tmp/zenfusion-audits/softpro/dashboard.html
"""

from __future__ import annotations

import argparse
import html as html_lib
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Classification rules — order matters; first match wins. Each rule is a
# (level, keyword_substrings) pair. The filename (lowercased, with hyphens
# and underscores treated as word separators) is tested against substrings.
# --------------------------------------------------------------------------- #

LEVEL_ORDER = ["domain", "page", "backlink", "market", "competitor", "keyword"]

LEVEL_META: Dict[str, Dict[str, str]] = {
    "domain":     {"title": "Domain-Level",     "desc": "Site-wide technical & visibility audits",          "icon": "🌐", "accent": "#0066cc"},
    "page":       {"title": "Page-Level",       "desc": "Per-URL audits — speed, content, on-page",          "icon": "📄", "accent": "#7c3aed"},
    "backlink":   {"title": "Backlink",         "desc": "Link profile, anchors, gaps, toxic-link reports",  "icon": "🔗", "accent": "#0891b2"},
    "market":     {"title": "Market",          "desc": "Demand trends, SERP landscape, seasonality",        "icon": "📊", "accent": "#059669"},
    "competitor": {"title": "Competitor",       "desc": "Competitor content, link velocity, page monitors", "icon": "⚔️", "accent": "#dc2626"},
    "keyword":    {"title": "Keyword",         "desc": "Rankings, difficulty, intent, cannibalization",   "icon": "🔑", "accent": "#d97706"},
}

# Each tuple: (level, list_of_filename_substrings). Evaluated top-to-bottom.
CLASSIFICATION_RULES: List[Tuple[str, List[str]]] = [
    # Competitor-first (more specific) before backlink/market — otherwise
    # "backlink-gap-vs-X" would land in backlink when it's really competitive.
    ("competitor", ["backlink-gap", "competitor-content", "competitor-page",
                    "competitive-analysis", "competitive-link", "share-of-voice",
                    "ig-analysis"]),
    ("market",     ["market-analysis", "seasonal-trends", "serp-feature-landscape",
                    "serp-feature-check", "market-"]),
    ("keyword",    ["keyword-dashboard", "keyword-difficulty", "keyword-rank",
                    "cannibalization", "content-plan", "content-decay",
                    "search-intent", "collection-opportunities"]),
    ("backlink",   ["backlink-analysis", "backlink-alerts", "backlink-gap",
                    "anchor-text", "lost-recovery", "toxic-link",
                    "page-backlink-audit", "competitive-link-velocity",
                    "referring-domains", "backlink"]),
    ("page",       ["page-audit", "page-speed", "instant-page-audit",
                    "local-seo-grid", "structured-data-audit"]),
    # Domain-level is the catch-all for site-wide reports
    ("domain",     ["site-audit", "seo-visibility", "portfolio-audit",
                    "domain-history", "ai-visibility", "reports.html",
                    "portal.html"]),
]

# Reports we explicitly pull KPIs from. (filename_pattern, kpi_group)
# filename_pattern is a regex matched against the lowercased filename.
KPI_REPORTS: List[Tuple[str, str]] = [
    (r"^site-audit-report\.html$",        "site_health"),
    (r"^seo-visibility-report\.html$",   "visibility"),
    (r"^keyword-rank-tracker\.html$",    "rankings"),
    (r"^keyword-dashboard\.html$",       "keyword_dash"),
    (r"^backlink-analysis-1\.html$",     "backlinks"),
    (r"^ai-visibility-report\.html$",    "ai_visibility"),
]

# Files we never surface as report cards (landing pages, the dashboard itself)
SKIP_FILES = {"index.html", "reports.html", "portal.html", "dashboard.html"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _slug_words(filename: str) -> str:
    """Lowercased, separator-normalized form of a filename for substring matching.
    The .html extension is stripped first so trailing tokens can match cleanly.
    """
    base = filename.lower()
    # Strip the extension first so trailing tokens can match cleanly.
    base = re.sub(r"\.(html?|htm)$", "", base)
    # Treat digit runs as word boundaries so "backlink-analysis-10-sheet10"
    # still matches "backlink-analysis" cleanly; digits are kept but isolated.
    base = re.sub(r"(\d+)", r" \1 ", base)
    base = base.replace("_", " ").replace("-", " ")
    # Collapse runs of whitespace so each token is space-padded uniformly.
    base = re.sub(r"\s+", " ", base).strip()
    return " " + base + " "


def classify_report(filename: str) -> str:
    """Return the primary level for a report filename."""
    slug = _slug_words(filename)
    # Re-create substring list with spaces so they don't accidentally
    # span word boundaries (e.g. "backlink" shouldn't match "backlink-gap").
    for level, substrings in CLASSIFICATION_RULES:
        for s in substrings:
            token = " " + s.replace("_", " ").replace("-", " ") + " "
            if token in slug:
                return level
    return "domain"  # safe default


def _git_last_commit_date(repo_dir: Path, rel_path: str) -> Optional[str]:
    """Return ISO date (YYYY-MM-DD) of the last git commit touching rel_path."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%cI", "--", rel_path],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=5,
        )
        out = result.stdout.strip()
        if out:
            # %cI is strict ISO 8601 — take just the date portion.
            return out[:10]
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def _extract_title(html_text: str) -> str:
    """Pull the contents of the first <title> tag."""
    m = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
    if m:
        return html_lib.unescape(m.group(1).strip())
    return "Untitled Report"


def _file_mtime_date(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")


def _human_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


# --------------------------------------------------------------------------- #
# 1. scan_available_reports
# --------------------------------------------------------------------------- #

def scan_available_reports(audits_dir: Path, brand: str) -> List[Dict]:
    """
    Walk the brand subdirectory of audits_dir, inventory every HTML report,
    classify it by level, and attach metadata (title, date, size).

    Returns a list of report dicts, sorted newest-first by date then name.
    """
    brand_dir = audits_dir / brand
    if not brand_dir.is_dir():
        # Fall back to scanning audits_dir directly if no brand subdir exists.
        brand_dir = audits_dir

    repo_root = audits_dir  # git repo root = the audits dir
    reports: List[Dict] = []

    for path in sorted(brand_dir.rglob("*.html")):
        rel = path.relative_to(brand_dir)
        if path.name in SKIP_FILES:
            continue

        level = classify_report(path.name)

        # Try to read just enough of the file for a title (first 8 KB).
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(8192)
        except OSError:
            continue

        title = _extract_title(head) or path.stem

        # Date: prefer git last-commit date, fall back to file mtime.
        git_rel = str(brand_dir.name / rel) if brand_dir != audits_dir else str(rel)
        date = _git_last_commit_date(repo_root, git_rel) or _file_mtime_date(path)

        # Relative link from the dashboard's own location (the brand dir).
        link = rel.as_posix()

        reports.append({
            "filename":    path.name,
            "title":        title,
            "level":        level,
            "date":         date,
            "size":         _human_size(path.stat().st_size),
            "link":         link,
            "key_metric":   "",  # filled in by extract_kpis for marquee reports
            "description":  LEVEL_META[level]["desc"],
        })

    # Sort newest first, then by title.
    reports.sort(key=lambda r: (r["date"], r["title"]), reverse=True)
    return reports


# --------------------------------------------------------------------------- #
# 2. extract_kpis
# --------------------------------------------------------------------------- #

def _read_report(path: Path, max_chars: int = 200_000) -> str:
    """Read up to max_chars of an HTML report for KPI extraction."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(max_chars)
    except OSError:
        return ""


def _extract_num_pairs(html_text: str) -> Dict[str, str]:
    """
    Pull every  <div class="num">VALUE</div><div class="label">LABEL</div>
    pair (the standard Zenfusion summary-card pattern) into a dict.
    Label is normalized to lowercase, value kept verbatim.
    """
    pattern = re.compile(
        r'class="num"[^>]*>\s*([^<]*?)\s*</div>\s*'
        r'<div class="label">\s*([^<]*?)\s*</div>',
        re.IGNORECASE,
    )
    pairs: Dict[str, str] = {}
    for m in pattern.finditer(html_text):
        val = html_lib.unescape(m.group(1).strip())
        label = html_lib.unescape(m.group(2).strip().lower())
        if label and label not in pairs:
            pairs[label] = val
    return pairs


def _extract_ai_visibility_rate(html_text: str) -> Optional[str]:
    """
    The AI Visibility report uses a Summary <table> rather than summary-cards.
    Find the Mentions row and extract the percentage.
    """
    m = re.search(
        r"<strong>\s*Mentions?\s*</strong>\s*</td>\s*<td>\s*(\d+)\s*\(([\d.]+)%\)",
        html_text, re.IGNORECASE,
    )
    if m:
        return f"{m.group(2)}%"
    # Fallback: any "X (Y.Y%)" mention pattern.
    m = re.search(r"Mentions?\s*\D+(\d+)\s*\(([\d.]+)%\)", html_text, re.IGNORECASE)
    return f"{m.group(2)}%" if m else None


def extract_kpis(reports: List[Dict], brand_dir: Path) -> Dict[str, Dict[str, str]]:
    """
    Walk the report inventory, open key reports, and pull headline metrics
    via regex. Returns a dict keyed by kpi_group with the extracted values.
    """
    kpis: Dict[str, Dict[str, str]] = {}

    # Build a quick lookup: regex pattern -> report dict (first match wins)
    by_name = {}
    for r in reports:
        for pattern, group in KPI_REPORTS:
            if re.search(pattern, r["filename"].lower()) and group not in by_name:
                by_name[group] = r

    # --- Site Health (from site-audit-report) ---
    if "site_health" in by_name:
        text = _read_report(brand_dir / by_name["site_health"]["link"])
        pairs = _extract_num_pairs(text)
        score = pairs.get("site health score", pairs.get("health score"))
        criticals = pairs.get("criticals")
        warnings = pairs.get("warnings")
        kpis["site_health"] = {
            "score":     score or "—",
            "criticals":  criticals or "—",
            "warnings":   warnings or "—",
            "source":     by_name["site_health"]["filename"],
        }

    # --- Visibility / Est. Traffic / Keywords Ranked / Backlinks / Referring Domains
    if "visibility" in by_name:
        text = _read_report(brand_dir / by_name["visibility"]["link"])
        pairs = _extract_num_pairs(text)
        kpis["visibility"] = {
            "est_traffic":       pairs.get("est. monthly traffic", pairs.get("est traffic", "—")),
            "keywords_ranked":    pairs.get("keywords ranked", pairs.get("keywords", "—")),
            "visibility_index":   pairs.get("visibility index", "—"),
            "avg_position":       pairs.get("avg position", pairs.get("average position", "—")),
            "page1_keywords":     pairs.get("page 1 keywords", "—"),
            "domain_rating":       pairs.get("domain rating", "—"),
            "total_backlinks":     pairs.get("total backlinks", "—"),
            "referring_domains":   pairs.get("referring domains", "—"),
            "source":              by_name["visibility"]["filename"],
        }

    # --- Rankings (from keyword-rank-tracker) ---
    if "rankings" in by_name:
        text = _read_report(brand_dir / by_name["rankings"]["link"])
        pairs = _extract_num_pairs(text)
        kpis["rankings"] = {
            "total_tracked":  pairs.get("total tracked", "—"),
            "ranking_top100": pairs.get("ranking (top 100)", pairs.get("ranking", "—")),
            "in_top10":       pairs.get("in top 10", "—"),
            "in_top3":        pairs.get("in top 3", "—"),
            "source":         by_name["rankings"]["filename"],
        }

    # --- Keyword dashboard (gap keyword count) ---
    if "keyword_dash" in by_name:
        text = _read_report(brand_dir / by_name["keyword_dash"]["link"])
        pairs = _extract_num_pairs(text)
        kpis["keyword_dash"] = {
            "gap_keywords":    pairs.get("gap keywords", "—"),
            "covered_keywords": pairs.get("covered keywords", "—"),
            "gap_volume":      pairs.get("gap monthly volume", "—"),
            "source":          by_name["keyword_dash"]["filename"],
        }

    # --- Backlinks (from first backlink-analysis report) ---
    if "backlinks" in by_name:
        text = _read_report(brand_dir / by_name["backlinks"]["link"])
        pairs = _extract_num_pairs(text)
        # These reports may not use the summary-card pattern; try table cells.
        bc = pairs.get("total backlinks") or pairs.get("backlinks")
        if not bc:
            m = re.search(r"(\d[\d,]*)\s+backlinks?", text, re.IGNORECASE)
            bc = m.group(1) if m else "—"
        kpis["backlinks"] = {
            "total_backlinks":   bc,
            "referring_domains": pairs.get("referring domains", "—"),
            "source":            by_name["backlinks"]["filename"],
        }

    # --- AI Visibility (separate format) ---
    if "ai_visibility" in by_name:
        text = _read_report(brand_dir / by_name["ai_visibility"]["link"])
        rate = _extract_ai_visibility_rate(text)
        kpis["ai_visibility"] = {
            "mention_rate":  rate or "—",
            "source":        by_name["ai_visibility"]["filename"],
        }

    return kpis


# --------------------------------------------------------------------------- #
# 3. generate_dashboard
# --------------------------------------------------------------------------- #

def _kpi_card(label: str, value: str, sub: str = "", source: str = "") -> str:
    esc = html_lib.escape
    return f"""
    <div class="kpi-card">
      <div class="kpi-value">{esc(value)}</div>
      <div class="kpi-label">{esc(label)}</div>
      {f'<div class="kpi-sub">{esc(sub)}</div>' if sub else ''}
      {f'<div class="kpi-src">{esc(source)}</div>' if source else ''}
    </div>"""


def _report_card(r: Dict, accent: str) -> str:
    esc = html_lib.escape
    metric = f'<div class="card-metric">{esc(r["key_metric"])}</div>' if r["key_metric"] else ""
    return f"""
    <a class="report-card" href="{esc(r['link'])}" data-level="{esc(r['level'])}"
       data-title="{esc(r['title'].lower())}" data-date="{esc(r['date'])}">
      <div class="card-top" style="border-top-color:{accent}">
        <span class="card-level" style="color:{accent}">{esc(r['level'].upper())}</span>
        <span class="card-date">{esc(r['date'])}</span>
      </div>
      <div class="card-title">{esc(r['title'])}</div>
      <div class="card-desc">{esc(r['description'])}</div>
      {metric}
      <div class="card-meta">{esc(r['filename'])} · {esc(r['size'])}</div>
    </a>"""


def generate_dashboard(
    reports: List[Dict],
    kpis: Dict[str, Dict[str, str]],
    output_path: Path,
    brand: str,
) -> None:
    """Render the self-contained HTML dashboard and write it to output_path."""
    esc = html_lib.escape
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # KPI row values
    vis = kpis.get("visibility", {})
    sh = kpis.get("site_health", {})
    bl = kpis.get("backlinks", {})
    ai = kpis.get("ai_visibility", {})
    rd = kpis.get("rankings", {})

    kpi_cards = "".join([
        _kpi_card("Site Health Score", sh.get("score", "—"),
                  f"{sh.get('criticals', '—')} critical · {sh.get('warnings', '—')} warnings",
                  sh.get("source", "")),
        _kpi_card("Est. Monthly Traffic", vis.get("est_traffic", "—"),
                  f"Visibility Index {vis.get('visibility_index', '—')}",
                  vis.get("source", "")),
        _kpi_card("Keywords Ranked", vis.get("keywords_ranked", "—"),
                  f"{rd.get('in_top10', '—')} in Top 10 · {rd.get('in_top3', '—')} in Top 3",
                  vis.get("source", "")),
        # Prefer the visibility report's domain-wide total backlinks; fall back
        # to the per-URL backlink-analysis report when the visibility report
        # doesn't carry that metric.
        _kpi_card("Total Backlinks",
                  vis.get("total_backlinks") or bl.get("total_backlinks", "—"),
                  "Domain-wide total", vis.get("source") or bl.get("source", "")),
        _kpi_card("Referring Domains", vis.get("referring_domains") or bl.get("referring_domains", "—"),
                  f"Domain Rating {vis.get('domain_rating', '—')}",
                  vis.get("source", "")),
        _kpi_card("AI Visibility Rate", ai.get("mention_rate", "—"),
                  "LLM mention rate", ai.get("source", "")),
    ])

    # Report library — group by level, in fixed order.
    by_level: Dict[str, List[Dict]] = {lv: [] for lv in LEVEL_ORDER}
    for r in reports:
        by_level[r["level"]].append(r)

    report_sections = []
    for lv in LEVEL_ORDER:
        items = by_level[lv]
        meta = LEVEL_META[lv]
        header = f"""
        <div class="level-header" id="level-{lv}">
          <span class="level-icon">{meta['icon']}</span>
          <h2>{meta['title']} <span class="level-count">({len(items)})</span></h2>
          <p class="level-desc">{esc(meta['desc'])}</p>
        </div>"""
        if not items:
            cards = '<div class="empty">No reports in this category yet.</div>'
        else:
            cards = "".join(_report_card(r, meta["accent"]) for r in items)
        report_sections.append(f'<section class="level-section" data-level="{lv}">{header}<div class="card-grid">{cards}</div></section>')

    # Level filter chips
    chips = "".join(
        f'<button class="chip" data-filter="{lv}">{LEVEL_META[lv]["icon"]} {LEVEL_META[lv]["title"]} <span class="chip-count">{len(by_level[lv])}</span></button>'
        for lv in LEVEL_ORDER
    )

    total = len(reports)

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex,nofollow">
<title>{esc(brand.title())} — SEO Dashboard</title>
<style>
:root {{
  --bg: #ffffff; --surface: #f8f9fa; --border: #e0e0e0; --text: #1a1a1a;
  --muted: #6b7280; --accent: #0066cc; --accent-dark: #004999;
  --shadow: 0 1px 3px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04);
}}
* {{ box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
  background: var(--bg); color: var(--text); margin: 0; font-size: 15px; line-height: 1.5;
}}
a {{ color: var(--accent); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.container {{ max-width: 1200px; margin: 0 auto; padding: 24px 20px; }}

/* Header */
.dashboard-header {{
  display: flex; align-items: center; justify-content: space-between;
  flex-wrap: wrap; gap: 12px; margin-bottom: 28px;
  padding-bottom: 20px; border-bottom: 2px solid var(--border);
}}
.dashboard-header h1 {{ font-size: 28px; font-weight: 700; margin: 0; color: var(--text); }}
.dashboard-header .brand-mark {{
  display: inline-flex; align-items: center; gap: 10px;
}}
.dashboard-header .brand-mark .dot {{
  width: 12px; height: 12px; border-radius: 50%; background: var(--accent);
}}
.header-meta {{ text-align: right; color: var(--muted); font-size: 13px; }}
.header-meta .updated {{ font-weight: 600; color: var(--text); }}

/* KPI row */
.kpi-row {{
  display: grid; grid-template-columns: repeat(6, 1fr); gap: 14px; margin-bottom: 32px;
}}
.kpi-card {{
  background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
  padding: 18px 14px; text-align: center; box-shadow: var(--shadow);
  transition: transform .15s ease, box-shadow .15s ease;
}}
.kpi-card:hover {{ transform: translateY(-2px); box-shadow: 0 4px 12px rgba(0,0,0,0.08); }}
.kpi-value {{ font-size: 26px; font-weight: 700; color: var(--accent); line-height: 1.1; word-break: break-word; }}
.kpi-label {{ font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .03em; margin-top: 6px; }}
.kpi-sub {{ font-size: 11px; color: var(--muted); margin-top: 6px; }}
.kpi-src {{ font-size: 10px; color: #aaa; margin-top: 4px; }}

/* Quick actions */
.quick-actions {{
  display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 28px;
}}
.quick-actions a {{
  display: inline-flex; align-items: center; gap: 6px; padding: 10px 16px;
  border: 1px solid var(--border); border-radius: 8px; background: var(--surface);
  font-size: 14px; font-weight: 600; color: var(--text); transition: all .15s ease;
}}
.quick-actions a:hover {{ border-color: var(--accent); color: var(--accent); text-decoration: none; }}

/* Toolbar: search + filter chips */
.toolbar {{ margin-bottom: 20px; }}
.toolbar .search {{
  width: 100%; padding: 11px 14px; font-size: 14px; border: 1px solid var(--border);
  border-radius: 8px; margin-bottom: 12px;
}}
.toolbar .search:focus {{ outline: none; border-color: var(--accent); }}
.chips {{ display: flex; gap: 8px; flex-wrap: wrap; }}
.chip {{
  display: inline-flex; align-items: center; gap: 6px; padding: 6px 14px;
  border: 1px solid var(--border); border-radius: 999px; background: var(--bg);
  font-size: 13px; font-weight: 600; cursor: pointer; transition: all .15s ease;
}}
.chip:hover {{ border-color: var(--accent); }}
.chip.active {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
.chip-count {{ font-size: 11px; opacity: .7; }}

/* Level sections */
.level-section {{ margin-bottom: 36px; }}
.level-header {{ margin-bottom: 14px; }}
.level-header h2 {{ font-size: 19px; font-weight: 700; display: inline; margin: 0; }}
.level-icon {{ margin-right: 8px; }}
.level-count {{ color: var(--muted); font-weight: 400; font-size: 15px; }}
.level-desc {{ font-size: 13px; color: var(--muted); margin: 4px 0 0; }}

.card-grid {{
  display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 14px;
}}
.report-card {{
  display: block; background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 14px; box-shadow: var(--shadow);
  color: var(--text); transition: transform .15s ease, box-shadow .15s ease, border-color .15s ease;
}}
.report-card:hover {{
  transform: translateY(-2px); box-shadow: 0 6px 16px rgba(0,0,0,0.10);
  border-color: var(--accent); text-decoration: none;
}}
.card-top {{
  display: flex; justify-content: space-between; align-items: center;
  border-top: 3px solid; margin: -14px -14px 10px; padding: 8px 14px;
  border-bottom: 1px solid var(--border);
}}
.card-level {{ font-size: 11px; font-weight: 700; letter-spacing: .04em; }}
.card-date {{ font-size: 12px; color: var(--muted); }}
.card-title {{ font-size: 15px; font-weight: 600; margin-bottom: 4px; line-height: 1.35; }}
.card-desc {{ font-size: 13px; color: var(--muted); margin-bottom: 8px; }}
.card-metric {{
  display: inline-block; font-size: 12px; font-weight: 600; color: var(--accent);
  background: rgba(0,102,204,.08); padding: 2px 8px; border-radius: 6px; margin-bottom: 8px;
}}
.card-meta {{ font-size: 11px; color: #999; word-break: break-all; }}
.empty {{ color: var(--muted); font-size: 14px; padding: 12px 0; }}

.footer {{
  margin-top: 40px; padding-top: 18px; border-top: 1px solid var(--border);
  font-size: 12px; color: var(--muted);
}}
.footer h3 {{ font-size: 13px; font-weight: 700; margin: 0 0 6px; color: var(--text); }}
.footer ul {{ margin: 0; padding-left: 18px; }}
.footer li {{ margin-bottom: 3px; }}

.no-results {{ display: none; text-align: center; padding: 40px; color: var(--muted); font-size: 16px; }}

@media (max-width: 900px) {{
  .kpi-row {{ grid-template-columns: repeat(3, 1fr); }}
}}
@media (max-width: 600px) {{
  .kpi-row {{ grid-template-columns: repeat(2, 1fr); }}
  .dashboard-header {{ flex-direction: column; align-items: flex-start; }}
  .header-meta {{ text-align: left; }}
}}
</style>
</head>
<body>
<div class="container">

  <header class="dashboard-header">
    <div class="brand-mark">
      <span class="dot"></span>
      <h1>{esc(brand.title())} — SEO Dashboard</h1>
    </div>
    <div class="header-meta">
      <div class="updated">Last updated {now}</div>
      <div>{total} reports · 6 categories</div>
    </div>
  </header>

  <div class="kpi-row">
    {kpi_cards}
  </div>

  <div class="quick-actions">
    <a href="#" onclick="alert('Run from CLI: python3 ~/.hermes/scripts/site_auditor.py --domain {esc(brand)}.com');return false">🔍 Run Site Audit</a>
    <a href="keyword-rank-tracker.html">📈 Check Rankings</a>
    <a href="backlink-analysis-1.html">🔗 Backlink Report</a>
    <a href="ai-visibility-report.html">🤖 AI Visibility</a>
    <a href="seo-visibility-report.html">📊 SEO Visibility</a>
    <a href="reports.html">📋 All Reports</a>
  </div>

  <div class="toolbar">
    <input type="text" class="search" id="search" placeholder="Search reports by title, keyword, or filename…" autocomplete="off">
    <div class="chips" id="chips">
      <button class="chip active" data-filter="all">All ({total})</button>
      {chips}
    </div>
  </div>

  <div id="no-results" class="no-results">No reports match your search.</div>

  {"".join(report_sections)}

  <footer class="footer">
    <h3>Data Sources</h3>
    <ul>
      <li>DataForSEO — SERP, keywords, backlinks, visibility metrics</li>
      <li>Screaming Frog / site_auditor.py — technical crawl &amp; site health</li>
      <li>Google Search Console — query &amp; page performance</li>
      <li>LLM Visibility — ChatGPT, Gemini, Perplexity, Claude (ai_visibility_helper.py)</li>
      <li>Majestic / backlink_gap_analyzer.py — referring domains &amp; link gaps</li>
    </ul>
    <p style="margin-top:10px">Generated by <code>portal_dashboard.py</code> · Zenfusion SEO Suite · {now}</p>
  </footer>

</div>

<script>
(function() {{
  const search = document.getElementById('search');
  const chips = document.getElementById('chips');
  const sections = document.querySelectorAll('.level-section');
  const noResults = document.getElementById('no-results');
  let activeFilter = 'all';

  function applyFilter() {{
    const q = search.value.trim().toLowerCase();
    let visibleCount = 0;
    sections.forEach(function(sec) {{
      const lv = sec.getAttribute('data-level');
      // Hide whole section if level filter is active and doesn't match.
      const levelOk = (activeFilter === 'all' || activeFilter === lv);
      let sectionVisible = 0;
      sec.querySelectorAll('.report-card').forEach(function(card) {{
        const title = card.getAttribute('data-title') || '';
        const text = card.textContent.toLowerCase();
        const matches = (q === '' || title.indexOf(q) !== -1 || text.indexOf(q) !== -1);
        const show = levelOk && matches;
        card.style.display = show ? '' : 'none';
        if (show) sectionVisible++;
      }});
      sec.style.display = (levelOk && sectionVisible > 0) ? '' : 'none';
      if (levelOk) visibleCount += sectionVisible;
    }});
    noResults.style.display = visibleCount === 0 ? 'block' : 'none';
  }}

  search.addEventListener('input', applyFilter);
  chips.addEventListener('click', function(e) {{
    const chip = e.target.closest('.chip');
    if (!chip) return;
    chips.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
    chip.classList.add('active');
    activeFilter = chip.getAttribute('data-filter');
    applyFilter();
  }});
}})();
</script>
</body>
</html>
"""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(doc, encoding="utf-8")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a Zenfusion client SEO dashboard HTML page."
    )
    parser.add_argument("--brand", required=True, help="Brand/client name (subdir under audits-dir)")
    parser.add_argument("--audits-dir", default="/tmp/zenfusion-audits",
                        help="Root directory containing brand subdirs (default: /tmp/zenfusion-audits)")
    parser.add_argument("--output", default=None,
                        help="Output HTML path (default: <audits-dir>/<brand>/dashboard.html)")
    args = parser.parse_args(argv)

    audits_dir = Path(args.audits_dir).resolve()
    brand = args.brand
    brand_dir = audits_dir / brand

    if not brand_dir.is_dir():
        print(f"ERROR: brand directory not found: {brand_dir}", file=sys.stderr)
        return 1

    output_path = Path(args.output) if args.output else (brand_dir / "dashboard.html")

    print(f"Scanning reports in {brand_dir} …")
    reports = scan_available_reports(audits_dir, brand)
    print(f"  Found {len(reports)} reports across {len(set(r['level'] for r in reports))} levels")

    print("Extracting KPIs from key reports …")
    kpis = extract_kpis(reports, brand_dir)
    for group, data in kpis.items():
        src = data.get("source", "")
        headline = next((v for k, v in data.items() if k != "source"), "—")
        print(f"  [{group}] {headline}  (from {src})")

    # Stamp key_metric onto the marquee report cards so they surface a number.
    # Reuses the same marquee-report mapping as extract_kpis to stay in sync.
    _metric_for = {
        "site_health":   lambda d: d.get("score"),
        "visibility":    lambda d: f"Est. Traffic {d.get('est_traffic', '—')}",
        "rankings":      lambda d: f"{d.get('total_tracked', '—')} tracked",
        "keyword_dash":  lambda d: f"{d.get('gap_keywords', '—')} gap keywords",
        "backlinks":     lambda d: f"{d.get('total_backlinks', '—')} backlinks",
        "ai_visibility": lambda d: d.get("mention_rate"),
    }
    by_filename = {r["filename"]: r for r in reports}
    for group, data in kpis.items():
        src = data.get("source", "")
        if src in by_filename and group in _metric_for:
            metric = _metric_for[group](data)
            if metric and metric != "—":
                by_filename[src]["key_metric"] = metric

    print(f"Generating dashboard → {output_path}")
    generate_dashboard(reports, kpis, output_path, brand)
    print(f"✓ Dashboard written ({output_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
