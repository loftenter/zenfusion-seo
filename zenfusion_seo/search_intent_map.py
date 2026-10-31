#!/usr/bin/env python3
"""
Search Intent Map — standalone CLI
==================================
Maps search intent distribution across an entire market.

Pipeline:
  1. fetch_market_keywords()  — DataForSEO keyword_ideas → full keyword universe (vol, difficulty, CPC, intent)
  2. classify_intent()         — informational / commercial / transactional / navigational (DataForSEO intent + SERP heuristics)
  3. map_page_types()          — live SERP for top keywords → what page types rank (product, collection, blog, homepage, review)
  4. generate_intent_report()  — HTML report: market overview, intent pie (SVG), funnel, page-type map, keyword tables,
                                  content gaps, commercial gaps, recommendations

Usage:
  python3 search_intent_map.py --seed 'water softener' --domain softprowatersystems.com --output /tmp/test_intent_map
  python3 search_intent_map.py --seed 'water softener' --limit 300 --no-serp

Environment:
  DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD  in ~/.hermes/.env
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
import math
import time
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
    "location_code": 2840,
    "limit": 500,
}

ENV_PATH = os.path.expanduser("~/.hermes/.env")

INTENT_COLORS = {
    "informational":   "#2563EB",
    "commercial":      "#D97706",
    "transactional":   "#16A34A",
    "navigational":    "#7C3AED",
}

INTENT_ORDER = ["informational", "commercial", "transactional", "navigational"]

PAGE_TYPE_LABELS = {
    "product":      "Product Page",
    "collection":   "Collection Page",
    "blog":         "Blog Post",
    "homepage":     "Homepage",
    "review":       "Comparison / Review Site",
    "other":        "Other",
}

# ---------------------------------------------------------------------------
# .env + API plumbing
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


def dfs_post(path, task, timeout=90, retries=2):
    """POST one task to a DataForSEO endpoint; return the raw response dict."""
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
                return data  # return so caller can inspect error details
            return data
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.5)
                continue
    sys.stderr.write(f"DataForSEO {path} request failed: {last_err}\n")
    return None


def _items(data, idx=0):
    """Extract items list from a standard DataForSEO response."""
    if not data:
        return []
    tasks = data.get("tasks") or []
    if not tasks or len(tasks) <= idx:
        return []
    res = tasks[idx].get("result") or []
    if not res:
        return []
    return res[0].get("items") or []


# ---------------------------------------------------------------------------
# 1. fetch_market_keywords
# ---------------------------------------------------------------------------

def fetch_market_keywords(seed_keyword, limit=500, location_name=None, language_code=None):
    """
    Expand a seed keyword into a full keyword universe using DataForSEO
    keyword_ideas, then enrich with bulk_keyword_difficulty and search_intent.

    Returns: list of dicts:
        {keyword, volume, cpc, competition, difficulty, intent, intent_probability}
    """
    location_name = location_name or DEFAULTS["location_name"]
    language_code = language_code or DEFAULTS["language_code"]
    seed = seed_keyword.strip().lower()
    if not seed:
        return []

    # --- keyword_ideas ---
    task = {
        "keywords": [seed],
        "location_name": location_name,
        "language_code": language_code,
        "limit": limit,
        "order_by": ["relevance,desc", "keyword_info.search_volume,desc"],
    }
    data = dfs_post("dataforseo_labs/google/keyword_ideas/live", task, timeout=120)
    items = _items(data)

    out = []
    seen = set()
    for it in items:
        kw = (it.get("keyword") or "").strip().lower()
        if not kw or kw in seen:
            continue
        seen.add(kw)
        ki = it.get("keyword_info") or {}
        out.append({
            "keyword": kw,
            "volume": ki.get("search_volume") or 0,
            "cpc": ki.get("cpc"),
            "competition": ki.get("competition"),
            "difficulty": None,
            "intent": None,
            "intent_probability": None,
        })

    if not out:
        return out

    # --- bulk_keyword_difficulty ---
    kws = [k["keyword"] for k in out]
    diff_map = {}
    for i in range(0, len(kws), 1000):
        chunk = kws[i:i + 1000]
        ddata = dfs_post("dataforseo_labs/google/bulk_keyword_difficulty/live", {
            "keywords": chunk,
            "location_name": location_name,
            "language_code": language_code,
        }, timeout=90)
        for it in _items(ddata) or []:
            k = (it.get("keyword") or "").strip().lower()
            if k:
                diff_map[k] = it.get("keyword_difficulty")

    # --- search_intent ---
    intent_map = {}
    for i in range(0, len(kws), 1000):
        chunk = kws[i:i + 1000]
        idata = dfs_post("dataforseo_labs/google/search_intent/live", {
            "keywords": chunk,
            "language_code": language_code,
        }, timeout=90)
        for it in _items(idata) or []:
            k = (it.get("keyword") or "").strip().lower()
            if not k:
                continue
            ki = it.get("keyword_intent") or {}
            intent_map[k] = {
                "label": ki.get("label"),
                "probability": ki.get("probability"),
            }

    for k in out:
        kw = k["keyword"]
        if k["difficulty"] is None:
            k["difficulty"] = diff_map.get(kw)
        if k["difficulty"] is None:
            k["difficulty"] = 50  # neutral fallback
        if k["intent"] is None:
            intn = intent_map.get(kw)
            if intn:
                k["intent"] = (intn.get("label") or "informational").lower()
                k["intent_probability"] = intn.get("probability")
            else:
                k["intent"] = "informational"
                k["intent_probability"] = None

    return out


# ---------------------------------------------------------------------------
# 2. classify_intent
# ---------------------------------------------------------------------------

# Keyword-pattern heuristics (supplement DataForSEO intent field)
_INTENT_PATTERNS = {
    "transactional": [
        r"\b(buy|purchase|order|shop|cheap|discount|deal|coupon|for sale|price)\b",
        r"\b(bestseller|best seller|on sale|free shipping)\b",
    ],
    "commercial": [
        r"\b(best|top|review|reviews|compare|comparison|vs|versus|alternative)\b",
        r"\b(rating|rated|recommend|recommended|which|what.*best)\b",
    ],
    "navigational": [
        r"\b(login|sign in|register|account|dashboard|official site)\b",
        r"\b(website|site|url|app)\b",
    ],
    "informational": [
        r"\b(how|what|why|when|where|who|guide|tutorial|tips|advice)\b",
        r"\b(learn|understand|explain|meaning|definition|examples?|info)\b",
        r"\b(is|are|can|does|do|should|will)\b",
    ],
}

def _heuristic_intent(keyword):
    """Classify intent from keyword text patterns. Returns one of the four labels."""
    kw = keyword.lower()
    for intent in INTENT_ORDER:
        for pat in _INTENT_PATTERNS.get(intent, []):
            if re.search(pat, kw):
                return intent
    return "informational"


def classify_intent(keywords):
    """
    Classify each keyword as informational / commercial / transactional / navigational.
    Uses DataForSEO's intent field as primary, heuristic SERP classification as fallback/enhancement.

    Mutates and returns the keywords list, setting 'intent' and 'intent_source' on each.
    """
    for k in keywords:
        api_intent = (k.get("intent") or "").lower().strip()
        if api_intent in ("informational", "commercial", "transactional", "navigational"):
            # Validate with heuristic: if heuristic strongly disagrees AND no probability, use heuristic
            heuristic = _heuristic_intent(k["keyword"])
            prob = k.get("intent_probability")
            if prob is not None and prob < 0.5:
                # Low-confidence API intent — prefer heuristic
                k["intent"] = heuristic
                k["intent_source"] = "heuristic"
            else:
                k["intent"] = api_intent
                k["intent_source"] = "api"
        else:
            k["intent"] = _heuristic_intent(k["keyword"])
            k["intent_source"] = "heuristic"
    return keywords


# ---------------------------------------------------------------------------
# 3. map_page_types
# ---------------------------------------------------------------------------

def _classify_serp_result(url, title, description=""):
    """Classify a single SERP result's page type from URL path and title text."""
    u = (url or "").lower()
    t = (title or "").lower()
    d = (description or "").lower()
    combined = u + " " + t + " " + d

    # Review / comparison sites
    if any(x in combined for x in [
        "review", "reviews", "best ", "top 10", "top 5", "comparison", "compare",
        "vs ", "alternatives", "buying guide", "editor's choice",
    ]):
        # but if it's a blog-style review on a store, treat as blog
        if "/blog" in u or "/post" in u or "/article" in u or "/guide" in u:
            return "blog"
        if any(x in u for x in ["/collections/", "/category/", "/shop/"]):
            return "collection"
        if any(x in u for x in ["/products/", "/p/"]):
            return "product"
        return "review"

    # Blog / informational
    if any(x in u for x in ["/blog", "/post", "/article", "/news", "/guide", "/learn", "/resources"]):
        return "blog"
    if any(x in t for x in ["how to", "what is", "why ", "guide to", "tips for", "ultimate guide"]):
        return "blog"

    # Collection / category
    if any(x in u for x in ["/collections/", "/category/", "/c/", "/shop/", "/catalog"]):
        return "collection"

    # Product page
    if any(x in u for x in ["/products/", "/p/", "/dp/", "/pd/", "/item/", "/listing/"]):
        return "product"
    if "product" in t and any(x in u for x in ["/store/", "/detail"]):
        return "product"

    # Homepage (root URL or very short path)
    from urllib.parse import urlparse
    parsed = urlparse(u)
    path = (parsed.path or "").strip("/")
    if not path or path in ("index.html", "index.php", "home"):
        return "homepage"

    return "other"


