#!/usr/bin/env python3
"""
Content Decay Detector — standalone Python script.

Identifies pages losing organic traffic over time by comparing Google Search
Console data across two consecutive periods (last N days vs. previous N days),
classifies the severity of decay, correlates with query-level losses, and emits
a white SaaS themed HTML report with summary cards, an SVG distribution chart,
expandable tables, top movers, keyword correlation, per-page recommendations,
and a prioritized action list.

Usage:
  python3 content_decay_detector.py --domain softprowatersystems.com \
      --output /tmp/test_decay [--days-back 28]

Outputs:
  - <output>/content_decay_report.html  (white SaaS theme report)
  - <output>/content_decay_data.json     (raw analysis JSON)
  - console summary

Environment:
  GSC credentials: ~/.hermes/google_token.json (via gsc_helper.py)
  DATAFORSEO credentials (optional) in ~/.hermes/.env
"""

import os
import sys
import json
import math
import html
import argparse
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

# ============================================================================
# CONFIG
# ============================================================================

DECAY_THRESHOLDS = {
    "impressions_drop": 0.20,   # flag if impressions dropped >20%
    "clicks_drop": 0.20,        # flag if clicks dropped >20%
    "position_worsen": 2.0,     # flag if position worsened >2 ranks
    "fell_out_threshold": 100,  # position beyond this = "fell out of top 100"
}

SEVERITY_LEVELS = {
    "Severe":    {"min_drop": 0.50, "color": "#ef4444", "label": "Severe"},
    "Moderate":  {"min_drop": 0.20, "color": "#f59e0b", "label": "Moderate"},
    "Mild":      {"min_drop": 0.01, "color": "#eab308", "label": "Mild"},
    "Recovered": {"min_drop": 0.00, "color": "#10b981", "label": "Recovered"},
    "New":       {"min_drop": -1.0, "color": "#3b82f6", "label": "New"},
}

# ============================================================================
# GSC DATA FETCH
# ============================================================================

def _resolve_site_url(domain):
    """Resolve the GSC site_url for a domain by listing properties."""
    try:
        from gsc_helper import list_properties
        properties = list_properties()
        if properties:
            bare = domain.replace("www.", "").lower()
            matching = [p for p in properties if bare in p.replace("www.", "").lower()]
            if matching:
                return matching[0]
    except Exception as e:
        print(f"  [warn] Could not list GSC properties: {e}")
    return f"https://{domain}/"


def fetch_gsc_data(domain, days_back=28):
    """
    Fetch GSC page data for the last N days AND the previous N days.

    Returns a dict:
      {
        "domain": ...,
        "days_back": N,
        "current_period": {"start": ..., "end": ..., "pages": [...]},
        "previous_period": {"start": ..., "end": ..., "pages": [...]},
        "queries_current":   [{"query":..., "page":..., "impressions":...}, ...],
        "queries_previous":  [{"query":..., "page":..., "impressions":...}, ...],
        "source": "gsc" | "synthetic",
      }

    Each page entry: {page, impressions, clicks, ctr, position}
    """
    site_url = _resolve_site_url(domain)

    today = datetime.now().date()
    cur_end = today - timedelta(days=1)
    cur_start = cur_end - timedelta(days=days_back - 1)
    prev_end = cur_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days_back - 1)

    print(f"  GSC site_url: {site_url}")
    print(f"  Current period:  {cur_start} → {cur_end} ({days_back}d)")
    print(f"  Previous period: {prev_start} → {prev_end} ({days_back}d)")

    try:
        from gsc_helper import get_search_analytics
        current_pages = get_search_analytics(
            site_url, dimensions=["page"],
            start_date=str(cur_start), end_date=str(cur_end),
            row_limit=1000)
        previous_pages = get_search_analytics(
            site_url, dimensions=["page"],
            start_date=str(prev_start), end_date=str(prev_end),
            row_limit=1000)
        # Query+page combos for keyword correlation
        queries_current = get_search_analytics(
            site_url, dimensions=["query", "page"],
            start_date=str(cur_start), end_date=str(cur_end),
            row_limit=1000)
        queries_previous = get_search_analytics(
            site_url, dimensions=["query", "page"],
            start_date=str(prev_start), end_date=str(prev_end),
            row_limit=1000)

        if not current_pages and not previous_pages:
            raise RuntimeError("GSC returned no page data for either period")

        return {
            "domain": domain,
            "site_url": site_url,
            "days_back": days_back,
            "current_period": {
                "start": str(cur_start), "end": str(cur_end),
                "pages": _normalize_pages(current_pages),
            },
            "previous_period": {
                "start": str(prev_start), "end": str(prev_end),
                "pages": _normalize_pages(previous_pages),
            },
            "queries_current": _normalize_queries(queries_current),
            "queries_previous": _normalize_queries(queries_previous),
            "source": "gsc",
        }
    except Exception as e:
        print(f"  [warn] GSC fetch failed ({e}). Falling back to synthetic dataset.")
        return _synthetic_data(domain, days_back, cur_start, cur_end, prev_start, prev_end)


def _normalize_pages(rows):
    """Normalize GSC page rows into list of {page, impressions, clicks, ctr, position}."""
    out = []
    for r in rows:
        out.append({
            "page": r.get("page") or (r.get("keys", [None])[0] if r.get("keys") else None),
            "impressions": r.get("impressions", 0),
            "clicks": r.get("clicks", 0),
            "ctr": r.get("ctr", 0.0),
            "position": r.get("position", 0.0),
        })
    # Remove entries with no page
    return [p for p in out if p.get("page")]


def _normalize_queries(rows):
    """Normalize query+page rows into list of {query, page, impressions, clicks, ctr, position}."""
    out = []
    for r in rows:
        keys = r.get("keys", [])
        out.append({
            "query": keys[0] if len(keys) > 0 else r.get("query"),
            "page": keys[1] if len(keys) > 1 else r.get("page"),
            "impressions": r.get("impressions", 0),
            "clicks": r.get("clicks", 0),
            "ctr": r.get("ctr", 0.0),
            "position": r.get("position", 0.0),
        })
    return [q for q in out if q.get("page") and q.get("query")]


# ============================================================================
# SYNTHETIC FALLBACK (for offline testing / no GSC access)
# ============================================================================

