#!/usr/bin/env python3
"""
Competitor Content Gap Analyzer
================================
Standalone Python script that finds pages competitors rank for that the
client doesn't, classifies the opportunities, identifies existing client
pages that could be optimized, and renders a white SaaS themed HTML report
for content creation planning.

Data sources (DataForSEO Labs):
  1. POST /v3/dataforseo_labs/google/domain_intersection/live
       — gap keywords (competitor ranks, client doesn't) when
         intersections=false, target1=competitor, target2=client.
       — shared-keyword total count when intersections=true, limit=1.
  2. POST /v3/dataforseo_labs/google/ranked_keywords/live
       — full client ranking-keyword set (used for matching client pages
         that could be optimized to capture each gap keyword).

Public API:
  discover_competitor_keywords(client_domain, competitor_domain, location_code=2840)
  classify_content_opportunities(keywords, client_domain)
  identify_matching_client_pages(keywords, client_domain)
  generate_content_gap_report(client_domain, competitors, gaps, output_path)

Usage:
  python3 competitor_content_gap.py \
      --client softprowatersystems.com \
      --competitors aquasana.com,culligan.com,springwellwater.com \
      --output /tmp/test_content_gap

Environment:
  DATAFORSEO_LOGIN    - API login email  (from ~/.hermes/.env)
  DATAFORSEO_PASSWORD - API password     (from ~/.hermes/.env)
"""

import os
import sys
import json
import math
import re
import time
import base64
import argparse
import urllib.request
import urllib.error
from datetime import datetime
from collections import defaultdict, Counter
from urllib.parse import urlparse

# =====================================================================
# CONFIG
# =====================================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOCATION_CODE = 2840        # United States
DEFAULT_LANGUAGE_CODE = 'en'
API_BASE = 'https://api.dataforseo.com/v3'
API_TIMEOUT = 120
RATE_LIMIT_DELAY = 0.5              # seconds between API calls
DEFAULT_GAP_LIMIT = 500             # gap keywords per competitor
DEFAULT_CLIENT_KW_LIMIT = 1000       # client ranked-keyword set size
DEFAULT_TOP_N = 50                   # top opportunities shown in detail table

# Standard CTR curve for ETV estimation (position -> CTR)
CTR_CURVE = {
    1: 0.27, 2: 0.15, 3: 0.11, 4: 0.08, 5: 0.06,
    6: 0.04, 7: 0.035, 8: 0.03, 9: 0.025, 10: 0.02,
}
DEFAULT_CTR = 0.015  # position 11+

INTENT_COLORS = {
    "informational":   "#2563EB",
    "commercial":      "#D97706",
    "transactional":   "#16A34A",
}

PAGE_TYPE_LABELS = {
    "product":       "Product Page",
    "blog":          "Blog Post",
    "collection":    "Collection Page",
    "comparison":    "Comparison / Review",
    "homepage":      "Homepage",
    "other":         "Other",
}

PAGE_TYPE_COLORS = {
    "product":     "#16A34A",
    "blog":         "#2563EB",
    "collection":   "#7C3AED",
    "comparison":   "#D97706",
    "homepage":     "#6B7280",
    "other":        "#9CA3AF",
}

STOPWORDS = {
    'the', 'a', 'an', 'is', 'are', 'for', 'and', 'to', 'in', 'of', 'with',
    'how', 'what', 'why', 'when', 'where', 'who', 'best', 'top', 'vs',
    'vs.', 'versus', 'on', 'or', 'your', 'my', 'our', 'this', 'that',
    'it', 'its', 'be', 'as', 'at', 'by', 'from', 'up', 'do', 'does',
    'can', 'should', 'will', 'i', 'you', 'he', 'she', 'they', 'we',
}

_INTENT_PATTERNS = {
    "transactional": [
        r"\b(buy|purchase|order|shop|cheap|discount|deal|coupon|for sale|price)\b",
        r"\b(bestseller|best seller|on sale|free shipping|cost)\b",
    ],
    "commercial": [
        r"\b(best|top|review|reviews|compare|comparison|alternative)\b",
        r"\b(rating|rated|recommend|recommended|which|what.*best)\b",
    ],
    "informational": [
        r"\b(how|what|why|when|where|who|guide|tutorial|tips|advice)\b",
        r"\b(learn|understand|explain|meaning|definition|examples?|info)\b",
        r"\b(is|are|can|does|do|should|will)\b",
    ],
}


# =====================================================================
# CREDENTIALS
# =====================================================================
def get_creds():
    """Load DataForSEO credentials from environment or ~/.hermes/.env."""
    login = os.environ.get('DATAFORSEO_LOGIN')
    password = os.environ.get('DATAFORSEO_PASSWORD')
    if not login or not password:
        env_path = os.path.expanduser('~/.hermes/.env')
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('DATAFORSEO_LOGIN='):
                        login = line.split('=', 1)[1].strip().strip('"').strip("'")
                    elif line.startswith('DATAFORSEO_PASSWORD='):
                        password = line.split('=', 1)[1].strip().strip('"').strip("'")
    if not login or not password:
        print("ERROR: DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD not found")
        print("Set them in ~/.hermes/.env or as environment variables")
        sys.exit(1)
    return login, password


# =====================================================================
# LOW-LEVEL API CALL
# =====================================================================
def dfs_api_call(endpoint, payload=None, method='POST'):
    """Make a DataForSEO API call. Payload is an array of task objects."""
    login, password = get_creds()
    creds = base64.b64encode(f'{login}:{password}'.encode()).decode()
    url = f'{API_BASE}/{endpoint}'
    headers = {
        'Authorization': f'Basic {creds}',
        'Content-Type': 'application/json',
    }
    if method == 'POST' and payload is not None:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers=headers)
    else:
        req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            data = json.loads(resp.read())
        if data.get('status_code') != 20000:
            print(f"  API Error: {data.get('status_code')} - "
                  f"{data.get('status_message')}")
            return None
        return data
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')[:500]
        print(f"  HTTP Error {e.code}: {body}")
        return None
    except Exception as e:
        print(f"  Request error: {e}")
        return None