def map_page_types(keywords, sample_size=50, location_name=None, language_code=None):
    """
    For top keywords (by volume), fetch SERP and classify what page types rank.
    Returns: dict keyed by keyword → {
        'page_type': dominant_type,
        'counts': {product, collection, blog, homepage, review, other},
        'sample_urls': [top 5 URLs],
        'top_domains': [top 3 ranking domains],
    }
    Also returns an aggregate: {'by_intent': {intent: {page_type: count}}, 'overall': {page_type: count}}
    """
    location_name = location_name or DEFAULTS["location_name"]
    language_code = language_code or DEFAULTS["language_code"]

    # Sort by volume desc, take top sample_size
    sorted_kws = sorted(keywords, key=lambda k: k.get("volume") or 0, reverse=True)
    sample = sorted_kws[:sample_size]

    per_keyword = {}
    by_intent = defaultdict(lambda: defaultdict(int))
    overall = defaultdict(int)

    for i, k in enumerate(sample):
        kw = k["keyword"]
        intent = k.get("intent", "informational")

        task = {
            "keyword": kw,
            "location_name": location_name,
            "language_code": language_code,
            "device": "desktop",
            "os": "windows",
        }
        data = dfs_post("serp/google/organic/live/advanced", task, timeout=60)
        items = _items(data)

        counts = defaultdict(int)
        sample_urls = []
        top_domains = []

        for it in items or []:
            if it.get("type") != "organic":
                continue
            url = it.get("url") or ""
            title = it.get("title") or ""
            desc = it.get("description") or ""
            domain = it.get("domain") or ""

            ptype = _classify_serp_result(url, title, desc)
            counts[ptype] += 1
            overall[ptype] += 1
            by_intent[intent][ptype] += 1

            if len(sample_urls) < 5:
                sample_urls.append(url)
            if domain and domain not in top_domains and len(top_domains) < 3:
                top_domains.append(domain)

        # dominant page type
        total = sum(counts.values()) or 1
        dominant = max(counts, key=lambda x: counts[x]) if counts else "other"
        if counts[dominant] / total <= 0.34 and len(counts) > 1:
            dominant = "mixed"

        per_keyword[kw] = {
            "page_type": dominant,
            "counts": dict(counts),
            "sample_urls": sample_urls,
            "top_domains": top_domains,
            "intent": intent,
        }

        if (i + 1) % 10 == 0:
            print(f"  SERP mapped {i + 1}/{len(sample)} keywords ...")
        # Rate limit — be gentle on API
        time.sleep(0.3)

    aggregate = {
        "by_intent": {intent: dict(ptypes) for intent, ptypes in by_intent.items()},
        "overall": dict(overall),
    }

    return per_keyword, aggregate


