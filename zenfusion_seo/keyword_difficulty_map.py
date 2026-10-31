#!/usr/bin/env python3
"""
Keyword Difficulty Map — standalone CLI
========================================
Creates a market-wide difficulty map showing where to compete.

Pipeline:
  1. fetch_keyword_difficulty()    — DataForSEO bulk_keyword_difficulty + keyword_ideas (volume)
  2. classify_difficulty()         — Easy / Moderate / Hard / Very Hard
  3. compute_winnability()          — difficulty × domain authority vs SERP × existing rankings × content gap
  4. generate_difficulty_report()  — HTML (white SaaS theme) with SVG charts + tables + action list

Usage:
  python3 keyword_difficulty_map.py --seed 'water softener' --domain softprowatersystems.com \
    --keywords 'best water softener,best water softener system,water softener,...' \
    --output /tmp/test_difficulty_map

Environment:
  DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD  in ~/.hermes/.env
  Optional KE cache: /tmp/ke_cache.json   ({"keyword": {vol,cpc,competition}})
"""

try:  # resolve google.com/goto passthrough URLs in SERP responses (see goto_resolver.py)
    from .goto_resolver import fix_goto_urls as _fix_goto
except ImportError:
    try:
        from goto_resolver import fix_goto_urls as _fix_goto
    except ImportError:
        def _fix_goto(data, endpoint=None):
            return data
import os
import re
import sys
import json
import time
import math
import base64
import argparse
import datetime
from collections import defaultdict, Counter

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

# ---------------------------------------------------------------------------
# Config / credentials
# ---------------------------------------------------------------------------

DEFAULTS = {
    "location_name": "United States",
    "language_code": "en",
    "location_code": 2840,        # US (DataForSEO numeric code)
}

KE_CACHE_PATH = "/tmp/ke_cache.json"
ENV_PATH = os.path.expanduser("~/.hermes/.env")

# Known high-authority domains (mega-brands) — used for SERP competitor strength heuristic
BIG_BRANDS = {
    "amazon.com", "wikipedia.org", "youtube.com", "reddit.com", "facebook.com",
    "instagram.com", "twitter.com", "x.com", "linkedin.com", "pinterest.com",
    "ebay.com", "walmart.com", "target.com", "homedepot.com", "lowes.com",
    "costco.com", "bestbuy.com", "consumerreports.org", "forbes.com", "nytimes.com",
    "webmd.com", "healthline.com", "mayoclinic.org", "medlineplus.gov",
    "yelp.com", "angi.com", "bbb.org", "trustpilot.com", "consumerreports.org",
    "wikihow.com", "quora.com", "medium.com", "buzzfeed.com",
}

# ---------------------------------------------------------------------------
# .env + API plumbing  (mirrors content_plan_builder.py)
# ---------------------------------------------------------------------------