def _synthetic_data(domain, days_back, cur_start, cur_end, prev_start, prev_end):
    """Generate a realistic synthetic dataset so the full pipeline can run offline."""
    import random
    random.seed(hash(domain) & 0xffffffff)

    base_paths = [
        "/", "/water-softener-systems", "/best-water-softener",
        "/water-filtration-systems", "/reverse-osmosis-systems",
        "/blog/how-water-softeners-work", "/blog/water-softener-vs-water-filtration",
        "/blog/best-water-softener-2025", "/blog/how-to-maintain-water-softener",
        "/whole-house-water-filter", "/salt-free-water-softener",
        "/water-softener-installation", "/commercial-water-softener",
        "/blog/water-softener-resin-guide", "/blog/hard-water-effects",
        "/blog/iron-filter-guide", "/blog/water-softener-troubleshooting",
        "/water-softener-reviews", "/water-softener-cost", "/contact",
    ]

    queries_map = {
        "/": ["water softener company", "water treatment services"],
        "/water-softener-systems": ["water softener systems", "best water softener system"],
        "/best-water-softener": ["best water softener", "top rated water softener"],
        "/water-filtration-systems": ["water filtration systems", "home water filtration"],
        "/reverse-osmosis-systems": ["reverse osmosis system", "ro water system"],
        "/blog/how-water-softeners-work": ["how do water softeners work", "water softener process"],
        "/blog/water-softener-vs-water-filtration": ["water softener vs water filtration"],
        "/blog/best-water-softener-2025": ["best water softener 2025", "water softener reviews 2025"],
        "/blog/how-to-maintain-water-softener": ["water softener maintenance", "maintain water softener"],
        "/whole-house-water-filter": ["whole house water filter", "whole home water filter"],
        "/salt-free-water-softener": ["salt free water softener", "saltless water softener"],
        "/water-softener-installation": ["water softener installation", "install water softener"],
        "/commercial-water-softener": ["commercial water softener", "industrial water softener"],
        "/blog/water-softener-resin-guide": ["water softener resin", "resin replacement"],
        "/blog/hard-water-effects": ["hard water effects", "hard water problems"],
        "/blog/iron-filter-guide": ["iron filter for well water", "iron water filter"],
        "/blog/water-softener-troubleshooting": ["water softener troubleshooting", "water softener problems"],
        "/water-softener-reviews": ["water softener reviews", "water softener ratings"],
        "/water-softener-cost": ["water softener cost", "how much is a water softener"],
        "/contact": ["contact water softener company"],
    }

    def make_period(decay_factors):
        pages = []
        for i, path in enumerate(base_paths):
            base_imp = random.randint(200, 5000)
            base_clk = int(base_imp * random.uniform(0.01, 0.06))
            factor = decay_factors.get(path, 1.0)
            pages.append({
                "page": path,
                "impressions": max(0, int(base_imp * factor)),
                "clicks": max(0, int(base_clk * factor)),
                "ctr": round(random.uniform(0.01, 0.05), 4),
                "position": round(random.uniform(3, 25) * (1.0 / max(factor, 0.1)), 1),
            })
        return pages

    # Previous period: baseline factors ~1.0
    prev_factors = {p: random.uniform(0.8, 1.2) for p in base_paths}

    # Current period: some pages decay, some gain, one new
    decay_set = {
        "/blog/best-water-softener-2025": 0.35,   # severe
        "/best-water-softener": 0.45,             # severe
        "/blog/water-softener-vs-water-filtration": 0.60,  # moderate
        "/blog/how-water-softeners-work": 0.70,    # moderate
        "/water-softener-reviews": 0.80,          # mild
        "/blog/water-softener-troubleshooting": 0.25,  # severe (fell far)
    }
    gain_set = {
        "/salt-free-water-softener": 1.55,
        "/blog/iron-filter-guide": 1.40,
        "/whole-house-water-filter": 1.30,
    }
    cur_factors = {}
    for p in base_paths:
        if p in decay_set:
            cur_factors[p] = decay_set[p] * prev_factors.get(p, 1.0)
        elif p in gain_set:
            cur_factors[p] = gain_set[p] * prev_factors.get(p, 1.0)
        else:
            cur_factors[p] = prev_factors.get(p, 1.0) * random.uniform(0.95, 1.05)

    prev_pages = make_period(prev_factors)
    cur_pages = make_period(cur_factors)

    # Add a new page in current period only
    new_page = "/blog/water-softener-salt-types"
    cur_pages.append({
        "page": new_page,
        "impressions": 850,
        "clicks": 22,
        "ctr": 0.0259,
        "position": 8.4,
    })
    queries_map[new_page] = ["types of water softener salt", "water softener salt guide"]

    # Build query data
    def make_queries(pages, factor_map):
        out = []
        for p in pages:
            for q in queries_map.get(p["page"], [p["page"]]):
                f = factor_map.get(p["page"], 1.0)
                out.append({
                    "query": q,
                    "page": p["page"],
                    "impressions": max(0, int(p["impressions"] / max(len(queries_map.get(p["page"], [p["page"]])) , 1) * random.uniform(0.4, 1.0))),
                    "clicks": max(0, int(p["clicks"] / 3)),
                    "ctr": round(random.uniform(0.01, 0.05), 4),
                    "position": p["position"],
                })
        return out

    queries_prev = make_queries(prev_pages, prev_factors)
    queries_cur = make_queries(cur_pages, cur_factors)

    return {
        "domain": domain,
        "site_url": f"https://{domain}/",
        "days_back": days_back,
        "current_period": {
            "start": str(cur_start), "end": str(cur_end),
            "pages": cur_pages,
        },
        "previous_period": {
            "start": str(prev_start), "end": str(prev_end),
            "pages": prev_pages,
        },
        "queries_current": queries_cur,
        "queries_previous": queries_prev,
        "source": "synthetic",
    }


# ============================================================================
# DECAY DETECTION
# ============================================================================

def detect_decay(current_period, previous_period):
    """
    Compare impressions/clicks/position per URL between two periods.

    Flags pages where:
      - impressions dropped >20%
      - clicks dropped >20%
      - position worsened >2 ranks
      - page fell out of top 100 (was <100, now >100 or gone)

    Computes a decay severity score (0-100) per page.

    Returns dict:
      {
        "pages": [ {page, current:{...}, previous:{...}, changes:{...},
                    severity, classification, decay_score, flags:[...]} ],
        "summary": {...}
      }
    """
    cur_map = {p["page"]: p for p in current_period.get("pages", [])}
    prev_map = {p["page"]: p for p in previous_period.get("pages", [])}
    all_pages = set(cur_map.keys()) | set(prev_map.keys())

    results = []
    for page in all_pages:
        cur = cur_map.get(page)
        prev = prev_map.get(page)

        # Handle fell-out: was in previous, not in current (or position >100 now)
        if prev and not cur:
            # Page dropped out entirely in current period
            entry = _build_entry(page, None, prev, fell_out=True)
            results.append(entry)
            continue

        entry = _build_entry(page, cur, prev)
        results.append(entry)

    # Sort by traffic loss (absolute impressions lost) descending
    results.sort(key=lambda x: x["changes"].get("impressions_lost", 0), reverse=True)

    summary = _compute_summary(results)
    return {"pages": results, "summary": summary}