# ---------------------------------------------------------------------------
# 4. generate_intent_report
# ---------------------------------------------------------------------------

def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "seed"


def _now():
    return datetime.datetime.now()


def _fmt(n):
    if n is None:
        return "—"
    return "{:,}".format(int(n))


def _svg_pie(data, colors, size=240):
    """
    Generate an SVG pie chart (donut style).
    data: list of (label, value) tuples.
    colors: dict label→hex color.
    """
    total = sum(v for _, v in data) or 1
    cx, cy, r_outer, r_inner = size / 2, size / 2, size / 2 - 8, size / 2 - 30
    start_angle = -math.pi / 2  # start at top

    slices = []
    legend = []
    angle = start_angle
    for label, value in data:
        if value <= 0:
            continue
        frac = value / total
        end_angle = angle + frac * 2 * math.pi
        # large-arc flag
        large_arc = 1 if frac > 0.5 else 0

        x1 = cx + r_outer * math.cos(angle)
        y1 = cy + r_outer * math.sin(angle)
        x2 = cx + r_outer * math.cos(end_angle)
        y2 = cy + r_outer * math.sin(end_angle)
        x3 = cx + r_inner * math.cos(end_angle)
        y3 = cy + r_inner * math.sin(end_angle)
        x4 = cx + r_inner * math.cos(angle)
        y4 = cy + r_inner * math.sin(angle)

        path = (
            f"M {x1:.1f} {y1:.1f} "
            f"A {r_outer:.1f} {r_outer:.1f} 0 {large_arc} 1 {x2:.1f} {y2:.1f} "
            f"L {x3:.1f} {y3:.1f} "
            f"A {r_inner:.1f} {r_inner:.1f} 0 {large_arc} 0 {x4:.1f} {y4:.1f} "
            f"Z"
        )
        color = colors.get(label, "#9CA3AF")
        slices.append(f'<path d="{path}" fill="{color}" stroke="#fff" stroke-width="1.5"/>')
        pct = round(frac * 100, 1)
        legend.append((label, value, pct, color))
        angle = end_angle

    svg_parts = [
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" '
        f'xmlns="http://www.w3.org/2000/svg">',
        f'<circle cx="{cx}" cy="{cy}" r="{r_outer + 4}" fill="#F3F4F6"/>',
    ]
    svg_parts.extend(slices)
    # center label
    svg_parts.append(
        f'<text x="{cx}" y="{cy - 6}" text-anchor="middle" '
        f'style="font-size:13px;font-weight:700;fill:#1F2937;">{_fmt(total)}</text>'
    )
    svg_parts.append(
        f'<text x="{cx}" y="{cy + 12}" text-anchor="middle" '
        f'style="font-size:10px;fill:#6B7280;">Total Vol</text>'
    )
    svg_parts.append("</svg>")
    return "\n".join(svg_parts), legend


def _svg_funnel(intent_data, colors):
    """
    Generate an SVG funnel chart showing the buying journey:
    Informational → Commercial → Transactional → Navigational
    """
    stages = INTENT_ORDER
    values = [intent_data.get(s, {}).get("volume", 0) for s in stages]
    max_val = max(values) or 1

    width = 700
    height = 60 * len(stages) + 40
    bar_h = 40
    gap = 12
    label_w = 140
    chart_x = label_w + 10
    chart_w = width - chart_x - 120

    parts = [
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg">'
    ]

    for i, (stage, val) in enumerate(zip(stages, values)):
        y = 20 + i * (bar_h + gap)
        frac = val / max_val if max_val else 0
        w = max(chart_w * frac, 20)
        color = colors.get(stage, "#9CA3AF")
        pct = round(frac * 100, 1)
        # label
        parts.append(
            f'<text x="10" y="{y + bar_h / 2 + 4}" '
            f'style="font-size:13px;font-weight:600;fill:#1F2937;">{stage.title()}</text>'
        )
        # bar
        parts.append(
            f'<rect x="{chart_x}" y="{y}" width="{w:.0f}" height="{bar_h}" '
            f'rx="4" fill="{color}" opacity="0.9"/>'
        )
        # value
        parts.append(
            f'<text x="{chart_x + w + 8:.0f}" y="{y + bar_h / 2 + 5}" '
            f'style="font-size:12px;font-weight:600;fill:#1F2937;">'
            f'{_fmt(val)}/mo ({pct}%)</text>'
        )

    parts.append("</svg>")
    return "\n".join(parts)