def _extract_first_result(data):
    """Extract the first result object from a DataForSEO response."""
    if not data or not data.get('tasks'):
        return None
    task = data['tasks'][0]
    if task.get('status_code') and task.get('status_code') != 20000:
        print(f"  Task error: {task.get('status_message', 'unknown')}")
    results = task.get('result', [])
    if not results:
        return None
    return results[0] if isinstance(results, list) else results


def _rate_limit():
    """Pause between API calls to respect rate limits."""
    time.sleep(RATE_LIMIT_DELAY)


# =====================================================================
# HELPERS
# =====================================================================
def _clean_domain(domain):
    """Strip protocol, www, and trailing slash."""
    return (domain.replace('https://', '').replace('http://', '')
            .replace('www.', '').rstrip('/'))


def _ctr_for_position(pos):
    """Approximate CTR for a SERP position (used for ETV estimation)."""
    if pos is None or pos <= 0:
        return 0.0
    return CTR_CURVE.get(pos, DEFAULT_CTR)


def _estimate_etv(volume, position):
    """Estimate traffic value = monthly volume * CTR for the position."""
    if not volume:
        return 0.0
    return round(volume * _ctr_for_position(position), 2)


def _heuristic_intent(keyword):
    """Classify intent from keyword text patterns."""
    kw = keyword.lower()
    for intent in ("transactional", "commercial", "informational"):
        for pat in _INTENT_PATTERNS.get(intent, []):
            if re.search(pat, kw):
                return intent
    return "informational"


def _classify_page_type(url, title, description=""):
    """
    Classify a single SERP result's page type from URL path and title.
    Returns one of: product, blog, collection, comparison, homepage, other.
    """
    u = (url or "").lower()
    t = (title or "").lower()
    d = (description or "").lower()
    combined = u + " " + t + " " + d

    # Comparison / review (check first — these often co-occur with product/blog)
    if any(x in combined for x in [
        "review", "reviews", "best ", "top 10", "top 5", "comparison",
        "compare", "vs ", "versus ", "alternatives", "buying guide",
        "editor's choice",
    ]):
        # but a review article on a blog path is still a blog post
        if any(x in u for x in ["/blog", "/post", "/article", "/guide", "/learn"]):
            return "blog"
        if any(x in u for x in ["/collections/", "/category/", "/shop/"]):
            return "collection"
        if any(x in u for x in ["/products/", "/p/", "/dp/"]):
            return "product"
        return "comparison"

    # Blog / informational
    if any(x in u for x in ["/blog", "/post", "/article", "/news",
                            "/guide", "/learn", "/resources"]):
        return "blog"
    if any(x in t for x in ["how to", "what is", "why ", "guide to",
                            "tips for", "ultimate guide"]):
        return "blog"

    # Collection / category
    if any(x in u for x in ["/collections/", "/category/", "/c/",
                            "/shop/", "/catalog"]):
        return "collection"

    # Product page
    if any(x in u for x in ["/products/", "/p/", "/dp/", "/pd/",
                            "/item/", "/listing/"]):
        return "product"

    # Homepage
    try:
        parsed = urlparse(u)
        path = (parsed.path or "").strip("/")
        if not path or path in ("index.html", "index.php", "home"):
            return "homepage"
    except Exception:
        pass

    return "other"


def _significant_tokens(keyword, min_len=3):
    """Return significant lowercase word tokens from a keyword string."""
    tokens = re.findall(r"[a-z0-9]+", keyword.lower())
    return [t for t in tokens if len(t) >= min_len and t not in STOPWORDS]


def _html_escape(text):
    """Escape text for safe HTML insertion."""
    if text is None:
        return ""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _fmt(n):
    """Format an integer with thousands separators."""
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n) if n is not None else "—"


def _now():
    return datetime.now()


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "report"


# =====================================================================
# 1. discover_competitor_keywords
# =====================================================================
def discover_competitor_keywords(client_domain, competitor_domain,
                                location_code=DEFAULT_LOCATION_CODE):
    """
    Use DataForSEO domain_intersection to find keywords the competitor
    ranks for but the client doesn't.

    Calls the Labs domain_intersection endpoint with:
        target1 = competitor_domain (ranks)
        target2 = client_domain    (doesn't rank — gap target)
        intersections = false

    Returns a list of keyword dicts with:
        keyword, volume, difficulty (competition), cpc,
        competitor_position, competitor_url, competitor_domain,
        competitor_title, competitor_description, etv,
        shared_count (int — total shared keywords between the two domains)
    """
    client_domain = _clean_domain(client_domain)
    competitor_domain = _clean_domain(competitor_domain)
    print(f"  -> Discovering gap keywords: {competitor_domain} vs "
          f"{client_domain} ...")

    keywords = []
    shared_count = 0

    # --- Gap call: competitor ranks, client does not ---
    payload = [{
        'target1': competitor_domain,
        'target2': client_domain,
        'location_code': location_code,
        'language_code': DEFAULT_LANGUAGE_CODE,
        'intersections': False,
        'limit': DEFAULT_GAP_LIMIT,
        'order_by': ['keyword_data.keyword_info.search_volume,desc'],
    }]
    data = dfs_api_call(
        'dataforseo_labs/google/domain_intersection/live', payload)
    result = _extract_first_result(data)
    if result:
        shared_count = result.get('total_count', 0) or 0
        items = result.get('items', []) or []
        for item in items:
            kw_data = item.get('keyword_data', {}) or {}
            kw_info = kw_data.get('keyword_info', {}) or {}
            serp = item.get('first_domain_serp_element', {}) or {}
            volume = kw_info.get('search_volume', 0) or 0
            position = serp.get('rank_absolute') or 0
            keywords.append({
                'keyword': kw_data.get('keyword', ''),
                'volume': volume,
                'difficulty': round(kw_info.get('competition', 0) or 0, 1),
                'cpc': kw_info.get('cpc', 0) or 0,
                'competitor_position': position,
                'competitor_url': serp.get('url', '') or '',
                'competitor_domain': serp.get('domain', '') or competitor_domain,
                'competitor_title': serp.get('title', '') or '',
                'competitor_description': serp.get('description', '') or '',
                'etv': _estimate_etv(volume, position),
            })

    _rate_limit()

    # --- Shared-keyword count call: both rank (limit=1, just need total) ---
    payload_shared = [{
        'target1': competitor_domain,
        'target2': client_domain,
        'location_code': location_code,
        'language_code': DEFAULT_LANGUAGE_CODE,
        'intersections': True,
        'limit': 1,
    }]
    data_shared = dfs_api_call(
        'dataforseo_labs/google/domain_intersection/live', payload_shared)
    result_shared = _extract_first_result(data_shared)
    if result_shared:
        shared_count = result_shared.get('total_count', shared_count) or shared_count

    # Stamp shared_count onto every keyword for downstream reporting
    for k in keywords:
        k['shared_count'] = shared_count

    print(f"  v {len(keywords)} gap keywords | {shared_count} shared keywords")
    return keywords