def _build_entry(page, cur, prev, fell_out=False):
    """Build a single page comparison entry."""
    cur_imp = cur["impressions"] if cur else 0
    cur_clk = cur["clicks"] if cur else 0
    cur_pos = cur["position"] if cur else 999.0
    cur_ctr = cur["ctr"] if cur else 0.0

    prev_imp = prev["impressions"] if prev else 0
    prev_clk = prev["clicks"] if prev else 0
    prev_pos = prev["position"] if prev else 999.0
    prev_ctr = prev["ctr"] if prev else 0.0

    flags = []

    if fell_out:
        imp_change_pct = -1.0
        clk_change_pct = -1.0 if prev_clk > 0 else 0.0
        pos_delta = 999.0 - prev_pos
        flags.append("fell_out_of_top100")
        classification = "Severe"
        decay_score = 100.0
    else:
        imp_change_pct = ((cur_imp - prev_imp) / prev_imp) if prev_imp > 0 else (1.0 if cur_imp > 0 else 0.0)
        clk_change_pct = ((cur_clk - prev_clk) / prev_clk) if prev_clk > 0 else (1.0 if cur_clk > 0 else 0.0)
        pos_delta = cur_pos - prev_pos  # positive = worsened

        # Flags
        if imp_change_pct < -DECAY_THRESHOLDS["impressions_drop"]:
            flags.append("impressions_drop")
        if clk_change_pct < -DECAY_THRESHOLDS["clicks_drop"]:
            flags.append("clicks_drop")
        if pos_delta > DECAY_THRESHOLDS["position_worsen"]:
            flags.append("position_worsen")
        if prev and cur and prev_pos < DECAY_THRESHOLDS["fell_out_threshold"] and cur_pos >= DECAY_THRESHOLDS["fell_out_threshold"]:
            flags.append("fell_out_of_top100")

        classification = classify_decay(page, cur, prev)
        decay_score = _compute_decay_score(imp_change_pct, clk_change_pct, pos_delta, flags)

    impressions_lost = prev_imp - cur_imp
    clicks_lost = prev_clk - cur_clk

    return {
        "page": page,
        "current": {"impressions": cur_imp, "clicks": cur_clk, "ctr": cur_ctr, "position": cur_pos},
        "previous": {"impressions": prev_imp, "clicks": prev_clk, "ctr": prev_ctr, "position": prev_pos},
        "changes": {
            "impressions_change_pct": round(imp_change_pct, 4),
            "clicks_change_pct": round(clk_change_pct, 4),
            "position_delta": round(pos_delta, 2),
            "impressions_lost": max(0, impressions_lost),
            "clicks_lost": max(0, clicks_lost),
        },
        "flags": flags,
        "severity": classification,
        "classification": classification,
        "decay_score": round(decay_score, 1),
    }


def _compute_decay_score(imp_pct, clk_pct, pos_delta, flags):
    """
    Compute a 0-100 decay severity score.

    Weighting:
      - impressions drop: 35%
      - clicks drop: 30%
      - position worsen: 25%
      - fell out flag: 10% bonus
    """
    imp_component = min(100, abs(min(0, imp_pct)) * 100 * (1 / 0.50))  # 50% drop = 100
    clk_component = min(100, abs(min(0, clk_pct)) * 100 * (1 / 0.50))
    pos_component = min(100, max(0, pos_delta) * (100 / 20))  # 20 positions = 100
    fell_bonus = 10 if "fell_out_of_top100" in flags else 0

    score = (imp_component * 0.35) + (clk_component * 0.30) + (pos_component * 0.25) + fell_bonus
    return min(100, score)


def classify_decay(page, current, previous):
    """
    Classify decay severity based on traffic change.

    - Severe:    traffic (impressions) dropped >50%
    - Moderate:  traffic dropped 20-50%
    - Mild:      traffic dropped <20% (but some drop)
    - Recovered: traffic increased
    - New:       not in previous period
    """
    if current and not previous:
        return "New"
    if previous and not current:
        return "Severe"  # fell out entirely

    cur_imp = current["impressions"] if current else 0
    prev_imp = previous["impressions"] if previous else 0

    if prev_imp == 0:
        return "New" if cur_imp > 0 else "Mild"

    change_pct = (cur_imp - prev_imp) / prev_imp

    if change_pct > 0:
        return "Recovered"
    if change_pct <= -0.50:
        return "Severe"
    if change_pct <= -0.20:
        return "Moderate"
    return "Mild"


def _compute_summary(results):
    """Compute summary stats over all page entries."""
    total = len(results)
    decaying = sum(1 for r in results if r["severity"] in ("Severe", "Moderate", "Mild"))
    severe = sum(1 for r in results if r["severity"] == "Severe")
    moderate = sum(1 for r in results if r["severity"] == "Moderate")
    mild = sum(1 for r in results if r["severity"] == "Mild")
    recovered = sum(1 for r in results if r["severity"] == "Recovered")
    new_pages = sum(1 for r in results if r["severity"] == "New")

    total_imp_lost = sum(r["changes"].get("impressions_lost", 0) for r in results)
    total_clk_lost = sum(r["changes"].get("clicks_lost", 0) for r in results)

    return {
        "total_pages": total,
        "decaying_pages": decaying,
        "severe": severe,
        "moderate": moderate,
        "mild": mild,
        "recovered": recovered,
        "new_pages": new_pages,
        "total_impressions_lost": total_imp_lost,
        "total_clicks_lost": total_clk_lost,
    }


# ============================================================================
# KEYWORD CORRELATION
# ============================================================================