def _html_escape(s):
    if s is None:
        return ""
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _build_recommendations(intent_stats, page_type_aggregate, domain, keywords):
    """Generate actionable recommendations based on intent distribution and gaps."""
    recs = []

    total_vol = intent_stats.get("_total_volume", 0)
    info_vol = intent_stats.get("informational", {}).get("volume", 0)
    comm_vol = intent_stats.get("commercial", {}).get("volume", 0)
    trans_vol = intent_stats.get("transactional", {}).get("volume", 0)
    nav_vol = intent_stats.get("navigational", {}).get("volume", 0)

    info_pct = round(info_vol / total_vol * 100, 1) if total_vol else 0
    comm_pct = round(comm_vol / total_vol * 100, 1) if total_vol else 0
    trans_pct = round(trans_vol / total_vol * 100, 1) if total_vol else 0

    # Informational content gap
    if info_pct > 30:
        recs.append({
            "priority": "High",
            "intent": "Informational",
            "title": f"Build editorial/blog content for {info_pct}% of market demand",
            "detail": (f"Informational queries account for {_fmt(info_vol)}/mo searches "
                       f"({info_pct}% of market). Create comprehensive guides, how-to articles, "
                       f"and educational content to capture top-of-funnel traffic."),
        })
    elif info_pct > 10:
        recs.append({
            "priority": "Medium",
            "intent": "Informational",
            "title": f"Expand blog content to capture {info_pct}% informational demand",
            "detail": (f"Informational queries represent {_fmt(info_vol)}/mo. "
                       f"Publish pillar pages and supporting articles for top informational keywords."),
        })

    # Commercial content
    if comm_pct > 15:
        recs.append({
            "priority": "High",
            "intent": "Commercial",
            "title": f"Create comparison/buying-guide content for {comm_pct}% commercial demand",
            "detail": (f"Commercial queries ({_fmt(comm_vol)}/mo) indicate active research intent. "
                       f"Publish 'best of', comparison, and buying-guide pages to intercept "
                       f"users in the consideration phase."),
        })

    # Transactional
    if trans_pct > 10:
        recs.append({
            "priority": "High",
            "intent": "Transactional",
            "title": f"Optimize product/collection pages for {trans_pct}% transactional demand",
            "detail": (f"Transactional queries ({_fmt(trans_vol)}/mo) represent bottom-of-funnel "
                       f"buying intent. Ensure product pages, pricing pages, and collection "
                       f"pages are optimized with clear CTAs and schema markup."),
        })

    # Page type alignment
    by_intent = page_type_aggregate.get("by_intent", {})
    for intent in INTENT_ORDER:
        ptypes = by_intent.get(intent, {})
        if not ptypes:
            continue
        dominant_pt = max(ptypes, key=ptypes.get)
        if intent == "informational" and dominant_pt == "product":
            recs.append({
                "priority": "Medium",
                "intent": "Informational",
                "title": "Google ranks product pages for informational queries — add educational content",
                "detail": (f"SERP analysis shows product pages dominating for informational keywords. "
                           f"Creating blog/guide content can differentiate and capture "
                           f"top-of-funnel traffic competitors miss."),
            })
        if intent == "transactional" and dominant_pt == "blog":
            recs.append({
                "priority": "Medium",
                "intent": "Transactional",
                "title": "Blog pages outrank product pages for transactional queries",
                "detail": (f"SERP shows blog content ranking for transactional keywords. "
                           f"Strengthen product pages with better content, reviews, and "
                           f"structured data to compete."),
            })

    # Navigational
    if nav_vol > 0 and total_vol and nav_vol / total_vol > 0.05:
        recs.append({
            "priority": "Low",
            "intent": "Navigational",
            "title": "Capture branded/navigational queries",
            "detail": (f"Navigational queries ({_fmt(nav_vol)}/mo) suggest brand searches. "
                       f"Ensure brand landing pages and site search are optimized."),
        })

    if not recs:
        recs.append({
            "priority": "Low",
            "intent": "General",
            "title": "Balanced market — maintain content across all intents",
            "detail": "Intent distribution is relatively balanced. Continue publishing "
                      "content across informational, commercial, and transactional queries.",
        })

    return recs