def load_env(path=ENV_PATH):
    """Populate os.environ from a flat KEY=VALUE .env file if not already set."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and k not in os.environ:
                os.environ[k] = v


def get_creds():
    login = os.environ.get("DATAFORSEO_LOGIN")
    password = os.environ.get("DATAFORSEO_PASSWORD")
    if not login or not password:
        load_env()
        login = os.environ.get("DATAFORSEO_LOGIN")
        password = os.environ.get("DATAFORSEO_PASSWORD")
    if not login or not password:
        sys.stderr.write("ERROR: DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD not found in env or ~/.hermes/.env\n")
        sys.exit(2)
    return login, password


def _auth_header():
    login, password = get_creds()
    raw = f"{login}:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def dfs_post(path, task, timeout=60, retries=2):
    """POST one task to a DataForSEO endpoint; return result dict or None."""
    if requests is None:
        sys.stderr.write("requests library not installed\n")
        return None
    url = "https://api.dataforseo.com/v3/" + path.lstrip("/")
    headers = {"Authorization": _auth_header(), "Content-Type": "application/json"}
    body = json.dumps([task]).encode()
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, data=body, headers=headers, timeout=timeout)
            data = _fix_goto(r.json(), path)
            if r.status_code == 401 or data.get("status_code") == 40101:
                sys.stderr.write("DataForSEO 401 — bad credentials.\n")
                return None
            if data.get("status_code") not in (20000, 20001):
                sys.stderr.write(
                    f"DataForSEO {path} status {data.get('status_code')}: "
                    f"{data.get('status_message', '')}\n"
                )
                if attempt < retries:
                    time.sleep(1.5)
                    continue
                return None
            tasks = data.get("tasks") or []
            if not tasks:
                return None
            res = tasks[0].get("result") or []
            if not res:
                return None
            return res[0]
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.5)
                continue
    sys.stderr.write(f"DataForSEO {path} request failed: {last_err}\n")
    return None


# ---------------------------------------------------------------------------
# KE cache (prepaid volume/CPC — use first when available)
# ---------------------------------------------------------------------------

_ke_cache = None


def load_ke_cache(path=KE_CACHE_PATH):
    global _ke_cache
    if _ke_cache is not None:
        return _ke_cache
    if os.path.exists(path):
        try:
            with open(path) as f:
                _ke_cache = json.load(f)
        except Exception as e:
            sys.stderr.write(f"KE cache load failed: {e}\n")
            _ke_cache = {}
    else:
        _ke_cache = {}
    return _ke_cache


def ke_lookup(keyword):
    """Return (volume, cpc) from KE cache if present, else (None, None)."""
    cache = load_ke_cache()
    rec = cache.get(keyword) or cache.get(keyword.lower())
    if not rec:
        return None, None
    vol = rec.get("vol") or rec.get("volume")
    cpc = rec.get("cpc")
    if isinstance(cpc, dict):
        cpc = cpc.get("value")
    return vol, cpc


# ---------------------------------------------------------------------------
# 1. fetch_keyword_difficulty
# ---------------------------------------------------------------------------

def fetch_keyword_difficulty(keywords, location_code=2840):
    """
    For each keyword, fetch difficulty score and search volume from DataForSEO.

    Uses two endpoints:
      - bulk_keyword_difficulty/live  → keyword_difficulty (0-100)
      - keyword_ideas/live            → search_volume, cpc, competition

    Falls back to KE cache for volume when available (prepaid, free).

    Returns: list of dicts
        {keyword, difficulty, volume, cpc, competition}
    """
    if not keywords:
        return []

    location_name = DEFAULTS["location_name"]
    language_code = DEFAULTS["language_code"]
    kws = [k.strip().lower() for k in keywords if k.strip()]
    if not kws:
        return []

    # --- difficulty (bulk, chunks of <=1000) ---
    diff_map = {}
    for i in range(0, len(kws), 1000):
        chunk = kws[i:i + 1000]
        result = dfs_post("dataforseo_labs/google/bulk_keyword_difficulty/live", {
            "keywords": chunk,
            "location_name": location_name,
            "language_code": language_code,
        }, timeout=90)
        if result and isinstance(result.get("items"), list):
            for it in result["items"]:
                k = (it.get("keyword") or "").strip().lower()
                if k:
                    diff_map[k] = it.get("keyword_difficulty")

    # --- volume via keywords_data search_volume API (direct lookup per keyword) ---
    vol_map = {}
    for kw in kws:
        ke_vol, ke_cpc = ke_lookup(kw)
        if ke_vol:
            vol_map[kw] = {"volume": ke_vol, "cpc": ke_cpc}
            continue
        # Try DataForSEO search_volume endpoint for this specific keyword
        result = dfs_post("keywords_data/google_ads/search_volume/live", {
            "keywords": [kw],
            "location_code": location_code,
            "language_code": language_code,
        }, timeout=60)
        if result:
            # search_volume/live returns {keyword, search_volume, cpc, ...} at top level
            sv = result.get("search_volume")
            if sv is not None:
                vol_map[kw] = {
                    "volume": sv,
                    "cpc": result.get("cpc"),
                    "competition": result.get("competition"),
                }
            else:
                # fallback: check items array
                items = result.get("items") or []
                if isinstance(items, list) and items:
                    item = items[0]
                    vol_map[kw] = {
                        "volume": item.get("search_volume") or 0,
                        "cpc": item.get("cpc"),
                        "competition": item.get("competition"),
                    }
        time.sleep(0.3)  # be gentle on rate limits

    # --- assemble output ---
    out = []
    for kw in kws:
        diff = diff_map.get(kw)
        if diff is None:
            diff = 50  # neutral fallback
        vol_info = vol_map.get(kw, {})
        out.append({
            "keyword": kw,
            "difficulty": round(diff, 1),
            "volume": vol_info.get("volume") or 0,
            "cpc": vol_info.get("cpc"),
            "competition": vol_info.get("competition"),
        })
    return out


# ---------------------------------------------------------------------------
# 2. classify_difficulty
# ---------------------------------------------------------------------------

def classify_difficulty(keywords):
    """
    Classify each keyword by difficulty tier:
      Easy      (0-30)
      Moderate  (31-50)
      Hard      (51-70)
      Very Hard (71-100)

    `keywords` is a list of dicts (as returned by fetch_keyword_difficulty).
    Adds 'difficulty_tier' key to each dict and returns the same list.
    """
    for kw in keywords:
        d = kw.get("difficulty", 50)
        if d <= 30:
            kw["difficulty_tier"] = "Easy"
        elif d <= 50:
            kw["difficulty_tier"] = "Moderate"
        elif d <= 70:
            kw["difficulty_tier"] = "Hard"
        else:
            kw["difficulty_tier"] = "Very Hard"
    return keywords


# ---------------------------------------------------------------------------
# 3. compute_winnability
# ---------------------------------------------------------------------------

def _is_big_brand(domain):
    """Check if a domain is a known high-authority mega-brand."""
    d = (domain or "").lower().lstrip("www.")
    for brand in BIG_BRANDS:
        if d == brand or d.endswith("." + brand):
            return True
    return False


def _get_domain_metrics(domain):
    """
    Fetch domain authority proxy metrics from DataForSEO backlinks summary.
    Returns: {referring_domains, backlinks, backlinks_dofollow} or {}
    """
    if not domain:
        return {}
    result = dfs_post("backlinks/summary/live", {
        "target": domain,
        "main_domain": domain,
        "search_mode": "as_is",
    }, timeout=60)
    if not result:
        return {}
    # backlinks/summary/live returns metrics at the top level (not in items array)
    return {
        "referring_domains": result.get("referring_domains", 0) or 0,
        "backlinks": result.get("backlinks", 0) or 0,
        "backlinks_dofollow": result.get("backlinks_dofollow", 0) or 0,
        "referring_main_domains": result.get("referring_main_domains", 0) or 0,
    }


def _get_serp(keyword, location_code=2840):
    """Fetch live SERP results for a keyword. Returns list of organic items."""
    result = dfs_post("serp/google/organic/live/advanced", {
        "keyword": keyword,
        "location_code": location_code,
        "language_code": DEFAULTS["language_code"],
        "device": "desktop",
        "os": "windows",
    }, timeout=60)
    if not result or not isinstance(result.get("items"), list):
        return []
    return [it for it in result["items"] if it.get("type") == "organic"]


def compute_winnability(keyword, domain, keyword_data=None, domain_metrics=None, serp_items=None):
    """
    Compute winnability score (0-100) for a keyword, factoring in:
      - difficulty (40% weight)
      - domain authority vs SERP competitors (30% weight)
      - existing rankings (20% weight)
      - content gap opportunity (10% weight)

    Parameters:
      keyword       — the keyword string
      domain        — target domain (e.g. softprowatersystems.com)
      keyword_data  — dict with at least {difficulty, volume} (fetched if not provided)
      domain_metrics — backlinks summary dict (fetched if not provided)
      serp_items     — list of SERP organic items (fetched if not provided)

    Returns: dict with winnability score and component breakdown.
    """
    if keyword_data is None:
        keyword_data = {"difficulty": 50, "volume": 0}
    difficulty = keyword_data.get("difficulty", 50)

    # --- Fetch domain metrics if not provided ---
    if domain_metrics is None:
        domain_metrics = _get_domain_metrics(domain) if domain else {}

    # --- Fetch SERP if not provided ---
    if serp_items is None:
        serp_items = _get_serp(keyword) if domain else []

    # --- Component 1: Base score from difficulty (40%) ---
    base_score = (100 - difficulty) * 0.40

    # --- Component 2: Domain authority vs SERP competitors (30%) ---
    # Domain authority: normalize referring_domains to 0-100 via log scale
    ref_domains = domain_metrics.get("referring_domains", 0) or 0
    domain_auth = min(100, math.log(1 + ref_domains) / math.log(1 + 5000) * 100)

    # SERP competitor strength: count big brands in top 10
    comp_domains = [s.get("domain", "") for s in serp_items[:10]]
    big_brand_count = sum(1 for d in comp_domains if _is_big_brand(d))
    total_competitors = max(len(comp_domains), 1)
    comp_strength = (big_brand_count / total_competitors) * 100

    # Authority score: domain authority penalized by competitor strength
    auth_score = domain_auth * (1 - comp_strength / 200)
    auth_component = max(0, min(100, auth_score)) * 0.30

    # --- Component 3: Existing rankings (20%) ---
    domain_rank = None
    domain_lower = (domain or "").lower()
    for s in serp_items:
        serp_domain = (s.get("domain") or "").lower()
        if domain_lower and (serp_domain == domain_lower or
                             serp_domain.endswith("." + domain_lower) or
                             domain_lower.endswith("." + serp_domain) or
                             serp_domain.lstrip("www.") == domain_lower.lstrip("www.")):
            domain_rank = s.get("rank_group")
            break

    if domain_rank and domain_rank <= 3:
        rank_score = 100
    elif domain_rank and domain_rank <= 10:
        rank_score = 90 - (domain_rank - 3) * 5  # 85 for #4, down to 45 for #10
    elif domain_rank and domain_rank <= 20:
        rank_score = 40
    elif domain_rank and domain_rank <= 50:
        rank_score = 20
    elif domain_rank and domain_rank <= 100:
        rank_score = 10
    else:
        rank_score = 0

    rank_component = rank_score * 0.20

    # --- Component 4: Content gap opportunity (10%) ---
    if domain_rank is None and difficulty <= 30:
        gap_score = 90   # high opportunity: low difficulty + no content
    elif domain_rank is None and difficulty <= 50:
        gap_score = 70   # good opportunity
    elif domain_rank is None:
        gap_score = 30   # harder, but still a gap
    else:
        gap_score = 50   # already has content

    gap_component = gap_score * 0.10

    # --- Final winnability ---
    winnability = base_score + auth_component + rank_component + gap_component
    winnability = max(0, min(100, round(winnability, 1)))

    return {
        "keyword": keyword,
        "difficulty": difficulty,
        "volume": keyword_data.get("volume", 0),
        "winnability": winnability,
        "domain_auth": round(domain_auth, 1),
        "comp_strength": round(comp_strength, 1),
        "comp_domains": comp_domains[:10],
        "big_brand_count": big_brand_count,
        "existing_rank": domain_rank,
        "rank_score": rank_score,
        "gap_score": gap_score,
        "base_score": round(base_score, 1),
        "auth_component": round(auth_component, 1),
        "rank_component": round(rank_component, 1),
        "gap_component": round(gap_component, 1),
    }


# ---------------------------------------------------------------------------
# 4. generate_difficulty_report  (HTML + SVG charts)
# ---------------------------------------------------------------------------

def _html_escape(text):
    """Escape HTML special characters."""
    if text is None:
        return ""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "seed"


def _now():
    return datetime.datetime.now()


# ---- SVG chart generators ----

def _svg_difficulty_distribution(keywords, width=600, height=280):
    """Bar chart showing how many keywords fall in each difficulty band."""
    tiers = ["Easy", "Moderate", "Hard", "Very Hard"]
    colors = ["#16A34A", "#2563EB", "#D97706", "#DC2626"]
    counts = [0, 0, 0, 0]
    for kw in keywords:
        tier = kw.get("difficulty_tier", "Moderate")
        if tier in tiers:
            counts[tiers.index(tier)] += 1

    max_count = max(counts) if counts else 1
    chart_h = height - 60
    bar_w = 80
    gap = 40
    start_x = 60
    base_y = height - 40

    svg = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
           f'style="max-width:{width}px;width:100%;height:auto;">']
    # Y-axis grid
    for i in range(5):
        y = base_y - (chart_h * i / 4)
        val = int(max_count * i / 4)
        svg.append(f'<line x1="{start_x}" y1="{y:.0f}" x2="{width - 20}" y2="{y:.0f}" '
                   f'stroke="#E5E7EB" stroke-width="1"/>')
        svg.append(f'<text x="{start_x - 8}" y="{y + 4:.0f}" text-anchor="end" '
                   f'font-size="11" fill="#6B7280">{val}</text>')

    # Bars
    for i, (tier, count, color) in enumerate(zip(tiers, counts, colors)):
        x = start_x + i * (bar_w + gap) + 10
        h = (count / max_count * chart_h) if max_count > 0 else 0
        y = base_y - h
        svg.append(f'<rect x="{x}" y="{y:.0f}" width="{bar_w}" height="{h:.0f}" '
                   f'fill="{color}" rx="4" opacity="0.85"/>')
        svg.append(f'<text x="{x + bar_w / 2}" y="{y - 6:.0f}" text-anchor="middle" '
                   f'font-size="13" font-weight="700" fill="#1F2937">{count}</text>')
        svg.append(f'<text x="{x + bar_w / 2}" y="{base_y + 18}" text-anchor="middle" '
                   f'font-size="12" fill="#374151">{tier}</text>')

    # Title
    svg.append(f'<text x="{width / 2}" y="20" text-anchor="middle" '
               f'font-size="14" font-weight="600" fill="#1F2937">'
               f'Keyword Difficulty Distribution</text>')

    svg.append('</svg>')
    return "\n".join(svg)


def _svg_scatter_vol_diff(keywords, width=600, height=400):
    """Scatter plot: difficulty (x) vs volume (y), colored by winnability."""
    if not keywords:
        return '<svg viewBox="0 0 600 400" xmlns="http://www.w3.org/2000/svg"></svg>'

    margin_l, margin_r, margin_t, margin_b = 50, 20, 30, 40
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b

    max_diff = 100
    max_vol = max((kw.get("volume", 0) for kw in keywords), default=1) or 1
    # Use log scale for volume if range is large
    use_log = max_vol > 1000

    def x_pos(d):
        return margin_l + (d / max_diff) * plot_w

    def y_pos(v):
        if use_log:
            vv = math.log(1 + v) / math.log(1 + max_vol)
        else:
            vv = v / max_vol if max_vol > 0 else 0
        return margin_t + plot_h - vv * plot_h

    def winnability_color(w):
        if w >= 70:
            return "#16A34A"   # green — high winnability
        elif w >= 50:
            return "#2563EB"   # blue — moderate
        elif w >= 30:
            return "#D97706"   # amber — low
        else:
            return "#DC2626"   # red — very low

    svg = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
           f'style="max-width:{width}px;width:100%;height:auto;">']

    # Title
    svg.append(f'<text x="{width / 2}" y="18" text-anchor="middle" '
               f'font-size="14" font-weight="600" fill="#1F2937">'
               f'Volume vs Difficulty (colored by winnability)</text>')

    # Grid lines
    for i in range(5):
        x = margin_l + (plot_w * i / 4)
        val = int(max_diff * i / 4)
        svg.append(f'<line x1="{x:.0f}" y1="{margin_t}" x2="{x:.0f}" y2="{margin_t + plot_h}" '
                   f'stroke="#E5E7EB" stroke-width="1"/>')
        svg.append(f'<text x="{x:.0f}" y="{margin_t + plot_h + 18}" text-anchor="middle" '
                   f'font-size="11" fill="#6B7280">{val}</text>')

    for i in range(5):
        y = margin_t + (plot_h * i / 4)
        if use_log:
            val = int(max_vol ** (i / 4))
        else:
            val = int(max_vol * (4 - i) / 4)
        svg.append(f'<line x1="{margin_l}" y1="{y:.0f}" x2="{margin_l + plot_w}" y2="{y:.0f}" '
                   f'stroke="#E5E7EB" stroke-width="1"/>')
        svg.append(f'<text x="{margin_l - 8}" y="{y + 4:.0f}" text-anchor="end" '
                   f'font-size="11" fill="#6B7280">{val:,}</text>')

    # Axis labels
    svg.append(f'<text x="{margin_l + plot_w / 2}" y="{height - 5}" text-anchor="middle" '
               f'font-size="12" fill="#374151">Difficulty Score (0-100)</text>')
    svg.append(f'<text x="15" y="{margin_t + plot_h / 2}" text-anchor="middle" '
               f'font-size="12" fill="#374151" transform="rotate(-90 15 {margin_t + plot_h / 2})">'
               f'Search Volume</text>')

    # Data points
    for kw in keywords:
        d = kw.get("difficulty", 50)
        v = kw.get("volume", 0)
        w = kw.get("winnability", 50)
        cx = x_pos(d)
        cy = y_pos(v)
        color = winnability_color(w)
        r = 5 if v > 0 else 3
        svg.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r}" fill="{color}" '
                   f'opacity="0.8" stroke="#fff" stroke-width="1">'
                   f'<title>{_html_escape(kw.get("keyword", ""))}: diff={d}, vol={v:,}, win={w}</title>'
                   f'</circle>')

    # Legend
    legend_y = height - 22
    legend_items = [
        ("High winnability (70+)", "#16A34A"),
        ("Moderate (50-69)", "#2563EB"),
        ("Low (30-49)", "#D97706"),
        ("Very low (<30)", "#DC2626"),
    ]
    lx = margin_l
    for label, color in legend_items:
        svg.append(f'<circle cx="{lx}" cy="{legend_y}" r="4" fill="{color}"/>')
        svg.append(f'<text x="{lx + 8}" y="{legend_y + 4}" font-size="10" fill="#6B7280">{label}</text>')
        lx += len(label) * 5.5 + 28

    svg.append('</svg>')
    return "\n".join(svg)


# ---- HTML report ----

HTML_TMPL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="robots" content="noindex,nofollow">
<title>Keyword Difficulty Map — {seed}</title>
<style>
  :root {{ --primary:#1F2937; --accent:#2563EB; --success:#16A34A; --warn:#D97706;
          --danger:#DC2626; --bg:#F3F4F6; --muted:#6B7280; --grid:#E5E7EB; --soft:#EFF6FF; }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
         margin:0; color:var(--primary); background:#fff; line-height:1.5; }}
  .wrap {{ max-width:1100px; margin:0 auto; padding:32px; }}
  header {{ border-bottom:3px solid var(--accent); padding-bottom:16px; margin-bottom:24px; }}
  h1 {{ margin:0; font-size:28px; }}
  .meta {{ color:var(--muted); font-size:13px; margin-top:4px; }}
  h2 {{ color:var(--accent); border-bottom:1px solid var(--grid); padding-bottom:6px; margin-top:36px; }}
  h3 {{ color:var(--primary); margin-top:24px; font-size:16px; }}
  .kpis {{ display:flex; gap:12px; flex-wrap:wrap; margin:16px 0; }}
  .kpi {{ background:var(--bg); border-top:3px solid var(--accent); border-radius:6px;
          padding:14px 18px; min-width:140px; flex:1; }}
  .kpi .label {{ font-size:11px; text-transform:uppercase; color:var(--muted); letter-spacing:.04em; }}
  .kpi .value {{ font-size:22px; font-weight:700; margin-top:4px; }}
  .kpi .sub {{ font-size:11px; color:var(--muted); }}
  .kpi.easy {{ border-top-color:var(--success); }}
  .kpi.mod {{ border-top-color:var(--accent); }}
  .kpi.hard {{ border-top-color:var(--warn); }}
  .kpi.vhard {{ border-top-color:var(--danger); }}
  table {{ width:100%; border-collapse:collapse; margin:12px 0; font-size:13px; }}
  th {{ background:var(--primary); color:#fff; text-align:left; padding:8px 10px; font-weight:600; }}
  td {{ padding:8px 10px; border-bottom:1px solid var(--grid); vertical-align:top; }}
  tr:nth-child(even) td {{ background:var(--bg); }}
  .tier {{ display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600; color:#fff; }}
  .tier-Easy {{ background:var(--success); }}
  .tier-Moderate {{ background:var(--accent); }}
  .tier-Hard {{ background:var(--warn); }}
  .tier-Very\\\\ Hard {{ background:var(--danger); }}
  .win-bar {{ display:inline-block; width:60px; height:8px; background:var(--grid); border-radius:4px;
              vertical-align:middle; overflow:hidden; }}
  .win-bar-fill {{ height:100%; border-radius:4px; }}
  .chart-box {{ background:var(--bg); border-radius:8px; padding:20px; margin:16px 0; text-align:center; }}
  .insight {{ background:var(--soft); border-left:3px solid var(--accent); padding:10px 14px;
             font-style:italic; margin:16px 0; border-radius:0 4px 4px 0; }}
  .action-item {{ border:1px solid var(--grid); border-radius:6px; padding:12px 16px; margin:10px 0;
                  display:flex; gap:12px; align-items:flex-start; }}
  .action-num {{ flex-shrink:0; width:28px; height:28px; border-radius:50%; background:var(--accent);
                 color:#fff; font-weight:700; display:flex; align-items:center; justify-content:center;
                 font-size:13px; }}
  .action-body {{ flex:1; }}
  .action-body .title {{ font-weight:600; font-size:14px; }}
  .action-body .desc {{ font-size:12px; color:var(--muted); margin-top:2px; }}
  .priority-tag {{ display:inline-block; padding:1px 7px; border-radius:8px; font-size:10px; font-weight:600;
                    margin-left:8px; }}
  .priority-High {{ background:#FEE2E2; color:#991B1B; }}
  .priority-Medium {{ background:#FEF3C7; color:#92400E; }}
  .priority-Low {{ background:#DBEAFE; color:#1E40AF; }}
  footer {{ margin-top:40px; padding-top:16px; border-top:1px solid var(--grid);
           color:var(--muted); font-size:11px; text-align:center; }}
  code {{ background:var(--bg); padding:1px 4px; border-radius:3px; font-size:12px; }}
</style></head><body><div class="wrap">
{body}
<footer>Generated {date_generated} &bull; DataForSEO data &bull; White-label difficulty map &bull; <code>noindex,nofollow</code></footer>
</div></body></html>"""