def correlate_keywords(decay_pages, queries_current, queries_previous, top_n=10):
    """
    For the top decaying pages, find which queries lost impressions.

    Returns list per page:
      {page, lost_queries: [{query, prev_imp, cur_imp, change_pct}, ...]}
    """
    cur_qmap = {}  # (page, query) -> impressions
    prev_qmap = {}
    for q in queries_current:
        key = (q["page"], q["query"])
        cur_qmap[key] = cur_qmap.get(key, 0) + q["impressions"]
    for q in queries_previous:
        key = (q["page"], q["query"])
        prev_qmap[key] = prev_qmap.get(key, 0) + q["impressions"]

    correlations = []
    for entry in decay_pages[:top_n]:
        page = entry["page"]
        # All queries for this page across both periods
        all_queries = set(k[1] for k in list(cur_qmap.keys()) + list(prev_qmap.keys()) if k[0] == page)
        lost = []
        for q in all_queries:
            cur_imp = cur_qmap.get((page, q), 0)
            prev_imp = prev_qmap.get((page, q), 0)
            if prev_imp > 0:
                change = (cur_imp - prev_imp) / prev_imp
                if change < 0:
                    lost.append({
                        "query": q,
                        "prev_imp": prev_imp,
                        "cur_imp": cur_imp,
                        "change_pct": round(change, 4),
                        "imp_lost": prev_imp - cur_imp,
                    })
        lost.sort(key=lambda x: x["imp_lost"], reverse=True)
        correlations.append({"page": page, "lost_queries": lost[:15]})

    return correlations


# ============================================================================
# RECOMMENDATIONS
# ============================================================================

def generate_recommendations(page_entry):
    """
    Generate per-page recommendations based on decay signals.

    Returns list of recommendation strings.
    """
    recs = []
    flags = page_entry.get("flags", [])
    severity = page_entry.get("severity", "")
    changes = page_entry.get("changes", {})

    if severity in ("Severe",):
        recs.append("🚨 URGENT: Significant traffic loss — audit content freshness, "
                     "check for algorithm update impact, and review competitor changes.")
    if "position_worsen" in flags:
        recs.append("📈 Position dropped significantly — refresh on-page SEO (title, meta, "
                     "H1, content depth) and build new internal links to this page.")
    if "impressions_drop" in flags:
        recs.append("🔍 Impressions declining — update content with current information, "
                     "add new sections, and expand keyword coverage.")
    if "clicks_drop" in flags:
        recs.append("🖱️ Clicks dropping — improve title tag and meta description for higher CTR; "
                     "check for SERP feature changes (featured snippets, etc.).")
    if "fell_out_of_top100" in flags:
        recs.append("❌ Page fell out of top 100 — check for manual actions, deindexing, "
                     "or severe content quality issues. May need full content overhaul.")
    if severity == "Moderate":
        recs.append("🔧 Moderate decay — refresh content, add internal links from high-authority pages, "
                     "and update publish date if appropriate.")
    if severity == "Mild":
        recs.append("✏️ Mild decay — monitor closely; consider a light content refresh and "
                     "internal linking boost.")
    if severity == "Recovered":
        recs.append("✅ Recovering — double down on what's working; expand winning content.")
    if severity == "New":
        recs.append("🆕 New page appearing — ensure proper indexing, build internal links, "
                     "and monitor for continued growth.")

    # Always include these baseline checks
    recs.append("🔗 Check for keyword cannibalization (run cannibalization_detector.py).")
    recs.append("📊 Refresh schema markup (Product, Article, FAQ, BreadcrumbList as applicable).")
    recs.append("⚡ Run PageSpeed audit — core web vitals improvements can recover rankings.")

    return recs


# ============================================================================
# PRIORITY ACTION LIST
# ============================================================================

def build_priority_list(decay_pages, top_n=20):
    """
    Build a priority action list sorted by traffic impact (impressions lost).
    Each entry: {rank, page, severity, impressions_lost, clicks_lost, decay_score, top_action}
    """
    # Only include pages with actual decay (not New or Recovered)
    decaying = [p for p in decay_pages if p["severity"] in ("Severe", "Moderate", "Mild")]
    decaying.sort(key=lambda x: x["changes"].get("impressions_lost", 0), reverse=True)

    priority = []
    for i, entry in enumerate(decaying[:top_n], 1):
        recs = generate_recommendations(entry)
        top_action = recs[0] if recs else "Review and refresh content."
        priority.append({
            "rank": i,
            "page": entry["page"],
            "severity": entry["severity"],
            "impressions_lost": entry["changes"].get("impressions_lost", 0),
            "clicks_lost": entry["changes"].get("clicks_lost", 0),
            "decay_score": entry["decay_score"],
            "top_action": top_action,
        })
    return priority


# ============================================================================
# SVG CHART
# ============================================================================

def generate_distribution_svg(summary):
    """Generate an SVG bar/donut chart for severity distribution."""
    severe = summary.get("severe", 0)
    moderate = summary.get("moderate", 0)
    mild = summary.get("mild", 0)
    recovered = summary.get("recovered", 0)
    new_pages = summary.get("new_pages", 0)

    segments = [
        ("Severe", severe, "#ef4444"),
        ("Moderate", moderate, "#f59e0b"),
        ("Mild", mild, "#eab308"),
        ("Recovered", recovered, "#10b981"),
        ("New", new_pages, "#3b82f6"),
    ]
    total = sum(s[1] for s in segments) or 1

    # Donut chart
    cx, cy, r_outer, r_inner = 110, 110, 90, 52
    angle = -90  # start at top
    paths = []
    labels = []
    for label, count, color in segments:
        if count == 0:
            continue
        sweep = (count / total) * 360
        start_angle = angle
        end_angle = angle + sweep
        path = _donut_segment(cx, cy, r_outer, r_inner, start_angle, end_angle)
        paths.append(f'<path d="{path}" fill="{color}" stroke="#fff" stroke-width="1.5"/>')
        # Label
        mid_angle = math.radians(start_angle + sweep / 2)
        lx = cx + (r_outer + 18) * math.cos(mid_angle)
        ly = cy + (r_outer + 18) * math.sin(mid_angle)
        labels.append(f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" '
                      f'dominant-baseline="middle" font-size="11" fill="#475569" '
                      f'font-weight="600">{label} ({count})</text>')
        angle = end_angle

    # Center text
    center_text = (f'<text x="{cx}" y="{cy-6}" text-anchor="middle" font-size="22" '
                   f'font-weight="700" fill="#1e293b">{summary.get("total_pages",0)}</text>'
                   f'<text x="{cx}" y="{cy+14}" text-anchor="middle" font-size="10" '
                   f'fill="#64748b">total pages</text>')

    legend_items = []
    for i, (label, count, color) in enumerate(segments):
        ly = 230 + i * 22
        legend_items.append(
            f'<rect x="20" y="{ly}" width="12" height="12" rx="2" fill="{color}"/>'
            f'<text x="40" y="{ly+10}" font-size="12" fill="#334155">{label}: {count}</text>'
        )

    svg = f'''<svg viewBox="0 0 280 360" xmlns="http://www.w3.org/2000/svg" style="max-width:320px;width:100%;">
  {''.join(paths)}
  {center_text}
  {''.join(labels)}
  {''.join(legend_items)}
</svg>'''
    return svg