def generate_intent_report(seed_keyword, keywords, page_types, output_path,
                           domain=None, location_name=None, language_code=None):
    """
    Generate the full HTML intent map report.

    Parameters:
        seed_keyword   — the seed query used to expand the market
        keywords       — list of keyword dicts (with intent, volume, difficulty, etc.)
        page_types     — tuple of (per_keyword, aggregate) from map_page_types()
        output_path    — directory to write the report into
        domain         — target domain for gap analysis (e.g. softprowatersystems.com)

    Returns: path to the generated HTML file.
    """
    os.makedirs(output_path, exist_ok=True)
    date_gen = _now().strftime("%Y-%m-%d %H:%M")
    slug = _slug(seed_keyword)

    per_keyword, aggregate = page_types if isinstance(page_types, tuple) else (page_types, {})

    # --- Market overview stats ---
    total_kw = len(keywords)
    total_vol = sum(k.get("volume") or 0 for k in keywords)
    avg_diff = round(sum(k.get("difficulty") or 0 for k in keywords) / total_kw, 1) if total_kw else 0
    avg_cpc = None
    cpc_vals = [k.get("cpc") for k in keywords if k.get("cpc")]
    if cpc_vals:
        avg_cpc = round(sum(cpc_vals) / len(cpc_vals), 2)

    # --- Intent stats ---
    intent_stats = defaultdict(lambda: {"count": 0, "volume": 0, "difficulty": 0})
    for k in keywords:
        intent = k.get("intent", "informational")
        intent_stats[intent]["count"] += 1
        intent_stats[intent]["volume"] += k.get("volume") or 0
        intent_stats[intent]["difficulty"] += k.get("difficulty") or 0
    for intent in intent_stats:
        c = intent_stats[intent]["count"]
        if c:
            intent_stats[intent]["difficulty"] = round(intent_stats[intent]["difficulty"] / c, 1)
    intent_stats["_total_volume"] = total_vol

    # --- Pie chart data ---
    pie_data = [(intent, intent_stats[intent]["volume"]) for intent in INTENT_ORDER if intent_stats[intent]["volume"] > 0]
    pie_svg, pie_legend = _svg_pie(pie_data, INTENT_COLORS, size=260)

    # --- Funnel ---
    funnel_svg = _svg_funnel(dict(intent_stats), INTENT_COLORS)

    # --- Page type map table ---
    by_intent = aggregate.get("by_intent", {}) if aggregate else {}

    # --- Content gap analysis ---
    info_kws = [k for k in keywords if k.get("intent") == "informational"]
    comm_kws = [k for k in keywords if k.get("intent") == "commercial"]
    trans_kws = [k for k in keywords if k.get("intent") == "transactional"]

    # Informational keywords without matching blog/page on domain
    info_gaps = []
    if domain:
        domain_clean = domain.replace("www.", "").lower()
        for k in sorted(info_kws, key=lambda x: x.get("volume") or 0, reverse=True)[:30]:
            # Check if domain ranks in SERP for this keyword
            serp_data = per_keyword.get(k["keyword"], {})
            top_domains = [d.lower() for d in serp_data.get("top_domains", [])]
            domain_ranks = any(domain_clean in d or d in domain_clean for d in top_domains)
            # Check if the dominant page type is blog (meaning blogs rank)
            page_type = serp_data.get("page_type", "")
            if not domain_ranks and page_type in ("blog", "mixed", "other", ""):
                info_gaps.append({
                    "keyword": k["keyword"],
                    "volume": k.get("volume", 0),
                    "difficulty": k.get("difficulty", "—"),
                    "serp_page_type": page_type or "unknown",
                    "reason": "Domain not ranking — blog content opportunity" if page_type == "blog" else "No presence in SERP",
                })

    # Commercial keywords without collection pages
    comm_gaps = []
    if domain:
        domain_clean = domain.replace("www.", "").lower()
        for k in sorted(comm_kws, key=lambda x: x.get("volume") or 0, reverse=True)[:30]:
            serp_data = per_keyword.get(k["keyword"], {})
            top_domains = [d.lower() for d in serp_data.get("top_domains", [])]
            domain_ranks = any(domain_clean in d or d in domain_clean for d in top_domains)
            page_type = serp_data.get("page_type", "")
            if not domain_ranks:
                comm_gaps.append({
                    "keyword": k["keyword"],
                    "volume": k.get("volume", 0),
                    "difficulty": k.get("difficulty", "—"),
                    "serp_page_type": page_type or "unknown",
                    "recommended_page": "Collection page" if page_type in ("collection", "mixed") else "Comparison/landing page",
                })

    # --- Recommendations ---
    recommendations = _build_recommendations(dict(intent_stats), aggregate, domain, keywords)

    # --- Build HTML ---
    body = []

    # Header
    body.append(f'<header><h1>Search Intent Map — {_html_escape(seed_keyword)}</h1>')
    meta_parts = [date_gen, f"US / en", f"{total_kw} keywords"]
    if domain:
        meta_parts.append(f"domain: {_html_escape(domain)}")
    body.append(f'<div class="meta">{" • ".join(meta_parts)}</div></header>')

    # Market overview KPIs
    body.append('<div class="kpis">')
    kpis = [
        ("Total Keywords", str(total_kw), "in market universe"),
        ("Total Search Volume", _fmt(total_vol), "monthly searches"),
        ("Avg Difficulty", str(avg_diff), "/ 100"),
        ("Avg CPC", f"${avg_cpc}" if avg_cpc else "—", "per click"),
    ]
    for label, val, sub in kpis:
        body.append(f'<div class="kpi"><div class="label">{label}</div>'
                     f'<div class="value">{val}</div><div class="sub">{sub}</div></div>')
    body.append('</div>')

    # Intent distribution pie + table
    body.append('<h2>Intent Distribution</h2>')
    body.append('<div class="chart-row">')
    body.append(f'<div class="chart-cell">{pie_svg}</div>')
    body.append('<div class="chart-cell"><table><tr><th>Intent</th><th>Keywords</th>'
                '<th>Volume</th><th>Share</th><th>Avg Diff</th></tr>')
    for intent in INTENT_ORDER:
        s = intent_stats[intent]
        share = round(s["volume"] / total_vol * 100, 1) if total_vol else 0
        color = INTENT_COLORS.get(intent, "#9CA3AF")
        body.append(
            f'<tr><td><span class="dot" style="background:{color}"></span>'
            f'<b>{intent.title()}</b></td><td>{s["count"]}</td>'
            f'<td>{_fmt(s["volume"])}</td><td>{share}%</td>'
            f'<td>{s["difficulty"]}</td></tr>'
        )
    body.append('</table></div>')
    body.append('</div>')  # chart-row

    # Intent funnel
    body.append('<h2>Buying Journey Funnel</h2>')
    body.append(f'<div class="funnel-wrap">{funnel_svg}</div>')
    body.append('<p class="muted">The funnel shows where market demand sits in the buying journey: '
                'Informational (awareness) → Commercial (research) → Transactional (purchase) → '
                'Navigational (branded).</p>')

    # Page type map
    body.append('<h2>Page Type Map — What Google Ranks by Intent</h2>')
    body.append('<table><tr><th>Intent</th><th>Product</th><th>Collection</th>'
                '<th>Blog</th><th>Homepage</th><th>Review</th><th>Other</th>'
                '<th>Dominant Type</th></tr>')
    for intent in INTENT_ORDER:
        ptypes = by_intent.get(intent, {})
        row_data = [ptypes.get(pt, 0) for pt in ["product", "collection", "blog", "homepage", "review", "other"]]
        total_pt = sum(row_data) or 1
        dominant = max(["product", "collection", "blog", "homepage", "review", "other"],
                        key=lambda x: ptypes.get(x, 0)) if any(row_data) else "—"
        dominant_label = PAGE_TYPE_LABELS.get(dominant, dominant)
        color = INTENT_COLORS.get(intent, "#9CA3AF")
        cells = "".join(f'<td>{v}</td>' for v in row_data)
        body.append(
            f'<tr><td><span class="dot" style="background:{color}"></span>'
            f'<b>{intent.title()}</b></td>{cells}'
            f'<td><b>{dominant_label}</b></td></tr>'
        )
    # overall row
    overall = aggregate.get("overall", {}) if aggregate else {}
    overall_data = [overall.get(pt, 0) for pt in ["product", "collection", "blog", "homepage", "review", "other"]]
    cells = "".join(f'<td>{v}</td>' for v in overall_data)
    body.append(f'<tr style="font-weight:600;background:var(--soft)"><td>Overall</td>{cells}<td>—</td></tr>')
    body.append('</table>')

    # Keyword table by intent (expandable, filterable)
    body.append('<h2>Keyword Table by Intent</h2>')
    body.append('<div class="filter-bar">')
    body.append('<label>Filter by intent: </label>')
    body.append('<select id="intentFilter" onchange="filterKeywords()">'
                '<option value="all">All Intents</option>')
    for intent in INTENT_ORDER:
        body.append(f'<option value="{intent}">{intent.title()} ({intent_stats[intent]["count"]})</option>')
    body.append('</select>')
    body.append('<input type="text" id="kwSearch" placeholder="Search keywords..." '
                'oninput="filterKeywords()" style="margin-left:12px;padding:4px 8px;width:200px">')
    body.append('</div>')

    body.append('<table id="kwTable"><thead><tr><th>Keyword</th><th>Intent</th>'
                '<th>Volume</th><th>Difficulty</th><th>CPC</th><th>SERP Page Type</th></tr></thead><tbody>')
    for k in sorted(keywords, key=lambda x: x.get("volume") or 0, reverse=True):
        intent = k.get("intent", "informational")
        color = INTENT_COLORS.get(intent, "#9CA3AF")
        vol = _fmt(k.get("volume", 0))
        diff = k.get("difficulty", "—")
        cpc = k.get("cpc")
        cpc_str = f"${cpc:.2f}" if cpc else "—"
        serp = per_keyword.get(k["keyword"], {}).get("page_type", "—")
        serp_label = PAGE_TYPE_LABELS.get(serp, serp) if serp != "—" else "—"
        body.append(
            f'<tr class="kw-row" data-intent="{intent}" data-kw="{_html_escape(k["keyword"].lower())}">'
            f'<td>{_html_escape(k["keyword"])}</td>'
            f'<td><span class="dot" style="background:{color}"></span>{intent.title()}</td>'
            f'<td>{vol}</td><td>{diff}</td><td>{cpc_str}</td>'
            f'<td>{serp_label}</td></tr>'
        )
    body.append('</tbody></table>')

    # Content gap: informational
    body.append('<h2>Content Gap — Informational Keywords Without Matching Content</h2>')
    if domain:
        if info_gaps:
            body.append(f'<p class="muted">{len(info_gaps)} informational keywords where '
                        f'<b>{_html_escape(domain)}</b> does not rank and blog/guide content is needed:</p>')
            body.append('<table><tr><th>Keyword</th><th>Volume</th><th>Difficulty</th>'
                        '<th>SERP Dominant Type</th><th>Opportunity</th></tr>')
            for g in info_gaps:
                body.append(
                    f'<tr><td>{_html_escape(g["keyword"])}</td><td>{_fmt(g["volume"])}</td>'
                    f'<td>{g["difficulty"]}</td><td>{PAGE_TYPE_LABELS.get(g["serp_page_type"], g["serp_page_type"])}</td>'
                    f'<td>{_html_escape(g["reason"])}</td></tr>'
                )
            body.append('</table>')
        else:
            body.append(f'<p class="muted">No informational content gaps detected for '
                        f'{_html_escape(domain)} (or SERP mapping was skipped).</p>')
    else:
        body.append('<p class="muted">No domain specified — content gap analysis skipped. '
                    'Use --domain to enable.</p>')

    # Commercial gap
    body.append('<h2>Commercial Keywords Without Collection Pages</h2>')
    if domain:
        if comm_gaps:
            body.append(f'<p class="muted">{len(comm_gaps)} commercial keywords where '
                        f'<b>{_html_escape(domain)}</b> does not rank:</p>')
            body.append('<table><tr><th>Keyword</th><th>Volume</th><th>Difficulty</th>'
                        '<th>SERP Type</th><th>Recommended Page</th></tr>')
            for g in comm_gaps:
                body.append(
                    f'<tr><td>{_html_escape(g["keyword"])}</td><td>{_fmt(g["volume"])}</td>'
                    f'<td>{g["difficulty"]}</td>'
                    f'<td>{PAGE_TYPE_LABELS.get(g["serp_page_type"], g["serp_page_type"])}</td>'
                    f'<td>{_html_escape(g["recommended_page"])}</td></tr>'
                )
            body.append('</table>')
        else:
            body.append(f'<p class="muted">No commercial content gaps detected for '
                        f'{_html_escape(domain)} (or SERP mapping was skipped).</p>')
    else:
        body.append('<p class="muted">No domain specified — commercial gap analysis skipped.</p>')

    # Recommendations
    body.append('<h2>Recommendations for Content Creation by Intent</h2>')
    for i, rec in enumerate(recommendations, 1):
        priority_color = {"High": "#DC2626", "Medium": "#D97706", "Low": "#16A34A"}.get(rec["priority"], "#6B7280")
        body.append(f'<div class="rec-card">')
        body.append(f'<div class="rec-head"><span class="rec-priority" '
                     f'style="background:{priority_color}">{rec["priority"]}</span>'
                     f'<span class="rec-intent">{rec["intent"]}</span>'
                     f'<span class="rec-num">#{i}</span></div>')
        body.append(f'<div class="rec-title">{_html_escape(rec["title"])}</div>')
        body.append(f'<div class="rec-detail">{_html_escape(rec["detail"])}</div>')
        body.append('</div>')

    # --- Assemble full HTML ---
    html = HTML_TEMPLATE.format(
        seed=_html_escape(seed_keyword),
        date_generated=date_gen,
        body="\n".join(body),
    )

    html_path = os.path.join(output_path, f"intent_map_{slug}.html")
    with open(html_path, "w") as f:
        f.write(html)

    # Also save JSON data
    json_path = os.path.join(output_path, f"intent_map_{slug}.json")
    json_data = {
        "seed_keyword": seed_keyword,
        "domain": domain,
        "generated": date_gen,
        "market_overview": {
            "total_keywords": total_kw,
            "total_volume": total_vol,
            "avg_difficulty": avg_diff,
            "avg_cpc": avg_cpc,
        },
        "intent_distribution": {intent: dict(intent_stats[intent]) for intent in INTENT_ORDER},
        "page_type_aggregate": aggregate if aggregate else {},
        "recommendations": recommendations,
    }
    with open(json_path, "w") as f:
        json.dump(json_data, f, indent=2, default=str)

    print(f"\nReport generated: {html_path}")
    print(f"JSON data: {json_path}")
    return html_path