# =====================================================================
# 2. classify_content_opportunities
# =====================================================================
def classify_content_opportunities(keywords, client_domain):
    """
    Classify each gap keyword by:
      - page_type: product, blog, collection, comparison/review, homepage, other
        (inferred from the competitor's ranking URL + title)
      - intent: informational, commercial, transactional
      - priority_score: volume * (1 - difficulty/100)  — higher = better

    Mutates and returns the keywords list (adds page_type, intent,
    priority_score fields). Sorted by priority_score desc.
    """
    for k in keywords:
        k['page_type'] = _classify_page_type(
            k.get('competitor_url', ''),
            k.get('competitor_title', ''),
            k.get('competitor_description', ''),
        )
        k['intent'] = _heuristic_intent(k.get('keyword', ''))
        volume = k.get('volume', 0) or 0
        difficulty = k.get('difficulty', 0) or 0
        # difficulty is on a 0-100 scale (DataForSEO competition index)
        k['priority_score'] = round(volume * (1 - max(0, min(100, difficulty)) / 100), 2)

    keywords.sort(key=lambda x: x.get('priority_score', 0), reverse=True)
    return keywords


# =====================================================================
# 3. identify_matching_client_pages
# =====================================================================
def identify_matching_client_pages(keywords, client_domain,
                                   location_code=DEFAULT_LOCATION_CODE):
    """
    For each gap keyword, check if the client has any existing page that
    could be optimized to rank for it. Strategy:
      1. Fetch the client's full ranked-keyword set via ranked_keywords/live.
      2. Build an index: client keyword -> {url, position, volume}.
      3. For each gap keyword:
           - exact match: client ranks for the same keyword (already covered
             by the domain_intersection gap filter — should be rare, but we
             still flag if present from the ranked set).
           - related match: shares >= 2 significant tokens with a client
             keyword -> the client's ranking URL is a candidate to optimize.
    Mutates and returns the keywords list (adds matching_client_url,
    matching_client_position, match_type fields; None when no match).
    """
    client_domain = _clean_domain(client_domain)
    print(f"  -> Fetching client ranked keywords for {client_domain} ...")

    payload = [{
        'target': client_domain,
        'location_code': location_code,
        'language_code': DEFAULT_LANGUAGE_CODE,
        'limit': DEFAULT_CLIENT_KW_LIMIT,
        'item_types': ['organic'],
        'order_by': ['keyword_data.keyword_info.search_volume,desc'],
    }]
    data = dfs_api_call(
        'dataforseo_labs/google/ranked_keywords/live', payload)
    result = _extract_first_result(data)

    client_kws = {}    # keyword string -> {url, position, volume}
    client_tokens = {}  # token -> list of (keyword, url, position)
    if result:
        items = result.get('items', []) or []
        for item in items:
            kw_data = item.get('keyword_data', {}) or {}
            kw_info = kw_data.get('keyword_info', {}) or {}
            kw = kw_data.get('keyword', '')
            serp = item.get('ranked_serp_element', {}) or {}
            serp_item = serp.get('serp_item', {}) or {}
            url = serp_item.get('url', '') or serp_item.get('relative_url', '')
            pos = serp_item.get('rank_absolute', 0) or 0
            vol = kw_info.get('search_volume', 0) or 0
            if not kw:
                continue
            client_kws[kw.lower()] = {'url': url, 'position': pos, 'volume': vol}
            for tok in _significant_tokens(kw):
                client_tokens.setdefault(tok, []).append((kw, url, pos))
        print(f"  v {len(client_kws)} client ranked keywords indexed")
    else:
        print("  x No client ranked-keyword data — match step skipped")
        for k in keywords:
            k['matching_client_url'] = None
            k['matching_client_position'] = None
            k['match_type'] = 'none'
        return keywords

    # Match each gap keyword against the client index
    for k in keywords:
        kw_lower = (k.get('keyword', '') or '').lower()
        # Exact match
        if kw_lower in client_kws:
            info = client_kws[kw_lower]
            k['matching_client_url'] = info['url']
            k['matching_client_position'] = info['position']
            k['match_type'] = 'exact'
            continue

        # Related match: shares >= 2 significant tokens with a client keyword
        gap_tokens = _significant_tokens(k.get('keyword', ''))
        candidate_scores = defaultdict(int)  # (kw, url) -> overlap count
        for tok in gap_tokens:
            for (c_kw, c_url, c_pos) in client_tokens.get(tok, []):
                candidate_scores[(c_kw, c_url, c_pos)] += 1

        best = None
        best_overlap = 0
        for (c_kw, c_url, c_pos), overlap in candidate_scores.items():
            if overlap >= 2 and overlap > best_overlap:
                best = (c_kw, c_url, c_pos)
                best_overlap = overlap

        if best:
            k['matching_client_url'] = best[1]
            k['matching_client_position'] = best[2]
            k['match_type'] = 'related'
        else:
            k['matching_client_url'] = None
            k['matching_client_position'] = None
            k['match_type'] = 'none'

    matched = sum(1 for k in keywords if k['match_type'] != 'none')
    print(f"  v {matched}/{len(keywords)} gap keywords have a candidate client page")
    return keywords