def _donut_segment(cx, cy, r_outer, r_inner, start_angle, end_angle):
    """Generate an SVG donut segment path."""
    if end_angle - start_angle >= 359.99:
        # Full circle
        return (f"M {cx-r_outer},{cy} A {r_outer},{r_outer} 0 1,1 {cx+r_outer},{cy} "
                f"A {r_outer},{r_outer} 0 1,1 {cx-r_outer},{cy} Z")

    s_rad = math.radians(start_angle)
    e_rad = math.radians(end_angle)
    x1 = cx + r_outer * math.cos(s_rad)
    y1 = cy + r_outer * math.sin(s_rad)
    x2 = cx + r_outer * math.cos(e_rad)
    y2 = cy + r_outer * math.sin(e_rad)
    x3 = cx + r_inner * math.cos(e_rad)
    y3 = cy + r_inner * math.sin(e_rad)
    x4 = cx + r_inner * math.cos(s_rad)
    y4 = cy + r_inner * math.sin(s_rad)

    large_arc = 1 if (end_angle - start_angle) > 180 else 0
    return (f"M {x1:.2f},{y1:.2f} "
            f"A {r_outer},{r_outer} 0 {large_arc},1 {x2:.2f},{y2:.2f} "
            f"L {x3:.2f},{y3:.2f} "
            f"A {r_inner},{r_inner} 0 {large_arc},0 {x4:.2f},{y4:.2f} Z")


# ============================================================================
# HTML REPORT GENERATION
# ============================================================================

def _esc(text):
    if text is None:
        return ""
    return html.escape(str(text))


def _severity_badge(severity):
    color = SEVERITY_LEVELS.get(severity, {}).get("color", "#64748b")
    return f'<span class="badge" style="background:{color}">{_esc(severity)}</span>'


def _fmt_pct(pct):
    """Format a percentage change with arrow and color."""
    if pct is None:
        return '<span class="muted">—</span>'
    if pct > 0:
        return f'<span class="gain">▲ +{pct*100:.1f}%</span>'
    elif pct < 0:
        return f'<span class="loss">▼ {pct*100:.1f}%</span>'
    return '<span class="muted">—</span>'


def _fmt_pos_delta(delta):
    if delta is None:
        return '<span class="muted">—</span>'
    if delta > 0:
        return f'<span class="loss">▼ +{delta:.1f}</span>'
    elif delta < 0:
        return f'<span class="gain">▲ {delta:.1f}</span>'
    return '<span class="muted">—</span>'


def generate_decay_report(domain, decay_data, output_path):
    """
    Generate the full HTML decay report.

    Args:
      domain: target domain
      decay_data: dict from detect_decay() + keyword correlation + priority list
      output_path: directory to write the report

    Returns: path to the HTML report.
    """
    os.makedirs(output_path, exist_ok=True)

    summary = decay_data.get("summary", {})
    pages = decay_data.get("pages", [])
    correlations = decay_data.get("keyword_correlations", [])
    priority = decay_data.get("priority_list", [])
    meta = decay_data.get("meta", {})
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Summary cards
    cards_html = _summary_cards(summary)

    # Distribution chart
    chart_svg = generate_distribution_svg(summary)

    # Decaying pages table (all pages, sorted by traffic loss)
    table_rows = _build_pages_table(pages, correlations)

    # Top 10 losers
    losers = [p for p in pages if p["severity"] in ("Severe", "Moderate", "Mild")]
    losers.sort(key=lambda x: x["changes"].get("impressions_lost", 0), reverse=True)
    losers_html = _build_movers_table(losers[:10], "losers")

    # Top 10 gainers
    gainers = [p for p in pages if p["severity"] in ("Recovered", "New")]
    gainers.sort(key=lambda x: x["changes"].get("impressions_lost", 0))  # least lost = most gained
    gainers_html = _build_movers_table(gainers[:10], "gainers")

    # Keyword correlation section
    kw_html = _build_keyword_correlation(correlations)

    # Per-page recommendations (for all decaying pages)
    recs_html = _build_recommendations(pages)

    # Priority action list
    priority_html = _build_priority_table(priority)

    source_note = ""
    if meta.get("source") == "synthetic":
        source_note = ('<div class="source-note">⚠️ GSC data unavailable — report generated '
                       'from <strong>synthetic sample data</strong> for demonstration. '
                       'Connect GSC to get real data.</div>')

    report_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex,nofollow">
