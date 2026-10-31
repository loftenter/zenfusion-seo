#!/usr/bin/env python3
"""
Domain History Audit
=====================
Standalone Python script that fetches domain-level SEO history and visibility
trends from DataForSEO and renders a white SaaS themed HTML report.

Data sources (6 endpoints):
  1. POST /v3/dataforseo_labs/google/domain_rank_overview/live — current visibility metrics
  2. POST /v3/backlinks/timeseries_summary/live — backlink history (lost/gained over time)
  3. POST /v3/backlinks/summary/live — current backlink profile
  4. POST /v3/dataforseo_labs/google/competitors_domain/live — discover top SERP competitors
  5. POST /v3/serp/google/organic/live/advanced — current SERP features for brand keywords
  6. POST /v3/dataforseo_labs/google/keyword_overview/live — search volume for brand keywords

Public API:
  fetch_domain_history(domain)
  fetch_competitor_list(domain)
  calculate_visibility_trend(domain)
  generate_history_report(domain, data, output_path)

Usage:
  python3 domain_history_audit.py --domain softprowatersystems.com \\
      --output /tmp/test_domain_history

Environment:
  DATAFORSEO_LOGIN    - API login email  (from ~/.hermes/.env)
  DATAFORSEO_PASSWORD - API password     (from ~/.hermes/.env)
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
import sys
import json
import base64
import math
import argparse
import time
import re
import urllib.request
import urllib.error
from datetime import datetime, timedelta

# =====================================================================
# CONFIG
# =====================================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOCATION_CODE = 2840  # United States
DEFAULT_LANGUAGE_CODE = 'en'
API_BASE = 'https://api.dataforseo.com/v3'
API_TIMEOUT = 90
RATE_LIMIT_DELAY = 0.5  # seconds between API calls

# Visibility Index weights (per DataForSEO skill convention)
VI_WEIGHTS = {
    'pos_1': 1.00,
    'pos_2_3': 0.85,
    'pos_4_10': 0.50,
    'pos_11_20': 0.20,
    'pos_21_30': 0.05,
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
                        login = line.split('=', 1)[1].strip()
                    elif line.startswith('DATAFORSEO_PASSWORD='):
                        password = line.split('=', 1)[1].strip()
    if not login or not password:
        print("ERROR: DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD not found")
        print("Set them in ~/.hermes/.env or as environment variables")
        sys.exit(1)
    return login, password


# =====================================================================
# LOW-LEVEL API CALL
# =====================================================================
def dfs_api_call(endpoint, payload=None, method='POST'):
    """
    Make a DataForSEO API call.
    Payload should be an array (list of task objects) per DataForSEO convention.
    """
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
            data = _fix_goto(json.loads(resp.read()), endpoint)
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


def _extract_items(data):
    """Extract items list from a DataForSEO response."""
    result = _extract_first_result(data)
    if not result:
        return []
    items = result.get('items', [])
    return items if isinstance(items, list) else []


def _rate_limit():
    """Pause between API calls to respect rate limits."""
    time.sleep(RATE_LIMIT_DELAY)


# =====================================================================
# HELPERS
# =====================================================================
def _calc_avg_position(organic):
    """Weighted average position from DataForSEO position buckets."""
    midpoints = [
        ('pos_1', 1), ('pos_2_3', 2.5), ('pos_4_10', 7),
        ('pos_11_20', 15.5), ('pos_21_30', 25.5),
        ('pos_31_40', 35.5), ('pos_41_50', 45.5),
        ('pos_51_60', 55.5), ('pos_61_70', 65.5),
        ('pos_71_80', 75.5), ('pos_81_90', 85.5), ('pos_91_100', 95.5),
    ]
    total_kw = sum(organic.get(k, 0) for k, _ in midpoints)
    if total_kw == 0:
        return 0.0
    weighted = sum(organic.get(k, 0) * mp for k, mp in midpoints)
    return round(weighted / total_kw, 1)


def _calc_vi(overview):
    """Calculate Visibility Index from position buckets."""
    kw = overview.get('keywords_count', 0)
    if kw == 0:
        return 0.0
    vi = 0
    for key, weight in VI_WEIGHTS.items():
        vi += overview.get(key, 0) * weight
    return round(vi / max(kw, 1) * 100, 1)


def _dr_from_rank(rank):
    """Convert DataForSEO raw rank (0-1000) to a 0-100 domain rating."""
    if not rank:
        return 0.0
    return round(math.sin(rank / 636.62) * 100, 1)


def brand_name_from_domain(domain):
    """
    Extract a brand name from a compound domain.
    softprowatersystems.com -> softpro
    thrivegarden.com -> thrive
    """
    root = domain.split('.')[0].lower()
    if 'water' in root:
        prefix = root.split('water')[0]
        return prefix if prefix else root
    parts = re.findall(r'[a-z]+', root)
    if len(parts) > 1:
        return parts[0]
    return root


def _clean_domain(domain):
    """Strip protocol, www, and trailing slash."""
    return (domain.replace('https://', '').replace('http://', '')
            .replace('www.', '').rstrip('/'))


# =====================================================================
# ENDPOINT WRAPPERS
# =====================================================================
def get_domain_overview(domain, location_code=DEFAULT_LOCATION_CODE,
                        language_code=DEFAULT_LANGUAGE_CODE):
    """
    Endpoint 1: POST /v3/dataforseo_labs/google/domain_rank_overview/live
    Returns current visibility metrics: etv, keyword count, position buckets,
    avg position, visibility index.
    """
    print(f"  -> Fetching domain overview for {domain}...")
    payload = [{
        'target': domain,
        'location_code': location_code,
        'language_code': language_code,
    }]
    data = dfs_api_call('dataforseo_labs/google/domain_rank_overview/live',
                        payload, 'POST')
    result = _extract_first_result(data)
    if not result:
        print("  x No domain overview data returned")
        return None
    items = result.get('items', [])
    if not items:
        print("  x No items in domain overview response")
        return None
    item = items[0] if isinstance(items, list) else items
    metrics = item.get('metrics', {})
    organic = metrics.get('organic', {})

    pos_31_100 = 0
    for i in range(31, 101, 10):
        pos_31_100 += organic.get(f'pos_{i}_{i+9}', 0)

    overview = {
        'etv': round(organic.get('etv', 0), 2),
        'keywords_count': organic.get('count', 0),
        'pos_1': organic.get('pos_1', 0),
        'pos_2_3': organic.get('pos_2_3', 0),
        'pos_4_10': organic.get('pos_4_10', 0),
        'pos_11_20': organic.get('pos_11_20', 0),
        'pos_21_30': organic.get('pos_21_30', 0),
        'pos_31_100': pos_31_100,
        'avg_position': _calc_avg_position(organic),
    }
    overview['vi'] = _calc_vi(overview)
    # First seen date — approximate domain age proxy if available
    overview['first_seen'] = result.get('first_seen', '') or item.get('first_seen', '')
    print(f"  v ETV: {overview['etv']:,} | Keywords: "
          f"{overview['keywords_count']} | VI: {overview['vi']}")
    return overview


def get_backlink_timeseries(domain, date_from=None, date_to=None,
                            group_range='month'):
    """
    Endpoint 2: POST /v3/backlinks/timeseries_summary/live
    Returns backlink history (referring domains, backlinks, new/lost over time).
    """
    print(f"  -> Fetching backlink timeseries for {domain}...")
    date_to = date_to or datetime.now().strftime('%Y-%m-%d')
    if not date_from:
        date_from = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
    payload = [{
        'target': domain,
        'date_from': date_from,
        'date_to': date_to,
        'group_range': group_range,
    }]
    data = dfs_api_call('backlinks/timeseries_summary/live',
                        payload, 'POST')
    items = _extract_items(data)
    if not items:
        print("  x No backlink timeseries data returned")
        return None
    series = []
    for it in items:
        series.append({
            'date': it.get('date', '') or it.get('datetime_from', ''),
            'referring_domains': it.get('referring_domains', 0) or 0,
            'backlinks': it.get('backlinks', 0) or 0,
            'new_backlinks': it.get('new_backlinks', 0) or 0,
            'lost_backlinks': it.get('lost_backlinks', 0) or 0,
            'new_referring_domains': it.get('new_referring_domains', 0) or 0,
            'lost_referring_domains': it.get('lost_referring_domains', 0) or 0,
        })
    first = series[0] if series else {}
    last = series[-1] if series else {}
    baseline_rd = first.get('referring_domains', 0)
    final_rd = last.get('referring_domains', 0)
    rd_delta = final_rd - baseline_rd
    rd_delta_pct = (rd_delta / baseline_rd * 100) if baseline_rd > 0 else 0
    total_new = sum(s.get('new_backlinks', 0) for s in series)
    total_lost = sum(s.get('lost_backlinks', 0) for s in series)
    print(f"  v {len(series)} data points | RD: {baseline_rd} -> {final_rd} "
          f"({rd_delta:+d}) | New: {total_new:,} Lost: {total_lost:,}")
    return {
        'series': series,
        'baseline_referring_domains': baseline_rd,
        'final_referring_domains': final_rd,
        'rd_delta': rd_delta,
        'rd_delta_pct': round(rd_delta_pct, 1),
        'total_new_backlinks': total_new,
        'total_lost_backlinks': total_lost,
    }


def get_backlink_summary(domain):
    """
    Endpoint 3: POST /v3/backlinks/summary/live
    Returns current backlink profile: DR, total backlinks, referring domains,
    broken links, etc.
    """
    print(f"  -> Fetching backlink summary for {domain}...")
    payload = [{
        'target': domain,
        'include_subdomains': True,
        'internal_list_limit': 1,
        'backlinks_status_type': 'all',
    }]
    data = dfs_api_call('backlinks/summary/live', payload, 'POST')
    result = _extract_first_result(data)
    if not result:
        print("  x No backlink summary data returned")
        return None
    rank = result.get('rank', 0)
    summary = {
        'dr': _dr_from_rank(rank),
        'rank_raw': rank,
        'backlinks': result.get('backlinks', 0) or 0,
        'referring_domains': result.get('referring_domains', 0) or 0,
        'referring_main_domains': result.get('referring_main_domains', 0) or 0,
        'referring_ips': result.get('referring_ips', 0) or 0,
        'referring_subnets': result.get('referring_subnets', 0) or 0,
        'backlinks_dofollow': (result.get('backlinks', 0) or 0) -
                              (result.get('referring_pages_nofollow', 0) or 0),
        'backlinks_nofollow': result.get('referring_pages_nofollow', 0) or 0,
        'broken_backlinks': result.get('broken_backlinks', 0) or 0,
        'broken_pages': result.get('broken_pages', 0) or 0,
        'first_seen': result.get('first_seen', '') or '',
    }
    print(f"  v DR: {summary['dr']} | Backlinks: {summary['backlinks']:,} "
          f"| Ref domains: {summary['referring_domains']:,}")
    return summary


def get_competitors_raw(domain, limit=10,
                        location_code=DEFAULT_LOCATION_CODE,
                        language_code=DEFAULT_LANGUAGE_CODE):
    """
    Endpoint 4: /v3/dataforseo_labs/google/competitors_domain/live
    Returns top SERP competitors with visibility, ETV, relevance scores.
    Note: the task spec lists this as 'competitors/live' but the correct
    DataForSEO Labs endpoint name is 'competitors_domain/live' (confirmed
    via the official docs). It is a POST endpoint.
    """
    print(f"  -> Fetching competitors for {domain}...")
    payload = [{
        'target': domain,
        'location_code': location_code,
        'language_code': language_code,
        'limit': limit,
    }]
    data = dfs_api_call('dataforseo_labs/google/competitors_domain/live',
                        payload, 'POST')
    result = _extract_first_result(data)
    if not result:
        print("  x No competitor data returned")
        return []
    items = result.get('items', [])
    if not isinstance(items, list):
        items = []
    print(f"  v {len(items[:limit])} competitors found")
    return items[:limit]


def get_serp_features(keyword, domain, location_code=DEFAULT_LOCATION_CODE,
                      language_code=DEFAULT_LANGUAGE_CODE):
    """
    Endpoint 5: POST /v3/serp/google/organic/live/advanced
    Returns SERP features (AI Overview, PAA, featured snippets) for a
    brand keyword, plus client position.
    """
    print(f"  -> Fetching SERP features for '{keyword}'...")
    payload = [{
        'keyword': keyword,
        'location_code': location_code,
        'language_code': language_code,
        'device': 'desktop',
        'os': 'windows',
        'depth': 30,
        'use_improved_etv': True,
    }]
    data = dfs_api_call('serp/google/organic/live/advanced', payload, 'POST')
    result = _extract_first_result(data)
    if not result:
        print(f"  x No SERP data for '{keyword}'")
        return None
    items = result.get('items', [])

    feature_type_map = {
        'featured_snippet', 'people_also_ask', 'people_also_search',
        'local_pack', 'video', 'image', 'images', 'news',
        'shopping', 'top_stories', 'twitter', 'questions',
        'ai_overview', 'knowledge_graph', 'map', 'carousel',
        'recipe', 'jobs', 'flights', 'hotels', 'pack',
    }

    features = []
    organic_results = []
    client_position = None
    client_url = None
    domain_clean = domain.replace('www.', '')

    for item in items:
        item_type = item.get('type', 'unknown')
        rank_absolute = item.get('rank_absolute', 0)
        if item_type == 'organic':
            entry = {
                'position': rank_absolute,
                'domain': item.get('domain', ''),
                'url': item.get('url', ''),
                'title': (item.get('title', '') or '')[:100],
            }
            organic_results.append(entry)
            if domain_clean in (item.get('domain', '') or '').replace('www.', ''):
                if client_position is None:
                    client_position = rank_absolute
                    client_url = item.get('url', '')
        elif item_type in feature_type_map:
            features.append({
                'type': item_type,
                'position': rank_absolute,
                'domain': item.get('domain', ''),
                'title': (item.get('title', '') or '')[:80],
            })

    return {
        'keyword': keyword,
        'features': features,
        'feature_types': sorted(set(f['type'] for f in features)),
        'organic_results': organic_results[:10],
        'client_position': client_position,
        'client_url': client_url,
        'result_count': result.get('result_count', 0),
    }


def get_keyword_info(keyword, location_code=DEFAULT_LOCATION_CODE,
                     language_code=DEFAULT_LANGUAGE_CODE):
    """
    Endpoint 6: POST /v3/dataforseo_labs/google/keyword_overview/live
    Returns search volume, CPC, competition for a brand keyword.
    Note: the task spec lists this as 'keyword_info/live' but the correct
    DataForSEO Labs endpoint name is 'keyword_overview/live' (confirmed via
    the official docs). It takes a 'keywords' array (up to 700 keywords).
    """
    print(f"  -> Fetching keyword info for '{keyword}'...")
    payload = [{
        'keywords': [keyword],
        'location_code': location_code,
        'language_code': language_code,
    }]
    data = dfs_api_call('dataforseo_labs/google/keyword_overview/live',
                        payload, 'POST')
    result = _extract_first_result(data)
    if not result:
        print(f"  x No keyword info for '{keyword}'")
        return None
    # keyword_overview returns items[] with per-keyword data
    items = result.get('items', [])
    item = None
    if items and isinstance(items, list):
        for it in items:
            if (it.get('keyword', '') or '').lower() == keyword.lower():
                item = it
                break
        if not item:
            item = items[0]
    if not item:
        print(f"  x No keyword info item for '{keyword}'")
        return None
    ki = item.get('keyword_info', {}) or {}
    info = {
        'keyword': item.get('keyword', keyword),
        'search_volume': ki.get('search_volume', 0) or 0,
        'cpc': ki.get('cpc', 0) or 0,
        'competition': ki.get('competition', None),
        'low_top_of_page_bid': ki.get('low_top_of_page_bid', 0) or 0,
        'high_top_of_page_bid': ki.get('high_top_of_page_bid', 0) or 0,
        'monthly_searches': ki.get('monthly_searches', []) or [],
    }
    print(f"  v Volume: {info['search_volume']} | CPC: {info['cpc']} | "
          f"Competition: {info['competition']}")
    return info


# =====================================================================
# PUBLIC API — required functions
# =====================================================================
def fetch_competitor_list(domain):
    """
    Discover top 10 SERP competitors for a domain.

    Returns a list of dicts with: domain, avg_position, visibility,
    se_keywords (keyword count), etv, relevance.
    """
    items = get_competitors_raw(domain, limit=10)
    competitors = []
    for item in items:
        comp_domain = item.get('domain', '')
        # competitors_domain/live returns full_domain_metrics + metrics
        full_metrics = item.get('full_domain_metrics', {}) or {}
        full_organic = full_metrics.get('organic', {}) or {}
        # intersecting metrics (shared with target)
        inter_metrics = item.get('metrics', {}) or {}
        inter_organic = inter_metrics.get('organic', {}) or {}
        competitors.append({
            'domain': comp_domain,
            'avg_position': round(item.get('avg_position', 0) or 0, 1),
            'visibility': round(item.get('visibility', 0) or 0, 2),
            'se_keywords': full_organic.get('count', 0) or 0,
            'etv': round(full_organic.get('etv', 0) or 0, 2),
            'relevance': round(item.get('relevance', 0) or inter_organic.get('count', 0) or 0, 2),
            'intersections': inter_organic.get('count', 0) or 0,
            'full': item,
        })
    return competitors


def calculate_visibility_trend(domain):
    """
    From backlink timeseries data, show visibility over time.

    Uses the backlinks/timeseries_summary endpoint to derive a referring
    domains + backlinks trend, which is the closest historical signal
    DataForSEO provides at domain level.

    Returns dict with: series (list of {date, referring_domains,
    backlinks, new_backlinks, lost_backlinks}), baseline, final, deltas.
    """
    return get_backlink_timeseries(domain)


def fetch_domain_history(domain):
    """
    Call all 6 DataForSEO endpoints and aggregate results into a single
    dict suitable for report generation.

    Returns: dict with keys domain, generated_at, overview, timeseries,
    backlinks, competitors, serp_features, brand_keyword_info.
    """
    domain = _clean_domain(domain)
    print(f"\n{'='*60}")
    print(f"  Domain History Audit for {domain}")
    print(f"{'='*60}\n")

    data = {
        'domain': domain,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'brand_name': brand_name_from_domain(domain),
    }

    # 1. Domain Overview (current visibility metrics)
    print("[1/6] Domain Overview")
    data['overview'] = get_domain_overview(domain)
    _rate_limit()

    # 2. Backlink Timeseries (history)
    print("\n[2/6] Backlink History (Timeseries)")
    data['timeseries'] = calculate_visibility_trend(domain)
    _rate_limit()

    # 3. Backlink Summary (current profile)
    print("\n[3/6] Backlink Summary")
    data['backlinks'] = get_backlink_summary(domain)
    _rate_limit()

    # 4. Competitors (top 10 SERP competitors)
    print("\n[4/6] Competitor Discovery")
    data['competitors'] = fetch_competitor_list(domain)
    _rate_limit()

    # 5. SERP features for brand keyword
    print("\n[5/6] SERP Features (brand keyword)")
    brand_kw = data['brand_name']
    data['serp_features'] = get_serp_features(brand_kw, domain)
    _rate_limit()

    # 6. Brand keyword volume
    print("\n[6/6] Brand Keyword Volume")
    data['brand_keyword_info'] = get_keyword_info(brand_kw)

    print(f"\n{'='*60}")
    print(f"  Data collection complete for {domain}")
    print(f"{'='*60}\n")
    return data


# =====================================================================
# HTML REPORT GENERATION
# =====================================================================
def _svg_line_chart(series, value_key, label, color='#0066cc',
                    width=720, height=180):
    """Build a simple SVG line chart from a series of {date, value} dicts."""
    if not series:
        return '<p style="color:#999;font-size:14px;">No timeseries data available.</p>'
    points = []
    for s in series:
        val = s.get(value_key, 0)
        date = s.get('date', '')
        if date:
            try:
                dt = datetime.strptime(date[:10], '%Y-%m-%d')
                points.append((dt, val))
            except (ValueError, TypeError):
                continue
    if len(points) < 2:
        return '<p style="color:#999;font-size:14px;">Insufficient timeseries data for chart.</p>'
    points.sort(key=lambda p: p[0])
    values = [p[1] for p in points]
    max_v = max(values) if values else 1
    min_v = min(values) if values else 0
    span = max(max_v - min_v, 1)
    pad_l, pad_r, pad_t, pad_b = 50, 20, 20, 30
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    n = len(points)
    coords = []
    for i, (dt, val) in enumerate(points):
        x = pad_l + (i / max(n - 1, 1)) * plot_w
        y = pad_t + plot_h - ((val - min_v) / span) * plot_h
        coords.append((x, y, dt, val))
    # Polyline
    poly = ' '.join(f'{x:.1f},{y:.1f}' for x, y, _, _ in coords)
    # Area fill
    area = (f'{coords[0][0]:.1f},{pad_t + plot_h:.1f} '
            + ' '.join(f'{x:.1f},{y:.1f}' for x, y, _, _ in coords)
            + f' {coords[-1][0]:.1f},{pad_t + plot_h:.1f}')
    # X labels (first, middle, last)
    label_indices = [0, n // 2, n - 1]
    x_labels = ''
    for idx in label_indices:
        x, _, dt, _ = coords[idx]
        lbl = dt.strftime('%b %Y')
        x_labels += (f'<text x="{x:.1f}" y="{height - 8}" '
                     f'font-size="11" fill="#888" text-anchor="middle">'
                     f'{lbl}</text>')
    # Y labels (min, max)
    y_labels = (f'<text x="{pad_l - 6}" y="{pad_t + 4}" font-size="11" '
                f'fill="#888" text-anchor="end">{max_v:,}</text>'
                f'<text x="{pad_l - 6}" y="{pad_t + plot_h}" font-size="11" '
                f'fill="#888" text-anchor="end">{min_v:,}</text>')
    svg = f'''<svg viewBox="0 0 {width} {height}" style="width:100%;max-width:{width}px;height:auto;">
  <polygon points="{area}" fill="{color}" fill-opacity="0.08"/>
  <polyline points="{poly}" fill="none" stroke="{color}" stroke-width="2.5"/>
  {''.join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}"/>' for x, y, _, _ in coords)}
  {y_labels}
  {x_labels}
  <text x="{pad_l}" y="14" font-size="12" fill="#666">{label}</text>
</svg>'''
    return svg


def _svg_bar_chart(series, value_key, label, color='#0066cc',
                   width=720, height=180):
    """Build a simple SVG bar chart (e.g., new vs lost backlinks per period)."""
    if not series:
        return '<p style="color:#999;font-size:14px;">No timeseries data available.</p>'
    points = []
    for s in series:
        val = s.get(value_key, 0)
        date = s.get('date', '')
        if date:
            try:
                dt = datetime.strptime(date[:10], '%Y-%m-%d')
                points.append((dt, val))
            except (ValueError, TypeError):
                continue
    if not points:
        return '<p style="color:#999;font-size:14px;">No chart data.</p>'
    points.sort(key=lambda p: p[0])
    max_v = max(p[1] for p in points) or 1
    pad_l, pad_r, pad_t, pad_b = 50, 20, 20, 30
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    n = len(points)
    bar_w = max(plot_w / n * 0.6, 4)
    bars = ''
    for i, (dt, val) in enumerate(points):
        x = pad_l + (i / n) * plot_w
        bar_h = (val / max_v) * plot_h if max_v else 0
        y = pad_t + plot_h - bar_h
        bars += (f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
                 f'height="{bar_h:.1f}" fill="{color}" rx="2"/>')
    label_indices = sorted(set([0, n // 2, n - 1]))
    x_labels = ''
    for idx in label_indices:
        if idx < n:
            x = pad_l + (idx / n) * plot_w + bar_w / 2
            lbl = points[idx][0].strftime('%b %Y')
            x_labels += (f'<text x="{x:.1f}" y="{height - 8}" '
                         f'font-size="11" fill="#888" text-anchor="middle">'
                         f'{lbl}</text>')
    y_labels = (f'<text x="{pad_l - 6}" y="{pad_t + 4}" font-size="11" '
                f'fill="#888" text-anchor="end">{max_v:,}</text>'
                f'<text x="{pad_l - 6}" y="{pad_t + plot_h}" font-size="11" '
                f'fill="#888" text-anchor="end">0</text>')
    svg = f'''<svg viewBox="0 0 {width} {height}" style="width:100%;max-width:{width}px;height:auto;">
  {bars}
  {y_labels}
  {x_labels}
  <text x="{pad_l}" y="14" font-size="12" fill="#666">{label}</text>
</svg>'''
    return svg


def _generate_action_items(data):
    """Generate data-grounded action items based on historical patterns."""
    actions = []
    domain = data.get('domain', '')
    overview = data.get('overview') or {}
    timeseries = data.get('timeseries') or {}
    backlinks = data.get('backlinks') or {}
    competitors = data.get('competitors') or []
    serp = data.get('serp_features') or {}
    brand_ki = data.get('brand_keyword_info') or {}

    # 1. Backlink trend (gained vs lost)
    total_new = timeseries.get('total_new_backlinks', 0)
    total_lost = timeseries.get('total_lost_backlinks', 0)
    rd_delta = timeseries.get('rd_delta', 0)
    rd_pct = timeseries.get('rd_delta_pct', 0)
    if total_lost > total_new:
        actions.append({
            'title': f'Backlink Attrition: {total_lost:,} lost vs {total_new:,} gained',
            'why': (f'Over the analysis period, {domain} lost more backlinks '
                    f'than it gained (net referring domains: {rd_delta:+d}, '
                    f'{rd_pct:+.1f}%). Recover lost links via outreach and '
                    f'strengthen the link acquisition pipeline.'),
            'effort': 'High',
            'impact': 'High',
        })
    elif total_new > 0 and rd_delta > 0:
        actions.append({
            'title': f'Sustain Backlink Growth ({rd_delta:+d} referring domains)',
            'why': (f'Positive backlink trend: +{rd_pct:.1f}% referring domains. '
                    f'Continue current link building momentum and monitor for '
                    f'future attrition.'),
            'effort': 'Low',
            'impact': 'Medium',
        })

    # 2. Domain Rating / authority
    dr = backlinks.get('dr', 0)
    ref_domains = backlinks.get('referring_domains', 0)
    if dr < 30 or ref_domains < 500:
        actions.append({
            'title': f'Build Domain Authority (DR {dr}, {ref_domains:,} ref domains)',
            'why': (f'Domain Rating of {dr} with {ref_domains:,} referring domains '
                    f'indicates significant room for authority growth. Prioritize '
                    f'high-quality dofollow links from authoritative niche sites.'),
            'effort': 'High',
            'impact': 'High',
        })

    # 3. Page 1 coverage
    page1 = (overview.get('pos_1', 0) + overview.get('pos_2_3', 0) +
             overview.get('pos_4_10', 0))
    total_kw = overview.get('keywords_count', 0)
    if total_kw > 0:
        page1_pct = (page1 / total_kw) * 100
        if page1_pct < 30:
            actions.append({
                'title': f'Improve Page 1 Coverage ({page1:,} of {total_kw:,} keywords)',
                'why': (f'Only {page1_pct:.1f}% of ranked keywords sit on page 1. '
                        f'Lift keywords from positions 11-20 into the top 10 to '
                        f'capture significantly more organic traffic.'),
                'effort': 'High',
                'impact': 'High',
            })

    # 4. Competitor gap
    if competitors:
        top_comp = competitors[0]
        comp_vis = top_comp.get('visibility', 0)
        our_vis = overview.get('vi', 0)
        if comp_vis > our_vis:
            actions.append({
                'title': f'Close Visibility Gap with {top_comp["domain"]}',
                'why': (f'Top competitor {top_comp["domain"]} has a visibility '
                        f'score of {comp_vis} vs your VI of {our_vis}. Analyze '
                        f'their top-ranking pages and target overlapping keyword '
                        f'opportunities.'),
                'effort': 'Medium',
                'impact': 'High',
            })

    # 5. SERP feature opportunities
    feature_types = serp.get('feature_types', []) if serp else []
    if 'featured_snippet' in feature_types:
        actions.append({
            'title': 'Optimize for Featured Snippets',
            'why': ('Featured snippets appear for your brand keyword. Structure '
                    'content with clear Q&A format and concise 40-60 word '
                    'definitions to win snippet placements.'),
            'effort': 'Low',
            'impact': 'High',
        })
    if 'people_also_ask' in feature_types:
        actions.append({
            'title': 'Target People Also Ask Questions',
            'why': ('PAA boxes appear for your brand keyword. Add an FAQ section '
                    'answering common questions to capture PAA placements.'),
            'effort': 'Low',
            'impact': 'Medium',
        })
    if 'ai_overview' in feature_types:
        actions.append({
            'title': 'Optimize for AI Overview (GEO)',
            'why': ('AI Overview appears for your brand keyword. Ensure content '
                    'is structured for AI extraction with clear factual summaries '
                    'and authoritative data.'),
            'effort': 'Medium',
            'impact': 'High',
        })

    # 6. Brand keyword volume
    vol = brand_ki.get('search_volume', 0) if brand_ki else 0
    if vol == 0:
        actions.append({
            'title': 'Low Brand Search Volume',
            'why': ('Brand keyword search volume is zero or negligible, '
                    'indicating low brand awareness. Invest in brand-building '
                    'campaigns and branded content to grow search demand.'),
            'effort': 'Medium',
            'impact': 'Medium',
        })

    if not actions:
        actions.append({
            'title': 'Continue SEO Monitoring',
            'why': ('No critical issues detected. Continue regular monitoring of '
                    'keyword positions, backlink profile, and SERP feature changes.'),
            'effort': 'Low',
            'impact': 'Low',
        })

    html = ''
    for i, a in enumerate(actions, 1):
        html += f'''    <div class="action-item">
      <h4>{i}. {a['title']}</h4>
      <p>{a['why']}</p>
      <div class="meta">Effort: {a['effort']} &middot; Impact: {a['impact']}</div>
    </div>
'''
    return html


def generate_history_report(domain, data, output_path):
    """
    Generate the white SaaS themed HTML report.

    Sections:
      - Domain overview (age, authority, current visibility)
      - Visibility trend chart
      - Backlink history (gained/lost over time)
      - Competitor discovery table (top 10, expandable rows)
      - SERP feature presence
      - Brand keyword volume
      - Action items
    """
    domain = data.get('domain', domain)
    generated_at = data.get('generated_at', '')
    overview = data.get('overview') or {}
    timeseries = data.get('timeseries') or {}
    backlinks = data.get('backlinks') or {}
    competitors = data.get('competitors') or []
    serp = data.get('serp_features') or {}
    brand_ki = data.get('brand_keyword_info') or {}
    brand_name = data.get('brand_name', '')

    # --- Summary cards ---
    etv = overview.get('etv', 0)
    kw_count = overview.get('keywords_count', 0)
    vi = overview.get('vi', 0)
    avg_pos = overview.get('avg_position', 0)
    dr = backlinks.get('dr', 0)
    ref_domains = backlinks.get('referring_domains', 0)
    total_backlinks = backlinks.get('backlinks', 0)

    # --- Domain age proxy ---
    first_seen = backlinks.get('first_seen', '') or overview.get('first_seen', '')
    domain_age = 'N/A'
    if first_seen:
        try:
            fs_date = datetime.strptime(first_seen[:10], '%Y-%m-%d')
            days = (datetime.now() - fs_date).days
            if days > 365:
                domain_age = f'{days // 365} year(s)'
            else:
                domain_age = f'{days} days'
        except (ValueError, TypeError):
            domain_age = first_seen[:10]

    # --- Visibility trend chart ---
    ts_series = timeseries.get('series', []) if timeseries else []
    vis_chart = _svg_line_chart(ts_series, 'referring_domains',
                                'Referring Domains Over Time',
                                color='#0066cc')

    # --- Backlink history charts ---
    new_chart = _svg_bar_chart(ts_series, 'new_backlinks',
                               'New Backlinks per Period',
                               color='#27ae60')
    lost_chart = _svg_bar_chart(ts_series, 'lost_backlinks',
                                'Lost Backlinks per Period',
                                color='#cc0000')

    # --- Competitor table (expandable rows) ---
    comp_rows = ''
    for i, comp in enumerate(competitors):
        comp_domain = comp.get('domain', '')
        comp_avg = comp.get('avg_position', 0)
        comp_vis = comp.get('visibility', 0)
        comp_kw = comp.get('se_keywords', 0)
        comp_etv = comp.get('etv', 0)
        comp_rel = comp.get('relevance', 0)
        comp_inter = comp.get('intersections', 0)
        full_json = json.dumps(comp.get('full', {}), indent=2,
                               default=str)
        full_escaped = (full_json.replace('&', '&amp;')
                        .replace('<', '&lt;').replace('>', '&gt;'))
        comp_rows += f'''
      <tr class="comp-row" onclick="toggleComp('comp-detail-{i}')" style="cursor:pointer">
        <td><span class="expand-icon" id="icon-{i}">&#9654;</span> {comp_domain}</td>
        <td>{comp_vis}</td>
        <td>{comp_etv:,.0f}</td>
        <td>{comp_kw:,}</td>
        <td>{comp_avg}</td>
        <td>{comp_rel}</td>
        <td>{comp_inter:,}</td>
      </tr>
      <tr id="comp-detail-{i}" class="comp-detail" style="display:none">
        <td colspan="7">
          <pre style="font-size:12px;background:#f8f9fa;padding:12px;border-radius:6px;overflow-x:auto;max-height:300px;">{full_escaped}</pre>
        </td>
      </tr>'''

    # --- SERP features ---
    feature_types = serp.get('feature_types', []) if serp else []
    feat_badges = ' '.join(
        f'<span class="feat-badge">{f}</span>' for f in feature_types
    ) if feature_types else '<span style="color:#999">No special features detected</span>'
    client_pos = serp.get('client_position') if serp else None
    client_url = serp.get('client_url', '') if serp else ''

    # Organic results table
    organic_rows = ''
    for r in (serp.get('organic_results', []) if serp else []):
        organic_rows += f'''
        <tr>
          <td>{r.get('position', '—')}</td>
          <td>{r.get('domain', '')}</td>
          <td><a href="{r.get('url', '')}" target="_blank" style="color:#0066cc">{(r.get('title', '') or '')[:70]}</a></td>
        </tr>'''

    # --- Brand keyword volume ---
    bk_vol = brand_ki.get('search_volume', 0) if brand_ki else 0
    bk_cpc = brand_ki.get('cpc', 0) if brand_ki else 0
    bk_comp = brand_ki.get('competition') if brand_ki else None
    bk_comp_label = '—'
    if bk_comp is not None:
        bk_comp_label = f'{bk_comp} ({["Low","Medium","High"][int(bk_comp)] if isinstance(bk_comp, (int, float)) and 0 <= bk_comp <= 2 else bk_comp})'
    monthly = brand_ki.get('monthly_searches', []) if brand_ki else []
    monthly_chart = ''
    if monthly:
        ms_series = []
        for m in monthly:
            ms_series.append({
                'date': m.get('year', '') and f"{m['year']}-{m.get('month',1):02d}-01",
                'search_volume': m.get('search_volume', 0),
            })
        monthly_chart = _svg_line_chart(ms_series, 'search_volume',
                                        'Monthly Search Volume',
                                        color='#0066cc')

    # --- Action items ---
    actions_html = _generate_action_items(data)

    # --- Build HTML ---
    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex,nofollow">
<title>Domain History Audit &mdash; {domain}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #ffffff; color: #1a1a1a; font-size: 16px; margin: 0; padding: 20px; max-width: 1200px; margin: 0 auto; line-height: 1.5; }}
  h1 {{ font-size: 24px; color: #1a1a1a; margin-bottom: 4px; }}
  h2 {{ font-size: 20px; color: #1a1a1a; margin-top: 36px; margin-bottom: 14px; border-bottom: 2px solid #0066cc; padding-bottom: 6px; }}
  h3 {{ font-size: 18px; color: #333; margin-top: 20px; margin-bottom: 10px; }}
  a {{ color: #0066cc; }}
  .subtitle {{ font-size: 14px; color: #666; margin-bottom: 20px; }}
  .url-headline {{ font-size: 20px; font-weight: 700; color: #0066cc; margin-bottom: 8px; }}
  .summary-cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 24px; }}
  .summary-card {{ background: #f8f9fa; border: 1px solid #e0e0e0; border-radius: 10px; padding: 16px; text-align: center; }}
  .summary-card .num {{ font-size: 24px; font-weight: 700; color: #0066cc; }}
  .summary-card .label {{ font-size: 12px; color: #666; text-transform: uppercase; margin-top: 4px; }}
  .section {{ background: #f8f9fa; border: 1px solid #e0e0e0; border-radius: 10px; padding: 20px; margin-bottom: 20px; }}
  table {{ width: 100%; border-collapse: collapse; background: #ffffff; }}
  th {{ background: #f0f0f0; color: #1a1a1a; font-weight: 700; font-size: 14px; padding: 10px 12px; text-align: left; border-bottom: 2px solid #e0e0e0; }}
  td {{ padding: 9px 12px; border-bottom: 1px solid #e0e0e0; font-size: 14px; color: #1a1a1a; }}
  tr:hover {{ background: #f8f9fa; }}
  .comp-detail td {{ background: #f0f7ff; }}
  .expand-icon {{ color: #0066cc; font-size: 12px; margin-right: 4px; transition: transform 0.2s; display: inline-block; }}
  .feat-badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: 600; background: #e7f3ff; color: #0066cc; margin: 2px; }}
  .action-item {{ background: #ffffff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 14px; margin-bottom: 12px; }}
  .action-item h4 {{ margin: 0 0 6px 0; font-size: 15px; color: #0066cc; }}
  .action-item p {{ margin: 0; font-size: 14px; color: #555; line-height: 1.5; }}
  .action-item .meta {{ font-size: 12px; color: #999; margin-top: 8px; }}
  .chart-box {{ background: #ffffff; border: 1px solid #e8e8e8; border-radius: 8px; padding: 16px; margin-bottom: 16px; }}
  .footer {{ margin-top: 30px; padding-top: 15px; border-top: 1px solid #e0e0e0; font-size: 13px; color: #999; text-align: center; }}
</style>
<script>
function toggleComp(id) {{
  var row = document.getElementById(id);
  if (row.style.display === 'none') {{
    row.style.display = '';
  }} else {{
    row.style.display = 'none';
  }}
}}
</script>
</head>
<body>
  <h1>Domain History Audit</h1>
  <div class="url-headline"><a href="https://{domain}" target="_blank">{domain}</a></div>
  <div class="subtitle">Generated: {generated_at} &middot; DataForSEO Domain History &amp; Visibility Trends &middot; noindex, nofollow</div>

  <!-- Domain Overview -->
  <h2>Domain Overview</h2>
  <div class="summary-cards">
    <div class="summary-card"><div class="num">{etv:,.0f}</div><div class="label">Est. Monthly Traffic</div></div>
    <div class="summary-card"><div class="num">{kw_count:,}</div><div class="label">Keywords Ranked</div></div>
    <div class="summary-card"><div class="num">{vi}</div><div class="label">Visibility Index</div></div>
    <div class="summary-card"><div class="num">{avg_pos:.1f}</div><div class="label">Avg Position</div></div>
    <div class="summary-card"><div class="num">{dr}</div><div class="label">Domain Rating</div></div>
    <div class="summary-card"><div class="num">{ref_domains:,}</div><div class="label">Referring Domains</div></div>
    <div class="summary-card"><div class="num">{total_backlinks:,}</div><div class="label">Total Backlinks</div></div>
    <div class="summary-card"><div class="num">{domain_age}</div><div class="label">Domain Age (proxy)</div></div>
  </div>

  <!-- Visibility Trend -->
  <h2>Visibility Trend</h2>
  <div class="section">
    <h3>Referring Domains Over Time</h3>
    <div class="chart-box">
      {vis_chart}
    </div>
    <p style="font-size:14px;color:#666;">
      Baseline referring domains: {timeseries.get('baseline_referring_domains', 0):,} &middot;
      Current: {timeseries.get('final_referring_domains', 0):,} &middot;
      Change: {timeseries.get('rd_delta', 0):+d} ({timeseries.get('rd_delta_pct', 0):+.1f}%)
    </p>
  </div>

  <!-- Backlink History -->
  <h2>Backlink History</h2>
  <div class="section">
    <h3>New Backlinks per Period</h3>
    <div class="chart-box">
      {new_chart}
    </div>
    <h3>Lost Backlinks per Period</h3>
    <div class="chart-box">
      {lost_chart}
    </div>
    <p style="font-size:14px;color:#666;">
      Total new backlinks (period): {timeseries.get('total_new_backlinks', 0):,} &middot;
      Total lost backlinks (period): {timeseries.get('total_lost_backlinks', 0):,}
    </p>
  </div>

  <!-- Competitor Discovery -->
  <h2>Competitor Discovery (Top 10 SERP Competitors)</h2>
  <div class="section">
    <p style="font-size:14px;color:#666;margin-bottom:12px;">Click any row to expand full competitor data.</p>
    <table>
      <thead>
        <tr>
          <th>Domain</th>
          <th>Visibility</th>
          <th>ETV</th>
          <th>Keywords</th>
          <th>Avg Position</th>
          <th>Relevance</th>
          <th>Intersections</th>
        </tr>
      </thead>
      <tbody>
{comp_rows}
      </tbody>
    </table>
  </div>

  <!-- SERP Feature Presence -->
  <h2>SERP Feature Presence (Brand Keyword: "{brand_name}")</h2>
  <div class="section">
    <h3>Detected SERP Features</h3>
    <p>{feat_badges}</p>
    <h3>Client Position</h3>
    <p style="font-size:14px;color:#666;">
      Position: {client_pos if client_pos else 'Not ranked'} &middot;
      URL: {('<a href="' + client_url + '" target="_blank" style="color:#0066cc">' + client_url + '</a>') if client_url else 'N/A'}
    </p>
    <h3>Top Organic Results</h3>
    <table>
      <thead>
        <tr><th>Position</th><th>Domain</th><th>Title</th></tr>
      </thead>
      <tbody>
{organic_rows}
      </tbody>
    </table>
  </div>

  <!-- Brand Keyword Volume -->
  <h2>Brand Keyword Volume</h2>
  <div class="section">
    <div class="summary-cards" style="grid-template-columns: repeat(3,1fr);">
      <div class="summary-card"><div class="num">{bk_vol:,}</div><div class="label">Monthly Search Volume</div></div>
      <div class="summary-card"><div class="num">${bk_cpc:.2f}</div><div class="label">CPC</div></div>
      <div class="summary-card"><div class="num">{bk_comp_label}</div><div class="label">Competition</div></div>
    </div>
    {('<h3>Monthly Search Volume Trend</h3><div class="chart-box">' + monthly_chart + '</div>') if monthly_chart else ''}
  </div>

  <!-- Action Items -->
  <h2>Action Items &amp; Historical Insights</h2>
  <div class="section">
{actions_html}  </div>

  <div class="footer">
    Zenfusion Domain History Audit &middot; Powered by DataForSEO &middot; {generated_at} &middot; noindex, nofollow
  </div>
</body>
</html>'''

    # Write output
    os.makedirs(output_path, exist_ok=True)
    html_file = os.path.join(output_path, f'{domain}_domain_history_audit.html')
    with open(html_file, 'w', encoding='utf-8') as f:
        f.write(html)
    # Save raw JSON
    json_file = os.path.join(output_path, f'{domain}_domain_history_data.json')
    with open(json_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, default=str)
    print(f"\n  v Report saved: {html_file}")
    print(f"  v Data saved:   {json_file}")
    return html_file


# =====================================================================
# CLI
# =====================================================================
def main():
    parser = argparse.ArgumentParser(
        description='Generate a domain-level SEO history and visibility audit.')
    parser.add_argument('--domain', required=True,
                        help='Target domain (e.g. softprowatersystems.com)')
    parser.add_argument('--output', default='/tmp/domain_history_audit',
                        help='Output directory for the HTML report')
    parser.add_argument('--location-code', type=int,
                        default=DEFAULT_LOCATION_CODE,
                        help='DataForSEO location code (default: 2840 = US)')
    parser.add_argument('--language-code', default=DEFAULT_LANGUAGE_CODE,
                        help='DataForSEO language code (default: en)')
    args = parser.parse_args()

    domain = _clean_domain(args.domain)
    data = fetch_domain_history(domain)
    report_path = generate_history_report(domain, data, args.output)
    print(f"\nDone. Report: {report_path}")


if __name__ == '__main__':
    main()