# ---------------------------------------------------------------------------
# HTML template (white SaaS theme)
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="robots" content="noindex,nofollow">
<title>Search Intent Map — {seed}</title>
<style>
  :root {{ --primary:#1F2937; --accent:#2563EB; --success:#16A34A; --warn:#D97706;
          --bg:#F3F4F6; --muted:#6B7280; --grid:#E5E7EB; --soft:#EFF6FF; }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
         margin:0; color:var(--primary); background:#fff; line-height:1.5; }}
  .wrap {{ max-width:1100px; margin:0 auto; padding:32px; }}
  header {{ border-bottom:3px solid var(--accent); padding-bottom:16px; margin-bottom:24px; }}
  h1 {{ margin:0; font-size:28px; }}
  .meta {{ color:var(--muted); font-size:13px; margin-top:4px; }}
  h2 {{ color:var(--accent); border-bottom:1px solid var(--grid); padding-bottom:6px; margin-top:36px; }}
  .kpis {{ display:flex; gap:12px; flex-wrap:wrap; margin:16px 0; }}
  .kpi {{ background:var(--bg); border-top:3px solid var(--accent); border-radius:6px;
          padding:14px 18px; min-width:160px; flex:1; }}
  .kpi .label {{ font-size:11px; text-transform:uppercase; color:var(--muted); letter-spacing:.04em; }}
  .kpi .value {{ font-size:22px; font-weight:700; margin-top:4px; }}
  .kpi .sub {{ font-size:11px; color:var(--muted); }}
  table {{ width:100%; border-collapse:collapse; margin:12px 0; font-size:13px; }}
  th {{ background:var(--primary); color:#fff; text-align:left; padding:8px 10px; font-weight:600; }}
  td {{ padding:8px 10px; border-bottom:1px solid var(--grid); vertical-align:top; }}
  tr:nth-child(even) td {{ background:var(--bg); }}
  .chart-row {{ display:flex; gap:24px; align-items:center; flex-wrap:wrap; }}
  .chart-cell {{ flex:1; min-width:280px; }}
  .funnel-wrap {{ overflow-x:auto; }}
  .dot {{ display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; vertical-align:middle; }}
  .muted {{ color:var(--muted); font-size:13px; }}
  .filter-bar {{ background:var(--bg); padding:10px 14px; border-radius:6px; margin:12px 0; }}
  .filter-bar select, .filter-bar input {{ font-size:13px; border:1px solid var(--grid); border-radius:4px; }}
  #kwTable {{ max-height:600px; overflow-y:auto; }}
  .rec-card {{ border:1px solid var(--grid); border-radius:6px; margin:12px 0; overflow:hidden; }}
  .rec-head {{ background:var(--bg); padding:8px 14px; display:flex; align-items:center; gap:10px; }}
  .rec-priority {{ color:#fff; font-size:11px; font-weight:600; padding:2px 8px; border-radius:10px; }}
  .rec-intent {{ font-size:12px; color:var(--muted); }}
  .rec-num {{ margin-left:auto; font-size:12px; color:var(--muted); }}
  .rec-title {{ padding:10px 14px 4px; font-weight:600; font-size:14px; }}
  .rec-detail {{ padding:4px 14px 12px; font-size:13px; color:var(--primary); }}
  footer {{ margin-top:40px; padding-top:16px; border-top:1px solid var(--grid);
           color:var(--muted); font-size:11px; text-align:center; }}
  script {{ display:none; }}
</style>
<script>
function filterKeywords() {{
  var intent = document.getElementById('intentFilter').value;
  var search = document.getElementById('kwSearch').value.toLowerCase();
  var rows = document.querySelectorAll('.kw-row');
  rows.forEach(function(row) {{
    var show = (intent === 'all' || row.dataset.intent === intent) &&
               (search === '' || row.dataset.kw.includes(search));
    row.style.display = show ? '' : 'none';
  }});
}}
</script>
</head><body><div class="wrap">
{body}
<footer>Generated {date_generated} • DataForSEO data • Search Intent Map — noindex/nofollow</footer>
</div></body></html>"""


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run(seed_keyword, domain=None, output_path="/tmp/test_intent_map",
        limit=500, location_name=None, language_code=None, fetch_serp=True,
        serp_sample=50):
    """Full pipeline: fetch → classify → map → report."""
    load_env()

    print(f"[1/4] Fetching market keywords for '{seed_keyword}' (limit={limit}) ...")
    keywords = fetch_market_keywords(seed_keyword, limit=limit,
                                     location_name=location_name,
                                     language_code=language_code)
    print(f"  → {len(keywords)} keywords retrieved")

    if not keywords:
        sys.stderr.write("No keywords returned from DataForSEO. Check credentials and seed.\n")
        return None

    print(f"[2/4] Classifying intent ...")
    keywords = classify_intent(keywords)
    intent_counts = Counter(k.get("intent", "informational") for k in keywords)
    for intent in INTENT_ORDER:
        print(f"  {intent}: {intent_counts.get(intent, 0)}")

    print(f"[3/4] Mapping SERP page types (sample={serp_sample}) ...")
    if fetch_serp:
        per_keyword, aggregate = map_page_types(keywords, sample_size=serp_sample,
                                                location_name=location_name,
                                                language_code=language_code)
        print(f"  → {len(per_keyword)} keywords SERP-mapped")
    else:
        per_keyword, aggregate = {}, {}
        print("  → SERP mapping skipped (--no-serp)")

    print(f"[4/4] Generating report ...")
    html_path = generate_intent_report(seed_keyword, keywords,
                                        (per_keyword, aggregate), output_path,
                                        domain=domain,
                                        location_name=location_name,
                                        language_code=language_code)
    return html_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Map search intent distribution across an entire market.")
    ap.add_argument("--seed", required=True, help="Seed keyword to expand")
    ap.add_argument("--domain", default=None,
                    help="Target domain for content gap analysis (e.g. softprowatersystems.com)")
    ap.add_argument("--output", default="/tmp/test_intent_map", help="Output directory")
    ap.add_argument("--limit", type=int, default=DEFAULTS["limit"],
                    help=f"Max keywords to expand (default {DEFAULTS['limit']})")
    ap.add_argument("--location", default=DEFAULTS["location_name"],
                    help="Location name (default United States)")
    ap.add_argument("--language", default=DEFAULTS["language_code"],
                    help="Language code (default en)")
    ap.add_argument("--no-serp", action="store_true",
                    help="Skip live SERP page-type mapping (saves API credits)")
    ap.add_argument("--serp-sample", type=int, default=50,
                    help="Number of top keywords to SERP map (default 50)")
    args = ap.parse_args()

    html_path = run(
        seed_keyword=args.seed,
        domain=args.domain,
        output_path=args.output,
        limit=args.limit,
        location_name=args.location,
        language_code=args.language,
        fetch_serp=not args.no_serp,
        serp_sample=args.serp_sample,
    )

    if html_path:
        print(f"\n✓ Done — Report: {html_path}")
    else:
        print("\n✗ Report generation failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