<title>Content Decay Report — {_esc(domain)}</title>
<style>
  :root {{
    --bg: #f8fafc; --card: #ffffff; --border: #e2e8f0;
    --text: #1e293b; --muted: #64748b; --accent: #3b82f6;
    --green: #10b981; --amber: #f59e0b; --red: #ef4444; --yellow: #eab308;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.6;
    padding: 24px; max-width: 1400px; margin: 0 auto;
  }}
  h1 {{ font-size: 1.75rem; font-weight: 700; margin-bottom: 4px; }}
  h2 {{ font-size: 1.25rem; font-weight: 700; margin: 32px 0 12px; }}
  .subtitle {{ color: var(--muted); font-size: 0.875rem; margin-bottom: 24px; }}
  .source-note {{
    background: #fef3c7; border: 1px solid #fcd34d; border-radius: 8px;
    padding: 12px 16px; margin-bottom: 20px; font-size: 0.85rem; color: #92400e;
  }}
  .stats-grid {{
    display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; margin-bottom: 24px;
  }}
  .stat-card {{
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 18px 12px; text-align: center;
  }}
  .stat-card .num {{ font-size: 1.75rem; font-weight: 700; }}
  .stat-card .label {{ font-size: 0.7rem; color: var(--muted); text-transform: uppercase;
    letter-spacing: 0.05em; margin-top: 4px; }}
  .stat-card.total .num {{ color: var(--text); }}
  .stat-card.decay .num {{ color: var(--red); }}
  .stat-card.severe .num {{ color: var(--red); }}
  .stat-card.recovering .num {{ color: var(--green); }}
  .stat-card.new .num {{ color: var(--accent); }}

  .chart-row {{ display: flex; gap: 24px; margin-bottom: 24px; align-items: flex-start; }}
  .chart-card {{
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 20px; flex: 0 0 360px;
  }}
  .chart-card h3 {{ font-size: 0.95rem; margin-bottom: 12px; }}
  .impact-card {{
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 20px; flex: 1;
  }}
  .impact-card h3 {{ font-size: 0.95rem; margin-bottom: 12px; }}
  .impact-row {{ display: flex; justify-content: space-between; padding: 8px 0;
    border-bottom: 1px solid var(--border); font-size: 0.875rem; }}
  .impact-row:last-child {{ border-bottom: none; }}
  .impact-row .val {{ font-weight: 700; }}

  table {{
    width: 100%; border-collapse: collapse; background: var(--card);
    border: 1px solid var(--border); border-radius: 12px; overflow: hidden;
    font-size: 0.8125rem;
  }}
  th {{
    background: #f1f5f9; padding: 10px 12px; text-align: left; font-weight: 600;
    font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em;
    color: var(--muted); border-bottom: 1px solid var(--border); white-space: nowrap;
    position: sticky; top: 0;
  }}
  td {{ padding: 10px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }}
  tr:last-child td {{ border-bottom: none; }}
  tbody tr:hover {{ background: #f8fafc; }}
  .badge {{
    display: inline-block; padding: 3px 10px; border-radius: 20px;
    font-size: 0.7rem; font-weight: 600; color: #fff; white-space: nowrap;
  }}
  .url-cell {{ max-width: 280px; word-break: break-all; }}
  .url-cell a {{ color: var(--accent); text-decoration: none; }}
  .url-cell a:hover {{ text-decoration: underline; }}
  .gain {{ color: var(--green); font-weight: 600; }}
  .loss {{ color: var(--red); font-weight: 600; }}
  .muted {{ color: var(--muted); }}
  .empty {{ text-align: center; padding: 40px; color: var(--muted); }}

  .expand-btn {{
    background: none; border: none; cursor: pointer; color: var(--accent);
    font-size: 0.8rem; padding: 2px 6px;
  }}
  .detail-row {{ display: none; }}
  .detail-row.open {{ display: table-row; }}
  .detail-content {{
    background: #f8fafc; padding: 16px; font-size: 0.8rem;
  }}
  .rec-list {{ margin: 8px 0; padding-left: 20px; }}
  .rec-list li {{ margin-bottom: 6px; }}
  .kw-list {{ margin: 4px 0; padding-left: 20px; }}
  .kw-list li {{ margin-bottom: 4px; }}

  .movers-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
  .mover-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 16px; }}
  .mover-card h3 {{ font-size: 0.95rem; margin-bottom: 10px; }}
  .mover-card.losers h3 {{ color: var(--red); }}
  .mover-card.gainers h3 {{ color: var(--green); }}
  .mover-item {{ padding: 8px 0; border-bottom: 1px solid var(--border); font-size: 0.8rem; }}
  .mover-item:last-child {{ border-bottom: none; }}
  .mover-item .page {{ font-weight: 600; word-break: break-all; }}
  .mover-item .delta {{ float: right; font-weight: 700; }}

  .priority-rank {{
    display: inline-block; width: 28px; height: 28px; line-height: 28px;
    text-align: center; border-radius: 50%; background: var(--accent);
    color: #fff; font-weight: 700; font-size: 0.8rem;
  }}
  .footer {{
    margin-top: 24px; text-align: center; font-size: 0.75rem; color: var(--muted);
  }}
</style>
<script>
  function toggleDetail(id) {{
    var row = document.getElementById(id);
    if (row) row.classList.toggle('open');
  }}
</script>
</head>
<body>
  <h1>📉 Content Decay Detector</h1>
  <p class="subtitle">{_esc(domain)} · Generated {timestamp} · Periods: {meta.get('current_period','')} vs {meta.get('previous_period','')} · Data source: {meta.get('source','gsc').upper()}</p>
  {source_note}

  <div class="stats-grid">
    {cards_html}
  </div>

  <h2>📊 Decay Severity Distribution</h2>
  <div class="chart-row">
    <div class="chart-card">
      <h3>Severity Distribution</h3>
      {chart_svg}
    </div>
    <div class="impact-card">
      <h3>Traffic Impact Summary</h3>
      <div class="impact-row"><span>Total Impressions Lost</span>
        <span class="val loss">{summary.get('total_impressions_lost',0):,}</span></div>
      <div class="impact-row"><span>Total Clicks Lost</span>
        <span class="val loss">{summary.get('total_clicks_lost',0):,}</span></div>
      <div class="impact-row"><span>Pages Decaying</span>
        <span class="val">{summary.get('decaying_pages',0)} / {summary.get('total_pages',0)}</span></div>
      <div class="impact-row"><span>Severely Decaying</span>
        <span class="val loss">{summary.get('severe',0)}</span></div>
      <div class="impact-row"><span>Recovering Pages</span>
        <span class="val gain">{summary.get('recovered',0)}</span></div>
      <div class="impact-row"><span>New Pages</span>
        <span class="val" style="color:var(--accent)">{summary.get('new_pages',0)}</span></div>
    </div>
  </div>

  <h2>📋 Decaying Pages Detail</h2>
  <p class="subtitle" style="margin-bottom:12px;">Sorted by traffic loss · click a row for recommendations &amp; keyword details</p>
  <table>
    <thead>
      <tr>
        <th>Page URL</th>
        <th>Current Imp</th>
        <th>Previous Imp</th>
        <th>% Change</th>
        <th>Cur Pos</th>
        <th>Prev Pos</th>
        <th>Pos Δ</th>
        <th>Severity</th>
        <th>Clicks Δ</th>
        <th>Score</th>
        <th></th>
      </tr>
    </thead>
    <tbody>
      {table_rows}
    </tbody>
  </table>

  <h2>🏆 Top 10 Traffic Losers &amp; Gainers</h2>
  <div class="movers-grid">
    <div class="mover-card losers">
      <h3>🔻 Biggest Traffic Losers</h3>
      {losers_html}
    </div>
    <div class="mover-card gainers">
      <h3>📈 Biggest Traffic Gainers</h3>
      {gainers_html}
    </div>
  </div>

  {kw_html}

  {recs_html}

  <h2>🎯 Priority Action List</h2>
  <p class="subtitle" style="margin-bottom:12px;">Fix these pages first — ranked by traffic impact</p>
  {priority_html}

  <div class="footer">
    Content Decay Detector · Compares GSC page performance across two consecutive periods.
    Severity: Severe (&gt;50% loss) · Moderate (20-50%) · Mild (&lt;20%) · Recovered (gained) · New (new page).
    · Generated by content_decay_detector.py
  </div>