def _win_bar(winnability):
    """Small inline bar for winnability."""
    if winnability >= 70:
        color = "#16A34A"
    elif winnability >= 50:
        color = "#2563EB"
    elif winnability >= 30:
        color = "#D97706"
    else:
        color = "#DC2626"
    return (f'<span class="win-bar"><span class="win-bar-fill" style="width:{winnability}%;'
            f'background:{color};"></span></span> <b>{winnability}</b>')


def _tier_badge(tier):
    cls = "tier-" + (tier or "Moderate").replace(" ", "\\ ")
    return f'<span class="tier {cls}">{tier}</span>'


def generate_difficulty_report(seed_keyword, keywords, analysis, output_path):
    """
    Generate a full HTML difficulty report.

    Parameters:
      seed_keyword  — the seed keyword used for this analysis
      keywords      — list of keyword dicts with difficulty, volume, tier, winnability
      analysis      — dict with market overview stats, domain_metrics, action_list
      output_path   — directory to write the HTML file into

    Returns: path to the generated HTML file.
    """
    os.makedirs(output_path, exist_ok=True)
    date_gen = _now().strftime("%Y-%m-%d")
    period = _now().strftime("%Y-%m")
    slug = _slug(seed_keyword)
    domain = analysis.get("domain", "")

    body = []

    # ---- Header ----
    body.append(f"<header><h1>Keyword Difficulty Map — {_html_escape(seed_keyword)}</h1>")
    body.append(f'<div class="meta">{period} &bull; Domain: <b>{_html_escape(domain)}</b> '
                f'&bull; {len(keywords)} keywords analyzed</div></header>')

    # ---- KPIs (market overview) ----
    overview = analysis.get("overview", {})
    avg_diff = overview.get("avg_difficulty", 0)
    dist = overview.get("distribution", {"Easy": 0, "Moderate": 0, "Hard": 0, "Very Hard": 0})
    total_vol = overview.get("total_volume", 0)
    avg_win = overview.get("avg_winnability", 0)

    body.append('<div class="kpis">')
    for label, val, sub, cls in [
        ("Avg Difficulty", f"{avg_diff:.1f}", "market average", ""),
        ("Total Volume", f"{total_vol:,}", "monthly searches", ""),
        ("Avg Winnability", f"{avg_win:.1f}%", "for " + _html_escape(domain), ""),
        ("Keywords", str(len(keywords)), "analyzed", ""),
    ]:
        body.append(f'<div class="kpi {cls}"><div class="label">{label}</div>'
                    f'<div class="value">{val}</div><div class="sub">{sub}</div></div>')
    body.append('</div>')

    # Difficulty tier KPIs
    body.append('<div class="kpis">')
    tier_colors = {"Easy": "easy", "Moderate": "mod", "Hard": "hard", "Very Hard": "vhard"}
    for tier in ["Easy", "Moderate", "Hard", "Very Hard"]:
        count = dist.get(tier, 0)
        cls = tier_colors.get(tier, "")
        body.append(f'<div class="kpi {cls}"><div class="label">{tier}</div>'
                    f'<div class="value">{count}</div>'
                    f'<div class="sub">{100 * count / max(len(keywords), 1):.0f}% of keywords</div></div>')
    body.append('</div>')

    # ---- Narrative insight ----
    insight = overview.get("insight", "")
    if insight:
        body.append(f'<div class="insight">{_html_escape(insight)}</div>')

    # ---- Charts ----
    body.append("<h2>Visual Analysis</h2>")
    body.append('<div class="chart-box">')
    body.append(_svg_difficulty_distribution(keywords))
    body.append('</div>')
    body.append('<div class="chart-box">')
    body.append(_svg_scatter_vol_diff(keywords))
    body.append('</div>')

    # ---- Keyword tables by tier ----
    # Easy wins: high volume + low difficulty
    easy_wins = sorted(
        [kw for kw in keywords if kw.get("difficulty_tier") == "Easy"],
        key=lambda k: (k.get("winnability", 0), k.get("volume", 0)),
        reverse=True
    )
    moderate = sorted(
        [kw for kw in keywords if kw.get("difficulty_tier") == "Moderate"],
        key=lambda k: (k.get("winnability", 0), k.get("volume", 0)),
        reverse=True
    )
    hard = sorted(
        [kw for kw in keywords if kw.get("difficulty_tier") in ("Hard", "Very Hard")],
        key=lambda k: (k.get("difficulty", 0), k.get("volume", 0)),
        reverse=False  # least hard first
    )

    def _kw_table(kws, title):
        body.append(f"<h3>{title}</h3>")
        if not kws:
            body.append('<p style="color:var(--muted)">No keywords in this tier.</p>')
            return
        body.append('<table><tr><th>Keyword</th><th>Difficulty</th><th>Tier</th>'
                    '<th>Volume</th><th>Winnability</th><th>Existing Rank</th>'
                    '<th>Big Brands in SERP</th></tr>')
        for kw in kws:
            win_data = kw.get("_win_analysis", {})
            existing_rank = win_data.get("existing_rank")
            rank_str = f"#{existing_rank}" if existing_rank else "—"
            big_brands = win_data.get("big_brand_count", 0)
            big_brands_str = f"{big_brands}/10" if big_brands else "0/10"
            body.append(
                f'<tr><td><b>{_html_escape(kw["keyword"])}</b></td>'
                f'<td>{kw.get("difficulty", 0):.1f}</td>'
                f'<td>{_tier_badge(kw.get("difficulty_tier"))}</td>'
                f'<td>{kw.get("volume", 0):,}</td>'
                f'<td>{_win_bar(kw.get("winnability", 0))}</td>'
                f'<td>{rank_str}</td>'
                f'<td>{big_brands_str}</td></tr>'
            )
        body.append('</table>')

    _kw_table(easy_wins, f"Easy Wins ({len(easy_wins)}) — High Opportunity, Low Effort")
    _kw_table(moderate, f"Moderate Competition ({len(moderate)}) — Worth Targeting With Effort")
    _kw_table(hard, f"Hard Keywords ({len(hard)}) — Build Authority or Avoid")

    # ---- Winnability breakdown table ----
    body.append("<h2>Winnability Breakdown</h2>")
    body.append('<p>Winnability = difficulty (40%) + authority vs competitors (30%) '
                '+ existing rankings (20%) + content gap (10%).</p>')
    body.append('<table><tr><th>Keyword</th><th>Difficulty</th><th>Domain Auth</th>'
                '<th>Comp Strength</th><th>Existing Rank</th><th>Gap Score</th>'
                '<th>Winnability</th><th>Priority</th></tr>')
    sorted_by_win = sorted(keywords, key=lambda k: k.get("winnability", 0), reverse=True)
    for kw in sorted_by_win:
        win_data = kw.get("_win_analysis", {})
        existing_rank = win_data.get("existing_rank")
        rank_str = f"#{existing_rank}" if existing_rank else "Not ranking"
        gap = win_data.get("gap_score", 0)
        priority = "High" if kw.get("winnability", 0) >= 60 else \
                   "Medium" if kw.get("winnability", 0) >= 35 else "Low"
        body.append(
            f'<tr><td><b>{_html_escape(kw["keyword"])}</b></td>'
            f'<td>{kw.get("difficulty", 0):.1f}</td>'
            f'<td>{win_data.get("domain_auth", 0):.1f}</td>'
            f'<td>{win_data.get("comp_strength", 0):.1f}</td>'
            f'<td>{rank_str}</td>'
            f'<td>{gap}</td>'
            f'<td>{_win_bar(kw.get("winnability", 0))}</td>'
            f'<td><span class="priority-tag priority-{priority}">{priority}</span></td></tr>'
        )
    body.append('</table>')

    # ---- Prioritized action list ----
    body.append("<h2>Prioritized Action List</h2>")
    action_list = analysis.get("action_list", [])
    if action_list:
        for i, action in enumerate(action_list, 1):
            priority = action.get("priority", "Medium")
            body.append(
                f'<div class="action-item">'
                f'<div class="action-num">{i}</div>'
                f'<div class="action-body">'
                f'<div class="title">{_html_escape(action.get("keyword", ""))}'
                f'<span class="priority-tag priority-{priority}">{priority}</span></div>'
                f'<div class="desc">{_html_escape(action.get("reason", ""))}</div>'
                f'</div></div>'
            )
    else:
        body.append('<p style="color:var(--muted)">No actions generated.</p>')

    # ---- Domain metrics section ----
    dm = analysis.get("domain_metrics", {})
    body.append("<h2>Domain Authority Snapshot</h2>")
    if dm and dm.get("referring_domains", 0) > 0:
        body.append(f'<table><tr><th>Metric</th><th>Value</th></tr>')
        body.append(f'<tr><td>Referring Domains</td><td><b>{dm.get("referring_domains", 0):,}</b></td></tr>')
        body.append(f'<tr><td>Total Backlinks</td><td><b>{dm.get("backlinks", 0):,}</b></td></tr>')
        body.append(f'<tr><td>Dofollow Backlinks</td><td><b>{dm.get("backlinks_dofollow", 0):,}</b></td></tr>')
        body.append(f'<tr><td>Referring Main Domains</td><td><b>{dm.get("referring_main_domains", 0):,}</b></td></tr>')
        body.append('</table>')
    else:
        body.append(f'<p style="color:var(--muted)">Domain metrics unavailable for '
                    f'<b>{_html_escape(domain)}</b>. Winnability scores computed with '
                    f'default authority estimate.</p>')

    # ---- Assemble HTML ----
    html = HTML_TMPL.format(
        seed=_html_escape(seed_keyword),
        date_generated=date_gen,
        body="".join(body),
    )

    html_path = os.path.join(output_path, f"{slug}_difficulty_map_{period}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    # Also write JSON
    json_path = os.path.join(output_path, f"{slug}_difficulty_map_{period}.json")
    json_payload = {
        "config": {
            "seed_keyword": seed_keyword,
            "domain": domain,
            "date_generated": date_gen,
            "location_code": DEFAULTS["location_code"],
        },
        "overview": overview,
        "domain_metrics": dm,
        "keywords": [{k: v for k, v in kw.items() if k != "_win_analysis"} for kw in keywords],
        "win_analysis": [{k: v for k, v in kw.get("_win_analysis", {}).items()}
                         for kw in keywords],
        "action_list": action_list,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_payload, f, indent=2, ensure_ascii=False)

    return html_path


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

def run_analysis(seed_keyword, keywords_list, domain, output_path, location_code=2840):
    """
    Run the full difficulty map analysis pipeline:
      1. Fetch difficulty + volume
      2. Classify difficulty tiers
      3. Compute winnability per keyword
      4. Generate report

    Returns: path to the HTML report.
    """
    print(f"[1/4] Fetching keyword difficulty for {len(keywords_list)} keywords ...")
    keywords = fetch_keyword_difficulty(keywords_list, location_code=location_code)
    print(f"    Got difficulty data for {len(keywords)} keywords.")

    print("[2/4] Classifying difficulty tiers ...")
    keywords = classify_difficulty(keywords)

    print(f"[3/4] Computing winnability for domain '{domain}' ...")
    # Fetch domain metrics once
    domain_metrics = _get_domain_metrics(domain) if domain else {}
    if domain_metrics:
        print(f"    Domain authority: {domain_metrics.get('referring_domains', 0)} referring domains.")
    else:
        print("    Domain metrics unavailable, using defaults.")

    # Fetch SERP for each keyword (for winnability)
    for kw in keywords:
        kw_text = kw["keyword"]
        serp_items = _get_serp(kw_text, location_code=location_code)
        win_result = compute_winnability(
            kw_text, domain,
            keyword_data=kw,
            domain_metrics=domain_metrics,
            serp_items=serp_items,
        )
        kw["winnability"] = win_result["winnability"]
        kw["_win_analysis"] = win_result
        time.sleep(0.3)  # rate limit courtesy

    print("[4/4] Generating report ...")

    # Build overview stats
    difficulties = [kw.get("difficulty", 50) for kw in keywords]
    volumes = [kw.get("volume", 0) for kw in keywords]
    winnabilities = [kw.get("winnability", 0) for kw in keywords]

    dist = {"Easy": 0, "Moderate": 0, "Hard": 0, "Very Hard": 0}
    for kw in keywords:
        tier = kw.get("difficulty_tier", "Moderate")
        if tier in dist:
            dist[tier] += 1

    avg_diff = sum(difficulties) / len(difficulties) if difficulties else 0
    total_vol = sum(volumes)
    avg_win = sum(winnabilities) / len(winnabilities) if winnabilities else 0

    # Generate insight
    easy_count = dist.get("Easy", 0)
    mod_count = dist.get("Moderate", 0)
    if easy_count > 0:
        insight = (f"The '{seed_keyword}' market has an average difficulty of {avg_diff:.1f}. "
                  f"{easy_count} easy-win keyword(s) and {mod_count} moderate keyword(s) "
                  f"represent the best opportunities for {domain}.")
    else:
        insight = (f"The '{seed_keyword}' market is competitive with an average difficulty "
                  f"of {avg_diff:.1f}. Focus on long-tail and content gap opportunities "
                  f"to build authority before tackling harder terms.")

    # Build action list (sorted by winnability * volume)
    action_candidates = sorted(keywords, key=lambda k: k.get("winnability", 0) * math.log(1 + k.get("volume", 0)), reverse=True)
    action_list = []
    for kw in action_candidates:
        win = kw.get("winnability", 0)
        diff = kw.get("difficulty", 50)
        vol = kw.get("volume", 0)
        tier = kw.get("difficulty_tier", "Moderate")
        if win >= 60:
            priority = "High"
            reason = (f"Winnability {win:.0f}% — difficulty {diff:.0f} ({tier}), "
                      f"volume {vol:,}/mo. Target immediately.")
        elif win >= 35:
            priority = "Medium"
            reason = (f"Winnability {win:.0f}% — difficulty {diff:.0f} ({tier}), "
                      f"volume {vol:,}/mo. Target with focused content effort.")
        else:
            priority = "Low"
            reason = (f"Winnability {win:.0f}% — difficulty {diff:.0f} ({tier}), "
                      f"volume {vol:,}/mo. Build authority before targeting.")
        action_list.append({
            "keyword": kw["keyword"],
            "priority": priority,
            "reason": reason,
            "winnability": win,
            "difficulty": diff,
            "volume": vol,
        })

    overview = {
        "avg_difficulty": round(avg_diff, 1),
        "distribution": dist,
        "total_volume": total_vol,
        "avg_winnability": round(avg_win, 1),
        "total_keywords": len(keywords),
        "insight": insight,
    }

    analysis = {
        "domain": domain,
        "overview": overview,
        "domain_metrics": domain_metrics,
        "action_list": action_list,
    }

    report_path = generate_difficulty_report(seed_keyword, keywords, analysis, output_path)
    print(f"\nDifficulty map complete.")
    print(f"  HTML report: {report_path}")
    print(f"  Keywords: {len(keywords)} analyzed")
    print(f"  Avg difficulty: {avg_diff:.1f} | Avg winnability: {avg_win:.1f}%")
    print(f"  Distribution: Easy={dist['Easy']} Moderate={dist['Moderate']} "
          f"Hard={dist['Hard']} Very Hard={dist['Very Hard']}")
    return report_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Build a market-wide keyword difficulty map showing where to compete.")
    ap.add_argument("--seed", required=True, help="Seed keyword for the market")
    ap.add_argument("--domain", required=True, help="Target domain (e.g. softprowatersystems.com)")
    ap.add_argument("--keywords", required=True,
                    help="Comma-separated list of keywords to analyze")
    ap.add_argument("--output", default="/tmp/difficulty_map",
                    help="Output directory (default /tmp/difficulty_map)")
    ap.add_argument("--location", type=int, default=DEFAULTS["location_code"],
                    help="DataForSEO location code (default 2840 = US)")
    args = ap.parse_args()

    load_env()

    keywords_list = [k.strip() for k in args.keywords.split(",") if k.strip()]
    if not keywords_list:
        sys.stderr.write("ERROR: No keywords provided.\n")
        sys.exit(2)

    domain = args.domain.lower().lstrip("www.").strip("/")
    domain = re.sub(r"^https?://", "", domain)

    run_analysis(
        seed_keyword=args.seed,
        keywords_list=keywords_list,
        domain=domain,
        output_path=args.output,
        location_code=args.location,
    )


if __name__ == "__main__":
    main()