# =====================================================================
# CONTENT CLUSTERS
# =====================================================================
def _build_content_clusters(keywords, min_cluster_size=3):
    """
    Greedy token-frequency clustering. Each cluster is named after the
    most-frequent significant token still available; keywords containing
    that token are pulled into the cluster. Returns a list of cluster
    dicts sorted by total_volume desc:
        {name, keywords: [...], total_volume, count, avg_difficulty}
    Keywords not fitting any cluster are placed in an 'Other' cluster.
    """
    remaining = list(keywords)
    clusters = []

    while len(remaining) >= min_cluster_size:
        token_freq = Counter()
        for k in remaining:
            for tok in set(_significant_tokens(k.get('keyword', ''))):
                token_freq[tok] += 1
        if not token_freq:
            break
        top_token, top_count = token_freq.most_common(1)[0]
        if top_count < min_cluster_size:
            break
        members = [k for k in remaining
                   if top_token in _significant_tokens(k.get('keyword', ''))]
        if len(members) < min_cluster_size:
            break
        clusters.append({
            'name': top_token,
            'keywords': members,
            'total_volume': sum(k.get('volume', 0) or 0 for k in members),
            'count': len(members),
            'avg_difficulty': round(
                sum(k.get('difficulty', 0) or 0 for k in members) / len(members), 1
            ) if members else 0,
        })
        remaining = [k for k in remaining if k not in members]

    if remaining:
        clusters.append({
            'name': 'other',
            'keywords': remaining,
            'total_volume': sum(k.get('volume', 0) or 0 for k in remaining),
            'count': len(remaining),
            'avg_difficulty': round(
                sum(k.get('difficulty', 0) or 0 for k in remaining) / len(remaining), 1
            ) if remaining else 0,
        })

    clusters.sort(key=lambda c: c['total_volume'], reverse=True)
    return clusters


# =====================================================================
# COMPETITOR PAGE-TYPE AGGREGATION
# =====================================================================
def _competitor_page_analysis(gaps):
    """Per-competitor page-type breakdown of ranking gap URLs."""
    per_comp = {}
    for comp, kws in gaps.items():
        type_counts = Counter(k.get('page_type', 'other') for k in kws)
        per_comp[comp] = {
            'total': len(kws),
            'by_type': dict(type_counts),
            'top_type': type_counts.most_common(1)[0][0] if type_counts else 'other',
        }
    return per_comp


# =====================================================================
# RECOMMENDATIONS
# =====================================================================
def _build_recommendations(client_domain, competitors, gaps, clusters):
    """
    Generate prioritized content-creation recommendations from the top
    gap opportunities + content clusters.
    """
    recs = []
    client_domain = _clean_domain(client_domain)

    # 1) Top 5 gap keywords with NO matching client page -> create new content
    new_opps = [k for k in
                sorted([k for comp in gaps for k in gaps[comp]],
                       key=lambda x: x.get('priority_score', 0), reverse=True)
                if k.get('match_type', 'none') == 'none'][:5]
    for k in new_opps:
        page_label = PAGE_TYPE_LABELS.get(k.get('page_type', 'other'), 'Page')
        recs.append({
            'priority': 'High',
            'action': 'Create',
            'page_type': page_label,
            'keyword': k.get('keyword', ''),
            'volume': k.get('volume', 0),
            'difficulty': k.get('difficulty', 0),
            'competitor': k.get('competitor_domain', ''),
            'detail': (f"Create a new {page_label.lower()} targeting "
                       f"'{k.get('keyword', '')}' — "
                       f"{k.get('competitor_domain', '')} ranks #{k.get('competitor_position', '—')}. "
                       f"Volume {k.get('volume', 0):,}/mo, difficulty "
                       f"{k.get('difficulty', 0)}/100."),
        })

    # 2) Top 5 gap keywords WITH a matching client page -> optimize existing
    optimize_opps = [k for k in
                     sorted([k for comp in gaps for k in gaps[comp]],
                            key=lambda x: x.get('priority_score', 0), reverse=True)
                     if k.get('match_type', 'none') != 'none'][:5]
    for k in optimize_opps:
        page_label = PAGE_TYPE_LABELS.get(k.get('page_type', 'other'), 'Page')
        recs.append({
            'priority': 'Medium',
            'action': 'Optimize',
            'page_type': page_label,
            'keyword': k.get('keyword', ''),
            'volume': k.get('volume', 0),
            'difficulty': k.get('difficulty', 0),
            'competitor': k.get('competitor_domain', ''),
            'detail': (f"Optimize existing client page "
                       f"{k.get('matching_client_url', '')} to also target "
                       f"'{k.get('keyword', '')}'. Currently ranks for a "
                       f"related term — competitor {k.get('competitor_domain', '')} "
                       f"ranks #{k.get('competitor_position', '—')}."),
        })

    # 3) Top 3 content clusters -> batch content production
    for c in clusters[:3]:
        if c['name'] == 'other' or c['count'] < 3:
            continue
        sample_kws = ', '.join(
            f"'{k.get('keyword', '')}'" for k in c['keywords'][:5])
        recs.append({
            'priority': 'Medium',
            'action': 'Cluster',
            'page_type': 'Topic Cluster',
            'keyword': c['name'],
            'volume': c['total_volume'],
            'difficulty': c['avg_difficulty'],
            'competitor': '',
            'detail': (f"Build a topic cluster around '{c['name']}' — "
                       f"{c['count']} gap keywords, "
                       f"{c['total_volume']:,} total monthly volume. "
                       f"Sample keywords: {sample_kws}."),
        })

    return recs