</body>
</html>"""

    html_path = os.path.join(output_path, "content_decay_report.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(report_html)
    return html_path


def _summary_cards(summary):
    """Build the 6 summary cards."""
    cards = [
        ("total", "", "Total Pages"),
        ("decay", "", "Decaying"),
        ("severe", "", "Severe"),
        ("recovering", "", "Recovering"),
        ("new", "", "New Pages"),
    ]
    values = [
        summary.get("total_pages", 0),
        summary.get("decaying_pages", 0),
        summary.get("severe", 0),
        summary.get("recovered", 0),
        summary.get("new_pages", 0),
    ]
    labels = ["Total Pages", "Decaying Pages", "Severely Decaying", "Recovering", "New Pages"]
    classes = ["total", "decay", "severe", "recovering", "new"]
    # 6th card: impressions lost
    html_parts = []
    for i in range(5):
        html_parts.append(
            f'<div class="stat-card {classes[i]}">'
            f'<div class="num">{values[i]:,}</div>'
            f'<div class="label">{labels[i]}</div></div>'
        )
    html_parts.append(
        f'<div class="stat-card decay">'
        f'<div class="num">{summary.get("total_impressions_lost", 0):,}</div>'
        f'<div class="label">Impressions Lost</div></div>'
    )
    return "\n    ".join(html_parts)


def _build_pages_table(pages, correlations):
    """Build the expandable decaying pages table rows."""
    if not pages:
        return '<tr><td colspan="11" class="empty">No page data available.</td></tr>'

    corr_map = {c["page"]: c for c in correlations}
    rows = []
    for i, p in enumerate(pages):
        pid = f"detail-{i}"
        cur = p.get("current", {})
        prev = p.get("previous", {})
        ch = p.get("changes", {})

        page_url = p["page"]
        # Make it a clickable link
        domain_scheme = "https://"
        full_url = page_url if page_url.startswith("http") else f"https://{_esc(page_url)}"
        url_cell = f'<td class="url-cell"><a href="{full_url}" target="_blank">{_esc(page_url)}</a></td>'

        imp_pct = ch.get("impressions_change_pct")
        clk_pct = ch.get("clicks_change_pct")
        pos_delta = ch.get("position_delta")

        cur_pos = cur.get("position", 0)
        prev_pos = prev.get("position", 0)
        cur_pos_str = f"{cur_pos:.1f}" if cur_pos < 999 else ">"
        prev_pos_str = f"{prev_pos:.1f}" if prev_pos < 999 else ">"

        # Keyword correlation for this page
        corr = corr_map.get(page_url, {})
        lost_queries = corr.get("lost_queries", [])
        kw_html = ""
        if lost_queries:
            kw_items = "".join(
                f'<li>{_esc(lq["query"])} — {lq["prev_imp"]:,} → {lq["cur_imp"]:,} '
                f'(<span class="loss">{lq["change_pct"]*100:.0f}%</span>)</li>'
                for lq in lost_queries[:8]
            )
            kw_html = f'<p style="margin:8px 0 4px;font-weight:600;">Keyword Correlation (lost queries):</p><ul class="kw-list">{kw_items}</ul>'
        else:
            kw_html = '<p class="muted" style="margin:8px 0;">No query-level data available for this page.</p>'

        # Recommendations
        recs = generate_recommendations(p)
        rec_items = "".join(f"<li>{_esc(r)}</li>" for r in recs)
        rec_html = f'<p style="margin:8px 0 4px;font-weight:600;">Recommended Actions:</p><ul class="rec-list">{rec_items}</ul>'

        detail_html = f'<td colspan="11" class="detail-content">{kw_html}{rec_html}</td>'

        rows.append(f"""
        <tr>
          {url_cell}
          <td>{cur.get('impressions',0):,}</td>
          <td>{prev.get('impressions',0):,}</td>
          <td>{_fmt_pct(imp_pct)}</td>
          <td>{cur_pos_str}</td>
          <td>{prev_pos_str}</td>
          <td>{_fmt_pos_delta(pos_delta)}</td>
          <td>{_severity_badge(p.get('severity',''))}</td>
          <td>{_fmt_pct(clk_pct)}</td>
          <td>{p.get('decay_score',0):.0f}</td>
          <td><button class="expand-btn" onclick="toggleDetail('{pid}')">▸</button></td>
        </tr>
        <tr id="{pid}" class="detail-row">{detail_html}</tr>""")

    return "\n".join(rows)


def _build_movers_table(movers, kind):
    """Build the top movers card content."""
    if not movers:
        return '<p class="muted">No significant movers detected.</p>'
    items = []
    for p in movers:
        ch = p.get("changes", {})
        lost = ch.get("impressions_lost", 0)
        pct = ch.get("impressions_change_pct", 0)
        if kind == "losers":
            delta_str = f'<span class="loss">-{lost:,} imp ({pct*100:.0f}%)</span>'
        else:
            gained = -ch.get("impressions_change_pct", 0)
            delta_str = f'<span class="gain">+{-lost:,} imp</span>'
        items.append(
            f'<div class="mover-item"><span class="page">{_esc(p["page"])}</span>'
            f'<span class="delta">{delta_str}</span>'
            f'<div class="muted" style="font-size:0.75rem">{_severity_badge(p.get("severity",""))} '
            f'Score: {p.get("decay_score",0):.0f}</div></div>'
        )
    return "\n".join(items)


def _build_keyword_correlation(correlations):
    """Build the keyword correlation section."""
    if not correlations:
        return ""
    rows = []
    for corr in correlations:
        page = corr["page"]
        lost_queries = corr.get("lost_queries", [])
        if not lost_queries:
            continue
        kw_items = "".join(
            f"<tr><td>{_esc(lq['query'])}</td><td>{lq['prev_imp']:,}</td>"
            f"<td>{lq['cur_imp']:,}</td><td class='loss'>{lq['change_pct']*100:.0f}%</td>"
            f"<td>{lq['imp_lost']:,}</td></tr>"
            for lq in lost_queries[:10]
        )
        rows.append(f"""
    <h3 style="font-size:0.95rem;margin:16px 0 8px;">🔗 {_esc(page)}</h3>
    <table>
      <thead><tr><th>Query</th><th>Prev Imp</th><th>Cur Imp</th><th>Change</th><th>Imp Lost</th></tr></thead>
      <tbody>{kw_items}</tbody>
    </table>""")
    if not rows:
        return ""
    return f'\n  <h2>🔍 Keyword Correlation — Queries Losing Impressions</h2>\n  <p class="subtitle" style="margin-bottom:12px;">For top decaying pages, which queries lost the most impressions</p>\n  {"".join(rows)}'


def _build_recommendations(pages):
    """Build the per-page recommendations section (decaying pages only)."""
    decaying = [p for p in pages if p["severity"] in ("Severe", "Moderate", "Mild")]
    if not decaying:
        return ""
    items = []
    for p in decaying:
        recs = generate_recommendations(p)
        rec_items = "".join(f"<li>{_esc(r)}</li>" for r in recs)
        items.append(f"""
    <div style="margin-bottom:16px;padding:14px;background:var(--card);border:1px solid var(--border);border-radius:10px;">
      <div style="font-weight:700;margin-bottom:6px;">{_esc(p['page'])} {_severity_badge(p['severity'])} <span class='muted' style='font-weight:400;font-size:0.8rem;'>Score: {p['decay_score']:.0f} · Imp lost: {p['changes'].get('impressions_lost',0):,}</span></div>
      <ul class="rec-list">{rec_items}</ul>
    </div>""")
    return f'\n  <h2>💡 Per-Page Recommendations</h2>\n  <p class="subtitle" style="margin-bottom:12px;">Actionable recommendations for each decaying page</p>\n  {"".join(items)}'


def _build_priority_table(priority):
    """Build the priority action list table."""
    if not priority:
        return '<p class="muted">No decaying pages to prioritize.</p>'
    rows = []
    for p in priority:
        rows.append(f"""
      <tr>
        <td><span class="priority-rank">{p['rank']}</span></td>
        <td class="url-cell"><a href="https://{_esc(p['page'])}" target="_blank">{_esc(p['page'])}</a></td>
        <td>{_severity_badge(p['severity'])}</td>
        <td class="loss">{p['impressions_lost']:,}</td>
        <td class="loss">{p['clicks_lost']:,}</td>
        <td>{p['decay_score']:.0f}</td>
        <td>{_esc(p['top_action'])}</td>
      </tr>""")
    return f"""
  <table>
    <thead>
      <tr><th>#</th><th>Page</th><th>Severity</th><th>Imp Lost</th><th>Clicks Lost</th><th>Score</th><th>Top Action</th></tr>
    </thead>
    <tbody>
      {"".join(rows)}
    </tbody>
  </table>"""


# ============================================================================
# CLI
# ============================================================================

def run_analysis(domain, days_back=28, output_path=None):
    """Run the full analysis pipeline and generate the report."""
    print(f"\n{'='*60}")
    print(f"CONTENT DECAY DETECTOR — {domain}")
    print(f"{'='*60}")

    # Step 1: Fetch GSC data
    print(f"\n[1/4] Fetching GSC data ({days_back}d periods)...")
    gsc_data = fetch_gsc_data(domain, days_back=days_back)

    # Step 2: Detect decay
    print(f"\n[2/4] Detecting content decay...")
    decay_result = detect_decay(
        gsc_data["current_period"],
        gsc_data["previous_period"],
    )
    summary = decay_result["summary"]
    print(f"  Total pages:        {summary['total_pages']}")
    print(f"  Decaying pages:     {summary['decaying_pages']}")
    print(f"  Severe:             {summary['severe']}")
    print(f"  Moderate:           {summary['moderate']}")
    print(f"  Mild:               {summary['mild']}")
    print(f"  Recovered:          {summary['recovered']}")
    print(f"  New pages:          {summary['new_pages']}")
    print(f"  Impressions lost:   {summary['total_impressions_lost']:,}")
    print(f"  Clicks lost:        {summary['total_clicks_lost']:,}")

    # Step 3: Keyword correlation
    print(f"\n[3/4] Correlating keyword losses...")
    correlations = correlate_keywords(
        decay_result["pages"],
        gsc_data.get("queries_current", []),
        gsc_data.get("queries_previous", []),
        top_n=10,
    )

    # Priority list
    priority = build_priority_list(decay_result["pages"], top_n=20)

    # Step 4: Generate report
    print(f"\n[4/4] Generating report...")
    full_data = {
        "domain": domain,
        "meta": {
            "source": gsc_data.get("source", "gsc"),
            "days_back": days_back,
            "current_period": f"{gsc_data['current_period']['start']} → {gsc_data['current_period']['end']}",
            "previous_period": f"{gsc_data['previous_period']['start']} → {gsc_data['previous_period']['end']}",
            "site_url": gsc_data.get("site_url", ""),
        },
        "summary": summary,
        "pages": decay_result["pages"],
        "keyword_correlations": correlations,
        "priority_list": priority,
    }

    if not output_path:
        output_path = os.path.expanduser(f"~/content_decay_{domain.replace('.', '_')}")

    html_path = generate_decay_report(domain, full_data, output_path)

    # Write raw JSON
    json_path = os.path.join(output_path, "content_decay_data.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(full_data, f, indent=2, ensure_ascii=False, default=str)

    # Console summary
    print(f"\n{'='*60}")
    print(f"DECAY SUMMARY — {domain}")
    print(f"{'='*60}")
    print(f"  Data source:    {gsc_data.get('source','gsc').upper()}")
    print(f"  Total pages:    {summary['total_pages']}")
    print(f"  Decaying:       {summary['decaying_pages']} (Severe: {summary['severe']}, Moderate: {summary['moderate']}, Mild: {summary['mild']})")
    print(f"  Recovering:     {summary['recovered']}")
    print(f"  New pages:      {summary['new_pages']}")
    print(f"  Imp lost:       {summary['total_impressions_lost']:,}")
    print(f"  Clicks lost:    {summary['total_clicks_lost']:,}")

    if priority:
        print(f"\nTOP 5 PRIORITY ACTIONS:")
        for p in priority[:5]:
            print(f"  {p['rank']}. [{p['severity']}] {p['page']} "
                  f"(lost {p['impressions_lost']:,} imp, {p['clicks_lost']:,} clicks)")

    print(f"\nReport: {html_path}")
    print(f"JSON:   {json_path}")
    print(f"{'='*60}")

    return {
        "html_path": html_path,
        "json_path": json_path,
        "summary": summary,
        "source": gsc_data.get("source", "gsc"),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Detect content decay by comparing GSC data across two time periods."
    )
    parser.add_argument("--domain", required=True,
                        help="Target domain (e.g. softprowatersystems.com)")
    parser.add_argument("--output", required=True,
                        help="Output directory for report + JSON")
    parser.add_argument("--days-back", type=int, default=28,
                        help="Days per period (default: 28)")
    args = parser.parse_args()

    result = run_analysis(args.domain, days_back=args.days_back, output_path=args.output)
    return result


if __name__ == "__main__":
    main()