# =====================================================================
# 4. generate_content_gap_report
# =====================================================================
def generate_content_gap_report(client_domain, competitors, gaps, output_path):
    """
    Render the white-SaaS-themed HTML content-gap report.

    Args:
        client_domain — the client site (gap target)
        competitors   — list of competitor domain strings
        gaps          — dict: {competitor_domain: [keyword dicts, ...]}
                        each keyword dict already classified (page_type,
                        intent, priority_score) and matched
                        (matching_client_url, match_type)
        output_path   — directory to write the report into

    Returns the path to the generated HTML file.
    """
    os.makedirs(output_path, exist_ok=True)
    date_gen = _now().strftime("%Y-%m-%d %H:%M")
    client_domain = _clean_domain(client_domain)
    slug = _slug(client_domain)

    # --- Flatten all gap keywords across competitors ---
    all_gaps = [k for comp in gaps for k in gaps[comp]]

    # --- Summary stats ---
    total_gap_kw = len(all_gaps)
    total_gap_etv = round(sum(k.get('etv', 0) or 0 for k in all_gaps), 2)
    total_gap_volume = sum(k.get('volume', 0) or 0 for k in all_gaps)
    top_competitor = max(gaps, key=lambda c: len(gaps[c])) if gaps else "—"
    top_competitor_count = len(gaps.get(top_competitor, [])) if gaps else 0

    # --- Per-competitor table ---
    per_comp_rows = []
    for comp in competitors:
        comp_clean = _clean_domain(comp)
        kws = gaps.get(comp_clean, [])
        gap_count = len(kws)
        gap_etv = round(sum(k.get('etv', 0) or 0 for k in kws), 2)
        shared = kws[0].get('shared_count', 0) if kws else 0
        per_comp_rows.append({
            'competitor': comp_clean,
            'shared': shared,
            'gap_count': gap_count,
            'gap_etv': gap_etv,
        })

    # --- Top gap opportunities (top N by priority_score) ---
    top_n = sorted(all_gaps, key=lambda x: x.get('priority_score', 0),
                   reverse=True)[:DEFAULT_TOP_N]

    # --- Content clusters ---
    clusters = _build_content_clusters(all_gaps)

    # --- Competitor page analysis ---
    comp_page = _competitor_page_analysis(gaps)

    # --- Recommendations ---
    recommendations = _build_recommendations(client_domain, competitors,
                                             gaps, clusters)

    # --- Intent distribution ---
    intent_counts = Counter(k.get('intent', 'informational') for k in all_gaps)
    intent_volume = defaultdict(int)
    for k in all_gaps:
        intent_volume[k.get('intent', 'informational')] += k.get('volume', 0) or 0

    # --- Page-type distribution ---
    page_type_counts = Counter(k.get('page_type', 'other') for k in all_gaps)

    # =================================================================
    # Build HTML body
    # =================================================================
    body = []

    # Header
    body.append(f'<header><h1>Competitor Content Gap — '
                f'{_html_escape(client_domain)}</h1>')
    body.append(f'<div class="meta">{date_gen} • US / en • '
                f'{len(competitors)} competitors • '
                f'{total_gap_kw} gap keywords</div></header>')

    # Summary KPIs
    body.append('<div class="kpis">')
    kpis = [
        ("Total Gap Keywords", _fmt(total_gap_kw), "competitor ranks, client doesn't"),
        ("Total Gap Traffic (ETV)", _fmt(int(total_gap_etv)), "estimated monthly visits"),
        ("Total Gap Volume", _fmt(total_gap_volume), "monthly searches"),
        ("Top Competitor (most gaps)", _html_escape(top_competitor),
         f"{_fmt(top_competitor_count)} gap keywords"),
    ]
    for label, val, sub in kpis:
        body.append(f'<div class="kpi"><div class="label">{label}</div>'
                     f'<div class="value">{val}</div>'
                     f'<div class="sub">{sub}</div></div>')
    body.append('</div>')

    # Gap by competitor table
    body.append('<h2>Gap by Competitor</h2>')
    body.append('<p class="muted">For each competitor: shared keywords (both '
                'rank), gap keywords (competitor ranks, client doesn\'t), and '
                'estimated gap traffic.</p>')
    body.append('<table><thead><tr>'
                '<th>Competitor</th><th>Shared Keywords</th>'
                '<th>Gap Keywords</th><th>Gap ETV</th>'
                '</tr></thead><tbody>')
    for row in per_comp_rows:
        body.append(
            f'<tr><td><b>{_html_escape(row["competitor"])}</b></td>'
            f'<td>{_fmt(row["shared"])}</td>'
            f'<td>{_fmt(row["gap_count"])}</td>'
            f'<td>{_fmt(int(row["gap_etv"]))}</td></tr>')
    body.append('</tbody></table>')

    # Intent distribution (compact)
    body.append('<h2>Gap Keyword Intent Distribution</h2>')
    body.append('<div class="intent-row">')
    for intent in ("informational", "commercial", "transactional"):
        cnt = intent_counts.get(intent, 0)
        vol = intent_volume.get(intent, 0)
        color = INTENT_COLORS.get(intent, "#9CA3AF")
        body.append(
            f'<div class="intent-card" style="border-top:3px solid {color}">'
            f'<div class="label">{intent.title()}</div>'
            f'<div class="value">{_fmt(cnt)}</div>'
            f'<div class="sub">{_fmt(vol)} volume</div></div>')
    body.append('</div>')

    # Page-type distribution
    body.append('<h2>What Type of Content Is Ranking (Competitor Pages)</h2>')
    body.append('<p class="muted">Breakdown of the page types competitors are '
                'using to rank for gap keywords.</p>')
    body.append('<table><thead><tr><th>Page Type</th><th>Count</th>'
                '<th>Share</th></tr></thead><tbody>')
    for ptype, cnt in page_type_counts.most_common():
        share = round(100 * cnt / max(total_gap_kw, 1), 1)
        color = PAGE_TYPE_COLORS.get(ptype, "#9CA3AF")
        label = PAGE_TYPE_LABELS.get(ptype, ptype)
        body.append(
            f'<tr><td><span class="dot" style="background:{color}"></span>'
            f'{_html_escape(label)}</td>'
            f'<td>{_fmt(cnt)}</td><td>{share}%</td></tr>')
    body.append('</tbody></table>')

    # Per-competitor page analysis
    body.append('<h2>Competitor Page Analysis</h2>')
    body.append('<table><thead><tr><th>Competitor</th><th>Total Gap KWs</th>'
                '<th>Product</th><th>Blog</th><th>Collection</th>'
                '<th>Comparison/Review</th><th>Other</th>'
                '<th>Top Type</th></tr></thead><tbody>')
    for comp, info in sorted(comp_page.items()):
        bt = info['by_type']
        body.append(
            f'<tr><td><b>{_html_escape(comp)}</b></td>'
            f'<td>{_fmt(info["total"])}</td>'
            f'<td>{_fmt(bt.get("product", 0))}</td>'
            f'<td>{_fmt(bt.get("blog", 0))}</td>'
            f'<td>{_fmt(bt.get("collection", 0))}</td>'
            f'<td>{_fmt(bt.get("comparison", 0))}</td>'
            f'<td>{_fmt(bt.get("other", 0)) + _fmt(bt.get("homepage", 0))}</td>'
            f'<td>{_html_escape(PAGE_TYPE_LABELS.get(info["top_type"], info["top_type"]))}</td></tr>')
    body.append('</tbody></table>')

    # Top gap opportunities (expandable)
    body.append('<h2>Top Gap Opportunities</h2>')
    body.append('<p class="muted">Top 50 gap keywords by priority score '
                '(volume × (1 − difficulty/100)). Click a row to expand.</p>')
    body.append('<table id="gapTable" class="expandable">'
                '<thead><tr><th>Keyword</th><th>Volume</th><th>Difficulty</th>'
                '<th>Competitor URL</th><th>Pos</th>'
                '<th>Suggested Page</th><th>Intent</th>'
                '<th>Priority</th></tr></thead><tbody>')
    for k in top_n:
        ptype = k.get('page_type', 'other')
        ptype_label = PAGE_TYPE_LABELS.get(ptype, ptype)
        ptype_color = PAGE_TYPE_COLORS.get(ptype, "#9CA3AF")
        intent = k.get('intent', 'informational')
        intent_color = INTENT_COLORS.get(intent, "#9CA3AF")
        comp_url = k.get('competitor_url', '')
        comp_url_short = comp_url.replace('https://', '').replace('http://', '')
        if len(comp_url_short) > 60:
            comp_url_short = comp_url_short[:57] + '...'
        priority = k.get('priority_score', 0)
        priority_color = ("#16A34A" if priority >= 1000 else
                          "#D97706" if priority >= 100 else "#6B7280")
        match_type = k.get('match_type', 'none')
        match_icon = ""
        if match_type == 'exact':
            match_icon = ' <span class="match exact" title="Client ranks for a related keyword">●</span>'
        elif match_type == 'related':
            match_icon = ' <span class="match related" title="Client has a related page">◐</span>'
        body.append(
            f'<tr class="gap-row">'
            f'<td><b>{_html_escape(k.get("keyword", ""))}</b>{match_icon}'
            f'<div class="row-detail">'
            f'<div>Competitor: {_html_escape(k.get("competitor_domain", ""))}</div>'
            f'<div>Competitor title: {_html_escape(k.get("competitor_title", ""))}</div>'
            f'<div>Competitor URL: <a href="{_html_escape(comp_url)}" target="_blank" rel="noopener nofollow">{_html_escape(comp_url_short)}</a></div>'
            f'<div>ETV: {_fmt(int(k.get("etv", 0)))}</div>'
            f'<div>CPC: ${k.get("cpc", 0):.2f}</div>'
            f'<div>Match: {match_type}'
            + (f' — client page: <a href="{_html_escape(k.get("matching_client_url", ""))}" target="_blank" rel="noopener nofollow">{_html_escape(k.get("matching_client_url", ""))}</a> '
               f'(pos {k.get("matching_client_position", "—")})'
               if match_type != 'none' else '')
            + '</div>'
            f'</div></td>'
            f'<td>{_fmt(k.get("volume", 0))}</td>'
            f'<td>{k.get("difficulty", "—")}</td>'
            f'<td><a href="{_html_escape(comp_url)}" target="_blank" rel="noopener nofollow">{_html_escape(comp_url_short)}</a></td>'
            f'<td>#{k.get("competitor_position", "—")}</td>'
            f'<td><span class="dot" style="background:{ptype_color}"></span>{_html_escape(ptype_label)}</td>'
            f'<td><span class="dot" style="background:{intent_color}"></span>{intent.title()}</td>'
            f'<td><b style="color:{priority_color}">{_fmt(int(priority))}</b></td>'
            f'</tr>')
    body.append('</tbody></table>')

    # Content clusters
    body.append('<h2>Content Clusters for Content Creation Planning</h2>')
    body.append('<p class="muted">Gap keywords grouped into topic clusters by '
                'shared significant word. Build a pillar + supporting pages per '
                'cluster.</p>')
    body.append('<table><thead><tr><th>Cluster</th><th>Keywords</th>'
                '<th>Total Volume</th><th>Avg Difficulty</th>'
                '<th>Sample Keywords</th></tr></thead><tbody>')
    for c in clusters:
        sample = '; '.join(k.get('keyword', '') for k in c['keywords'][:5])
        if c['count'] > 5:
            sample += f" (+{c['count'] - 5} more)"
        body.append(
            f'<tr><td><b>{_html_escape(c["name"].title())}</b></td>'
            f'<td>{_fmt(c["count"])}</td>'
            f'<td>{_fmt(c["total_volume"])}</td>'
            f'<td>{c["avg_difficulty"]}</td>'
            f'<td class="muted">{_html_escape(sample)}</td></tr>')
    body.append('</tbody></table>')

    # Recommendations
    body.append('<h2>Recommendations — Pages to Create First</h2>')
    if not recommendations:
        body.append('<p class="muted">No recommendations generated.</p>')
    else:
        for i, rec in enumerate(recommendations, 1):
            priority_color = {"High": "#DC2626", "Medium": "#D97706",
                              "Low": "#16A34A"}.get(rec["priority"], "#6B7280")
            body.append('<div class="rec-card">')
            body.append(f'<div class="rec-head">'
                        f'<span class="rec-priority" style="background:{priority_color}">'
                        f'{rec["priority"]}</span>'
                        f'<span class="rec-action">{rec["action"]}</span>'
                        f'<span class="rec-page">{_html_escape(rec["page_type"])}</span>'
                        f'<span class="rec-num">#{i}</span></div>')
            body.append(f'<div class="rec-title">{_html_escape(rec["keyword"])} '
                        f'<span class="muted">— vol {_fmt(rec["volume"])} / '
                        f'diff {rec["difficulty"]}</span></div>')
            body.append(f'<div class="rec-detail">{_html_escape(rec["detail"])}</div>')
            body.append('</div>')

    # Assemble full HTML
    html = HTML_TEMPLATE.format(
        client=_html_escape(client_domain),
        date_generated=date_gen,
        body="\n".join(body),
    )

    html_path = os.path.join(output_path, f"content_gap_{slug}.html")
    with open(html_path, "w") as f:
        f.write(html)

    # Also save JSON data
    json_path = os.path.join(output_path, f"content_gap_{slug}.json")
    json_data = {
        "client_domain": client_domain,
        "competitors": [_clean_domain(c) for c in competitors],
        "generated": date_gen,
        "summary": {
            "total_gap_keywords": total_gap_kw,
            "total_gap_etv": total_gap_etv,
            "total_gap_volume": total_gap_volume,
            "top_competitor": top_competitor,
            "top_competitor_gap_count": top_competitor_count,
        },
        "gap_by_competitor": per_comp_rows,
        "intent_distribution": {i: {"count": intent_counts.get(i, 0),
                                    "volume": intent_volume.get(i, 0)}
                                for i in ("informational", "commercial",
                                          "transactional")},
        "page_type_distribution": dict(page_type_counts),
        "competitor_page_analysis": comp_page,
        "content_clusters": [
            {"name": c["name"], "count": c["count"],
             "total_volume": c["total_volume"],
             "avg_difficulty": c["avg_difficulty"],
             "keywords": [k.get("keyword", "") for k in c["keywords"]]}
            for c in clusters
        ],
        "top_opportunities": [
            {"keyword": k.get("keyword", ""),
             "volume": k.get("volume", 0),
             "difficulty": k.get("difficulty", 0),
             "competitor_domain": k.get("competitor_domain", ""),
             "competitor_url": k.get("competitor_url", ""),
             "competitor_position": k.get("competitor_position", 0),
             "page_type": k.get("page_type", "other"),
             "intent": k.get("intent", "informational"),
             "priority_score": k.get("priority_score", 0),
             "matching_client_url": k.get("matching_client_url"),
             "match_type": k.get("match_type", "none")}
            for k in top_n
        ],
        "recommendations": recommendations,
    }
    with open(json_path, "w") as f:
        json.dump(json_data, f, indent=2, default=str)

    print(f"\nReport generated: {html_path}")
    print(f"JSON data: {json_path}")
    return html_path


# =====================================================================
# HTML TEMPLATE (white SaaS theme)
# =====================================================================
HTML_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="robots" content="noindex,nofollow">
<title>Competitor Content Gap — {client}</title>
<style>
  :root {{ --primary:#1a1a1a; --accent:#0066cc; --success:#16A34A;
          --warn:#D97706; --danger:#DC2626;
          --bg:#F3F4F6; --muted:#6B7280; --grid:#E5E7EB; --soft:#EFF6FF; }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
         margin:0; color:var(--primary); background:#fff;
         line-height:1.5; font-size:16px; }}
  .wrap {{ max-width:1200px; margin:0 auto; padding:32px; }}
  header {{ border-bottom:3px solid var(--accent); padding-bottom:16px; margin-bottom:24px; }}
  h1 {{ margin:0; font-size:24px; }}
  .meta {{ color:var(--muted); font-size:13px; margin-top:4px; }}
  h2 {{ color:var(--accent); border-bottom:1px solid var(--grid);
        padding-bottom:6px; margin-top:36px; font-size:20px; }}
  .kpis {{ display:flex; gap:12px; flex-wrap:wrap; margin:16px 0; }}
  .kpi {{ background:var(--bg); border-top:3px solid var(--accent);
          border-radius:6px; padding:14px 18px; min-width:200px; flex:1; }}
  .kpi .label {{ font-size:11px; text-transform:uppercase;
                color:var(--muted); letter-spacing:.04em; }}
  .kpi .value {{ font-size:22px; font-weight:700; margin-top:4px; word-break:break-word; }}
  .kpi .sub {{ font-size:11px; color:var(--muted); margin-top:2px; }}
  .intent-row {{ display:flex; gap:12px; flex-wrap:wrap; margin:16px 0; }}
  .intent-card {{ background:var(--bg); border-radius:6px; padding:12px 16px;
                  min-width:180px; flex:1; }}
  .intent-card .label {{ font-size:12px; text-transform:uppercase;
                         color:var(--muted); }}
  .intent-card .value {{ font-size:20px; font-weight:700; margin-top:2px; }}
  .intent-card .sub {{ font-size:11px; color:var(--muted); margin-top:2px; }}
  table {{ width:100%; border-collapse:collapse; margin:12px 0; font-size:14px; }}
  th {{ background:var(--primary); color:#fff; text-align:left;
        padding:8px 10px; font-weight:600; font-size:13px; }}
  td {{ padding:8px 10px; border-bottom:1px solid var(--grid);
        vertical-align:top; }}
  tr:nth-child(even) td {{ background:var(--bg); }}
  .dot {{ display:inline-block; width:10px; height:10px; border-radius:50%;
          margin-right:6px; vertical-align:middle; }}
  .muted {{ color:var(--muted); font-size:13px; }}
  a {{ color:var(--accent); text-decoration:none; }}
  a:hover {{ text-decoration:underline; }}

  /* Expandable rows */
  table.expandable .row-detail {{ display:none; margin-top:8px;
                                  padding:8px 10px; background:#fff;
                                  border:1px solid var(--grid);
                                  border-radius:4px; font-size:12px;
                                  color:var(--muted); }}
  table.expandable .row-detail div {{ margin:2px 0; }}
  table.expandable tr.gap-row td:first-child {{ cursor:pointer; }}
  table.expandable tr.gap-row.open .row-detail {{ display:block; }}
  .match {{ font-size:11px; margin-left:4px; }}
  .match.exact {{ color:var(--danger); }}
  .match.related {{ color:var(--warn); }}

  /* Recommendation cards */
  .rec-card {{ border:1px solid var(--grid); border-radius:6px;
               margin:12px 0; overflow:hidden; }}
  .rec-head {{ background:var(--bg); padding:8px 14px;
               display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
  .rec-priority {{ color:#fff; font-size:11px; font-weight:600;
                   padding:2px 8px; border-radius:10px; }}
  .rec-action {{ font-size:12px; font-weight:600; color:var(--accent); }}
  .rec-page {{ font-size:12px; color:var(--muted); }}
  .rec-num {{ margin-left:auto; font-size:12px; color:var(--muted); }}
  .rec-title {{ padding:10px 14px 4px; font-weight:600; font-size:15px; }}
  .rec-detail {{ padding:4px 14px 12px; font-size:13px;
                 color:var(--primary); line-height:1.5; }}

  footer {{ margin-top:40px; padding-top:16px; border-top:1px solid var(--grid);
           color:var(--muted); font-size:11px; text-align:center; }}
  script {{ display:none; }}
</style>
<script>
document.addEventListener('DOMContentLoaded', function() {{
  document.querySelectorAll('table.expandable tr.gap-row').forEach(function(row) {{
    row.querySelector('td:first-child').addEventListener('click', function() {{
      row.classList.toggle('open');
    }});
  }});
}});
</script>
</head><body><div class="wrap">
{body}
<footer>Generated {date_generated} • DataForSEO data • Competitor Content Gap — noindex/nofollow</footer>
</div></body></html>"""


# =====================================================================
# PIPELINE
# =====================================================================
def run(client_domain, competitors, output_path,
        location_code=DEFAULT_LOCATION_CODE):
    """Full pipeline: discover → classify → match → report."""
    client_domain = _clean_domain(client_domain)
    competitors = [_clean_domain(c) for c in competitors]

    print(f"\n{'='*60}")
    print(f"  COMPETITOR CONTENT GAP ANALYSIS")
    print(f"  Client:      {client_domain}")
    print(f"  Competitors:  {', '.join(competitors)}")
    print(f"{'='*60}\n")

    # --- Step 1: discover gap keywords per competitor ---
    print("[1/4] Discovering competitor gap keywords ...")
    gaps = {}
    for comp in competitors:
        print(f"\n  Competitor: {comp}")
        kws = discover_competitor_keywords(client_domain, comp, location_code)
        gaps[comp] = kws

    # --- Step 2: classify opportunities ---
    print(f"\n[2/4] Classifying content opportunities ...")
    for comp in gaps:
        gaps[comp] = classify_content_opportunities(gaps[comp], client_domain)

    # --- Step 3: identify matching client pages ---
    print(f"\n[3/4] Identifying matching client pages ...")
    # Fetch client keywords once, then match across all competitors
    all_kws = [k for comp in gaps for k in gaps[comp]]
    identify_matching_client_pages(all_kws, client_domain, location_code)
    # Re-stamp back into per-competitor dicts (they share references)
    # all_kws items are the same objects as gaps[comp] items, so already updated.

    # --- Step 4: generate report ---
    print(f"\n[4/4] Generating report ...")
    html_path = generate_content_gap_report(client_domain, competitors,
                                             gaps, output_path)
    return html_path


# =====================================================================
# CLI
# =====================================================================
def main():
    ap = argparse.ArgumentParser(
        description="Find pages competitors rank for that the client doesn't, "
                    "and plan content creation.")
    ap.add_argument("--client", required=True,
                    help="Client domain (e.g. softprowatersystems.com)")
    ap.add_argument("--competitors", required=True,
                    help="Comma-separated competitor domains "
                         "(e.g. aquasana.com,culligan.com)")
    ap.add_argument("--output", default="/tmp/test_content_gap",
                    help="Output directory for the report")
    ap.add_argument("--location", type=int, default=DEFAULT_LOCATION_CODE,
                    help=f"DataForSEO location code (default {DEFAULT_LOCATION_CODE})")
    args = ap.parse_args()

    competitors = [c.strip() for c in args.competitors.split(",") if c.strip()]
    if not competitors:
        print("ERROR: at least one competitor required")
        sys.exit(1)

    html_path = run(args.client, competitors, args.output,
                    location_code=args.location)
    if html_path:
        print(f"\n✓ Done — Report: {html_path}")
    else:
        print("\n✗ Report generation failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
