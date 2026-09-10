#!/usr/bin/env python3
"""
SEO Visibility Report Generator
=================================
Standalone script that generates a domain-level SEO visibility report.

Fetches data from DataForSEO API:
  - Domain rank overview (traffic estimates, position distribution, keyword counts)
  - Competitor intersection (overlapping keywords between client and competitors)
  - Backlink summary (overall backlink health metrics)
  - SERP features (AI Overview, featured snippets, PAA, etc.)

Cross-references with GSC data (gsc_helper.py) when available to compare
real impressions vs estimated traffic.

Integrates with perplexity_helper.py for AEO citation testing.

Output: White SaaS theme HTML report.

Usage:
  python3 seo_visibility_report.py --domain softprowatersystems.com \
      --competitors aquasana.com,culligan.com \
      --keyword 'best water softener' \
      --output /tmp/seo_visibility_test

Environment:
  DATAFORSEO_LOGIN    - API login email
  DATAFORSEO_PASSWORD - API password
  PERPLEXITY_API_KEY   - (optional) for AEO citation testing
"""

import os, sys, json, base64, math, argparse, time
import urllib.request, urllib.error
from datetime import datetime

# === CONFIG ===
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOCATION_CODE = 2840  # United States
DEFAULT_LANGUAGE_CODE = 'en'
API_BASE = 'https://api.dataforseo.com/v3'
API_TIMEOUT = 60

# === CREDENTIALS ===
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


def dfs_api_call(endpoint, payload=None, method='POST'):
    """Make a DataForSEO API call. Payload should be a list (array of task objects)."""
    login, password = get_creds()
    creds = base64.b64encode(f'{login}:{password}'.encode()).decode()
    url = f'{API_BASE}/{endpoint}'
    headers = {
        'Authorization': f'Basic {creds}',
        'Content-Type': 'application/json'
    }
    if method == 'POST' and payload is not None:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    else:
        req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            data = json.loads(resp.read())
        if data.get('status_code') != 20000:
            print(f"  API Error: {data.get('status_code')} - {data.get('status_message')}")
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
    if task.get('status_code') != 20000:
        print(f"  Task error: {task.get('status_message', 'unknown')}")
    results = task.get('result', [])
    if not results:
        return None
    return results[0] if isinstance(results, list) else results


# ============================================================
# 1. DOMAIN OVERVIEW
# ============================================================
def _calc_avg_position(organic):
    """Calculate weighted average position from position buckets."""
    midpoints = [
        ('pos_1', 1), ('pos_2_3', 2.5), ('pos_4_10', 7),
        ('pos_11_20', 15.5), ('pos_21_30', 25.5),
        ('pos_31_40', 35.5), ('pos_41_50', 45.5),
        ('pos_51_60', 55.5), ('pos_61_70', 65.5),
        ('pos_71_80', 75.5), ('pos_81_90', 85.5), ('pos_91_100', 95.5),
    ]
    total_kw = sum(organic.get(k, 0) for k, _ in midpoints)
    if total_kw == 0:
        return 0
    weighted = sum(organic.get(k, 0) * mp for k, mp in midpoints)
    return weighted / total_kw


def get_domain_overview(domain, location_code=DEFAULT_LOCATION_CODE, language_code=DEFAULT_LANGUAGE_CODE):
    """
    Fetch domain rank overview: estimated traffic, position buckets, keyword counts.

    Returns dict with: etv, keywords_count, pos_1, pos_2_3, pos_4_10, pos_11_20,
                      pos_21_30, pos_31_100, avg_position, vi
    """
    print(f"  → Fetching domain overview for {domain}...")
    payload = [{
        'target': domain,
        'location_code': location_code,
        'language_code': language_code
    }]
    data = dfs_api_call('dataforseo_labs/google/domain_rank_overview/live', payload, 'POST')
    result = _extract_first_result(data)
    if not result:
        print("  ✗ No domain overview data returned")
        return None

    # DataForSEO nests metrics inside items[0]['metrics']['organic']
    items = result.get('items', [])
    if not items:
        print("  ✗ No items in domain overview response")
        return None
    item = items[0] if isinstance(items, list) else items
    metrics = item.get('metrics', {})
    organic = metrics.get('organic', {})

    # Sum pos_31_40 through pos_91_100 into pos_31_100
    pos_31_100 = 0
    for i in range(31, 101, 10):
        pos_31_100 += organic.get(f'pos_{i}_{i+9}', 0)

    keywords_count = organic.get('count', 0)
    etv = organic.get('etv', 0)

    overview = {
        'etv': round(etv, 2),
        'keywords_count': keywords_count,
        'pos_1': organic.get('pos_1', 0),
        'pos_2_3': organic.get('pos_2_3', 0),
        'pos_4_10': organic.get('pos_4_10', 0),
        'pos_11_20': organic.get('pos_11_20', 0),
        'pos_21_30': organic.get('pos_21_30', 0),
        'pos_31_100': pos_31_100,
        'avg_position': round(_calc_avg_position(organic), 1),
    }

    # Visibility Index formula (per skill)
    vi = (
        overview['pos_1'] * 1.0 +
        overview['pos_2_3'] * 0.85 +
        overview['pos_4_10'] * 0.5 +
        overview['pos_11_20'] * 0.2 +
        overview['pos_21_30'] * 0.05
    ) / max(keywords_count, 1) * 100
    overview['vi'] = round(vi, 1)

    print(f"  ✓ ETV: {overview['etv']:,} | Keywords: {keywords_count} | VI: {overview['vi']}")
    return overview


# ============================================================
# 2. COMPETITOR INTERSECTION
# ============================================================
def get_competitor_intersection(domain, competitors, keywords=None,
                                location_code=DEFAULT_LOCATION_CODE,
                                 language_code=DEFAULT_LANGUAGE_CODE):
    """
    Find overlapping keywords between client and competitors.

    Primary approach: try domain_intersection/live (per-domain overlap).
    Fallback: serp_competitors/live using tracked keywords to get per-keyword
    competitor positions and ETV.

    Returns dict: {competitor_domain: {intersections, client_etv, competitor_etv, avg_pos}}
    """
    print(f"  → Fetching competitor intersections for {domain} vs {competitors}...")
    results = {}

    # --- Primary: domain_intersection/live ---
    use_fallback = False
    for comp in competitors:
        payload = [{
            'target1': domain,
            'target2': comp,
            'location_code': location_code,
            'language_code': language_code,
            'intersect': True,
            'limit': 100
        }]
        data = dfs_api_call('dataforseo_labs/google/domain_intersection/live', payload, 'POST')
        result = _extract_first_result(data)
        if result and result.get('items'):
            items = result.get('items', [])
            intersections = len(items)
            client_etv = 0
            competitor_etv = 0
            for item in items:
                client_metrics = item.get('metrics', {})
                competitor_metrics = item.get('intersection_metrics', {})
                client_etv += client_metrics.get('etv', 0)
                competitor_etv += competitor_metrics.get('etv', 0)
            results[comp] = {
                'intersections': intersections,
                'client_etv': round(client_etv, 2),
                'competitor_etv': round(competitor_etv, 2),
                'avg_pos': round(result.get('avg_position', 0), 1),
            }
            print(f"    ✓ {comp}: {intersections} overlapping keywords")
        else:
            use_fallback = True
            results[comp] = {'intersections': 0, 'client_etv': 0, 'competitor_etv': 0, 'avg_pos': 0}

    # --- Fallback: serp_competitors/live using tracked keywords ---
    if use_fallback and keywords:
        print("  → Domain intersection unavailable, using serp_competitors fallback...")
        payload = [{
            'keywords': keywords,
            'location_code': location_code,
            'language_code': language_code,
            'limit': 100
        }]
        data = dfs_api_call('dataforseo_labs/google/serp_competitors/live', payload, 'POST')
        result = _extract_first_result(data)
        if result and result.get('items'):
            # Build a lookup of domain -> competitor data
            serp_comp_map = {}
            for item in result['items']:
                comp_domain = item.get('domain', '').replace('www.', '')
                serp_comp_map[comp_domain] = item

            # Also find the client domain in the SERP competitors
            client_domain_clean = domain.replace('www.', '')
            client_item = serp_comp_map.get(client_domain_clean)

            for comp in competitors:
                comp_clean = comp.replace('www.', '')
                comp_item = serp_comp_map.get(comp_clean)
                if comp_item:
                    kw_positions = comp_item.get('keywords_positions', {})
                    intersections = len(kw_positions)
                    comp_etv = comp_item.get('etv', 0)
                    avg_pos = comp_item.get('avg_position', 0)
                    results[comp] = {
                        'intersections': intersections,
                        'client_etv': round(client_item.get('etv', 0), 2) if client_item else 0,
                        'competitor_etv': round(comp_etv, 2),
                        'avg_pos': round(avg_pos, 1),
                    }
                    print(f"    ✓ {comp}: {intersections} overlapping keywords (serp_competitors)")
                else:
                    results[comp] = {
                        'intersections': 0,
                        'client_etv': round(client_item.get('etv', 0), 2) if client_item else 0,
                        'competitor_etv': 0,
                        'avg_pos': 0,
                    }
                    print(f"    — {comp}: not found in SERP competitors")

    return results


# ============================================================
# 3. BACKLINK SUMMARY
# ============================================================
def get_backlink_summary(domain):
    """
    Overall backlink health metrics.

    Returns dict with: dr (0-100), backlinks, referring_domains,
                      referring_ips, broken_backlinks, broken_pages
    """
    print(f"  → Fetching backlink summary for {domain}...")
    payload = [{
        'target': domain,
        'include_subdomains': True,
        'internal_list_limit': 1,
        'backlinks_status_type': 'all'
    }]
    data = dfs_api_call('backlinks/summary/live', payload, 'POST')
    result = _extract_first_result(data)
    if not result:
        print("  ✗ No backlink data returned")
        return None

    rank = result.get('rank', 0)
    # DR normalization: rank is 0-1000, convert to 0-100
    dr = round(math.sin(rank / 636.62) * 100, 1) if rank else 0

    summary = {
        'dr': dr,
        'rank_raw': rank,
        'backlinks': result.get('backlinks', 0),
        'referring_domains': result.get('referring_domains', 0),
        'referring_main_domains': result.get('referring_main_domains', 0),
        'referring_ips': result.get('referring_ips', 0),
        'referring_subnets': result.get('referring_subnets', 0),
        'backlinks_dofollow': result.get('backlinks', 0) - result.get('referring_pages_nofollow', 0),
        'backlinks_nofollow': result.get('referring_pages_nofollow', 0),
        'broken_backlinks': result.get('broken_backlinks', 0),
        'broken_pages': result.get('broken_pages', 0),
    }
    print(f"  ✓ DR: {summary['dr']} | Backlinks: {summary['backlinks']:,} | Ref domains: {summary['referring_domains']:,}")
    return summary


# ============================================================
# 4. SERP FEATURES
# ============================================================
def get_serp_features(keyword, location_code=DEFAULT_LOCATION_CODE, language_code=DEFAULT_LANGUAGE_CODE):
    """
    Check what SERP features appear for a keyword (AI Overview, featured snippets, PAA, etc.).

    Returns dict with: keyword, features (list), client_position, client_url,
                      organic_results (top 10)
    """
    print(f"  → Fetching SERP features for '{keyword}'...")
    payload = [{
        'keyword': keyword,
        'location_code': location_code,
        'language_code': language_code,
        'device': 'desktop',
        'os': 'windows',
        'depth': 30,
        'use_improved_etv': True
    }]
    data = dfs_api_call('serp/google/organic/live/advanced', payload, 'POST')
    result = _extract_first_result(data)
    if not result:
        print(f"  ✗ No SERP data for '{keyword}'")
        return None

    items = result.get('items', [])
    features = []
    organic_results = []
    client_position = None
    client_url = None

    # Known SERP feature types
    feature_types = {
        'featured_snippet', 'people_also_ask', 'people_also_search',
        'local_pack', 'video', 'image', 'images', 'news',
        'shopping', 'top_stories', 'twitter', 'questions',
        'ai_overview', 'knowledge_graph', 'map', 'carousel',
        'recipe', 'jobs', 'flights', 'hotels', 'pack'
    }

    for item in items:
        item_type = item.get('type', 'unknown')
        rank_group = item.get('rank_group', 0)
        rank_absolute = item.get('rank_absolute', 0)

        if item_type == 'organic':
            organic_results.append({
                'position': rank_absolute,
                'domain': item.get('domain', ''),
                'url': item.get('url', ''),
                'title': item.get('title', '')[:100],
            })
        elif item_type in feature_types:
            features.append({
                'type': item_type,
                'position': rank_absolute,
                'domain': item.get('domain', ''),
                'title': item.get('title', '')[:80],
            })

    result_data = {
        'keyword': keyword,
        'features': features,
        'feature_types': list(set(f['type'] for f in features)),
        'organic_results': organic_results[:10],
        'client_position': client_position,
        'client_url': client_url,
        'result_count': result.get('result_count', 0),
    }
    feat_names = ', '.join(result_data['feature_types']) if result_data['feature_types'] else 'none'
    print(f"  ✓ Features: {feat_names} | Organic results: {len(organic_results)}")
    return result_data


# ============================================================
# GSC INTEGRATION (optional)
# ============================================================
def try_get_gsc_data(domain, keywords):
    """
    Try to cross-reference with GSC data via gsc_helper.py.
    Returns dict of keyword -> {impressions, clicks, ctr, position} or None.
    """
    try:
        sys.path.insert(0, SCRIPT_DIR)
        from gsc_helper import get_keyword_performance, list_properties
    except Exception:
        return None

    # Try to find matching GSC property
    site_url = f'https://{domain}/'
    try:
        properties = list_properties()
        # Find property matching our domain
        matching = [p for p in properties if domain.replace('www.', '') in p.replace('www.', '')]
        if matching:
            site_url = matching[0]
        else:
            return None
    except Exception:
        return None

    gsc_data = {}
    for kw in keywords:
        try:
            perf = get_keyword_performance(site_url, kw, days=28)
            if perf:
                gsc_data[kw] = perf
        except Exception:
            pass
    return gsc_data if gsc_data else None


# ============================================================
# PERPLEXITY INTEGRATION (optional)
# ============================================================
def try_get_aeo_data(domain, keywords, brand_name=None):
    """
    Try to get AEO citation data via perplexity_helper.py.
    Returns dict of keyword -> {cited, mentioned} or None.
    """
    try:
        sys.path.insert(0, SCRIPT_DIR)
        from perplexity_helper import test_aeo_citation
    except Exception:
        return None

    aeo_data = {}
    for kw in keywords[:5]:  # Limit to 5 keywords for cost
        try:
            result = test_aeo_citation(kw, domain, brand_name)
            if 'error' not in result:
                aeo_data[kw] = {
                    'cited': result.get('cited', False),
                    'mentioned': result.get('mentioned', False),
                    'citation_urls': result.get('citation_urls', []),
                }
        except Exception:
            pass
    return aeo_data if aeo_data else None


# ============================================================
# 5. GENERATE VISIBILITY REPORT
# ============================================================
def generate_visibility_report(domain, competitors, keywords, output_path,
                                location_code=DEFAULT_LOCATION_CODE,
                                language_code=DEFAULT_LANGUAGE_CODE):
    """
    Full HTML report with:
      - Executive summary (traffic, position distribution, visibility trend)
      - Keyword performance table (position, volume, SERP features)
      - Competitor comparison (overlapping keywords, visibility gap)
      - Backlink profile summary
      - Action items / opportunities
    """
    print(f"\n{'='*60}")
    print(f"  SEO Visibility Report for {domain}")
    print(f"  Competitors: {', '.join(competitors)}")
    print(f"  Keywords: {', '.join(keywords)}")
    print(f"{'='*60}\n")

    report_data = {
        'domain': domain,
        'competitors': competitors,
        'keywords': keywords,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
    }

    # 1. Domain Overview
    print("[1/5] Domain Overview")
    overview = get_domain_overview(domain, location_code, language_code)
    report_data['overview'] = overview

    # Also get competitor overviews for comparison
    comp_overviews = {}
    for comp in competitors:
        co = get_domain_overview(comp, location_code, language_code)
        comp_overviews[comp] = co
    report_data['competitor_overviews'] = comp_overviews

    # 2. Competitor Intersection
    print("\n[2/5] Competitor Intersection")
    intersections = get_competitor_intersection(domain, competitors, keywords=keywords,
                                                location_code=location_code,
                                                language_code=language_code)
    report_data['intersections'] = intersections

    # 3. Backlink Summary
    print("\n[3/5] Backlink Summary")
    backlinks = get_backlink_summary(domain)
    report_data['backlinks'] = backlinks

    # Also get competitor backlinks for comparison
    comp_backlinks = {}
    for comp in competitors:
        cb = get_backlink_summary(comp)
        comp_backlinks[comp] = cb
    report_data['competitor_backlinks'] = comp_backlinks

    # 4. SERP Features per keyword
    print("\n[4/5] SERP Features")
    serp_data = {}
    for kw in keywords:
        serp = get_serp_features(kw, location_code, language_code)
        serp_data[kw] = serp
        time.sleep(0.5)  # Rate limit
    report_data['serp_features'] = serp_data

    # 5. GSC cross-reference (optional)
    print("\n[5/5] GSC Cross-reference")
    gsc_data = try_get_gsc_data(domain, keywords)
    report_data['gsc_data'] = gsc_data
    if gsc_data:
        print(f"  ✓ GSC data found for {len(gsc_data)} keywords")
    else:
        print("  — No GSC data available (gsc_helper not configured or no matching property)")

    # AEO data (optional)
    aeo_data = try_get_aeo_data(domain, keywords)
    report_data['aeo_data'] = aeo_data
    if aeo_data:
        print(f"  ✓ AEO citation data for {len(aeo_data)} keywords")

    # Generate HTML
    print("\nGenerating HTML report...")
    html = _build_html_report(report_data)

    # Write output
    os.makedirs(output_path, exist_ok=True)
    html_file = os.path.join(output_path, f'{domain}_seo_visibility_report.html')
    with open(html_file, 'w', encoding='utf-8') as f:
        f.write(html)

    # Also save raw JSON data
    json_file = os.path.join(output_path, f'{domain}_seo_visibility_data.json')
    with open(json_file, 'w', encoding='utf-8') as f:
        json.dump(report_data, f, indent=2, default=str)

    print(f"\n{'='*60}")
    print(f"  ✓ Report saved: {html_file}")
    print(f"  ✓ Data saved:   {json_file}")
    print(f"{'='*60}")

    return html_file


def _build_html_report(data):
    """Build the white SaaS theme HTML report."""

    domain = data['domain']
    competitors = data['competitors']
    keywords = data['keywords']
    overview = data.get('overview') or {}
    generated_at = data.get('generated_at', '')

    # === EXECUTIVE SUMMARY CARDS ===
    etv = overview.get('etv', 0)
    kw_count = overview.get('keywords_count', 0)
    vi = overview.get('vi', 0)
    avg_pos = overview.get('avg_position', 0)
    page1 = overview.get('pos_1', 0) + overview.get('pos_2_3', 0) + overview.get('pos_4_10', 0)

    # === POSITION DISTRIBUTION CHART ===
    pos_labels = ['Top 1', 'Top 2-3', 'Top 4-10', 'Top 11-20', 'Top 21-30', 'Top 31-100']
    pos_values = [
        overview.get('pos_1', 0),
        overview.get('pos_2_3', 0),
        overview.get('pos_4_10', 0),
        overview.get('pos_11_20', 0),
        overview.get('pos_21_30', 0),
        overview.get('pos_31_100', 0),
    ]
    max_pos = max(pos_values) if pos_values else 1
    pos_bars = ''
    pos_colors = ['#27ae60', '#2ecc71', '#0066cc', '#f39c12', '#e67e22', '#cc0000']
    for i, (label, val) in enumerate(zip(pos_labels, pos_values)):
        pct = (val / max(max_pos, 1)) * 100
        pos_bars += f'''
        <div class="bar-row">
          <div class="bar-label">{label}</div>
          <div class="bar-track"><div class="bar-fill" style="width:{pct:.1f}%;background:{pos_colors[i]}"></div></div>
          <div class="bar-value">{val:,}</div>
        </div>'''

    # === COMPETITOR COMPARISON TABLE ===
    comp_overviews = data.get('competitor_overviews') or {}
    intersections = data.get('intersections') or {}
    comp_backlinks = data.get('competitor_backlinks') or {}

    comp_rows = ''
    # Client row
    comp_rows += f'''
      <tr style="background:#e7f3ff;font-weight:600">
        <td>{domain}</td>
        <td>{etv:,.0f}</td>
        <td>{kw_count:,}</td>
        <td>{vi}</td>
        <td>{avg_pos:.1f}</td>
        <td>—</td>
      </tr>'''
    for comp in competitors:
        co = comp_overviews.get(comp) or {}
        inter = intersections.get(comp) or {}
        comp_etv = co.get('etv', 0)
        comp_kw = co.get('keywords_count', 0)
        comp_vi = co.get('vi', 0)
        comp_avg = co.get('avg_position', 0)
        overlap = inter.get('intersections', 0)
        comp_rows += f'''
      <tr>
        <td>{comp}</td>
        <td>{comp_etv:,.0f}</td>
        <td>{comp_kw:,}</td>
        <td>{comp_vi}</td>
        <td>{comp_avg:.1f}</td>
        <td>{overlap:,}</td>
      </tr>'''

    # === BACKLINK PROFILE ===
    backlinks = data.get('backlinks') or {}
    bl_dr = backlinks.get('dr', 0)
    bl_total = backlinks.get('backlinks', 0)
    bl_ref_domains = backlinks.get('referring_domains', 0)
    bl_ref_ips = backlinks.get('referring_ips', 0)
    bl_dofollow = backlinks.get('backlinks_dofollow', 0)
    bl_nofollow = backlinks.get('backlinks_nofollow', 0)
    bl_broken = backlinks.get('broken_backlinks', 0)

    comp_bl_rows = ''
    for comp in competitors:
        cb = comp_backlinks.get(comp) or {}
        comp_bl_rows += f'''
      <tr>
        <td>{comp}</td>
        <td>{cb.get('dr', 0)}</td>
        <td>{cb.get('backlinks', 0):,}</td>
        <td>{cb.get('referring_domains', 0):,}</td>
        <td>{cb.get('backlinks_dofollow', 0):,}</td>
      </tr>'''

    # === KEYWORD PERFORMANCE TABLE ===
    serp_features = data.get('serp_features') or {}
    gsc_data = data.get('gsc_data') or {}
    aeo_data = data.get('aeo_data') or {}

    kw_rows = ''
    for kw in keywords:
        serp = serp_features.get(kw) or {}
        features = serp.get('feature_types', [])
        feat_html = ' '.join(f'<span class="feat-badge">{f}</span>' for f in features) if features else '<span style="color:#999">—</span>'

        # Find client position in organic results
        client_pos = '—'
        for result in serp.get('organic_results', []):
            if domain.replace('www.', '') in (result.get('domain', '') or '').replace('www.', ''):
                client_pos = str(result['position'])
                break

        # GSC data
        gsc = gsc_data.get(kw, {})
        gsc_imp = gsc.get('impressions', '—')
        gsc_ctr = f"{gsc['ctr']:.1%}" if gsc.get('ctr') is not None else '—'
        gsc_pos = f"{gsc['position']:.1f}" if gsc.get('position') is not None else '—'

        # AEO data
        aeo = aeo_data.get(kw, {})
        aeo_badge = ''
        if aeo:
            if aeo.get('cited'):
                aeo_badge = '<span class="aeo-badge aeo-cited">AI Cited</span>'
            elif aeo.get('mentioned'):
                aeo_badge = '<span class="aeo-badge aeo-mentioned">AI Mentioned</span>'
            else:
                aeo_badge = '<span class="aeo-badge aeo-missed">AI Missed</span>'

        kw_safe = kw.replace('<', '&lt;').replace('>', '&gt;')
        kw_rows += f'''
      <tr>
        <td><strong>{kw_safe}</strong></td>
        <td>{client_pos}</td>
        <td>{gsc_imp if gsc_imp != '—' else '—'}</td>
        <td>{gsc_ctr}</td>
        <td>{gsc_pos}</td>
        <td>{feat_html}</td>
        <td>{aeo_badge}</td>
      </tr>'''

    # === ACTION ITEMS ===
    actions = _generate_action_items(data)

    # === BUILD HTML ===
    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex,nofollow">
<title>SEO Visibility Report — {domain}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #ffffff; color: #1a1a1a; font-size: 16px; margin: 0; padding: 20px; max-width: 1200px; margin: 0 auto; }}
  h1 {{ font-size: 28px; color: #1a1a1a; margin-bottom: 4px; }}
  h2 {{ font-size: 20px; color: #1a1a1a; margin-top: 36px; margin-bottom: 14px; border-bottom: 2px solid #0066cc; padding-bottom: 6px; }}
  h3 {{ font-size: 17px; color: #333; margin-top: 20px; margin-bottom: 10px; }}
  .subtitle {{ font-size: 15px; color: #666; margin-bottom: 20px; }}
  .url-headline {{ font-size: 20px; font-weight: 700; color: #0066cc; margin-bottom: 20px; }}
  .url-headline a {{ color: #0066cc; text-decoration: none; }}
  .summary-cards {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 24px; }}
  .summary-card {{ background: #f8f9fa; border: 1px solid #e0e0e0; border-radius: 10px; padding: 16px; text-align: center; }}
  .summary-card .num {{ font-size: 26px; font-weight: 700; color: #0066cc; }}
  .summary-card .label {{ font-size: 13px; color: #666; text-transform: uppercase; margin-top: 4px; }}
  .section {{ background: #f8f9fa; border: 1px solid #e0e0e0; border-radius: 10px; padding: 20px; margin-bottom: 20px; }}
  table {{ width: 100%; border-collapse: collapse; background: #ffffff; }}
  th {{ background: #f0f0f0; color: #1a1a1a; font-weight: 700; font-size: 14px; padding: 10px 12px; text-align: left; border-bottom: 2px solid #e0e0e0; }}
  td {{ padding: 9px 12px; border-bottom: 1px solid #e0e0e0; font-size: 14px; color: #1a1a1a; }}
  tr:hover {{ background: #f8f9fa; }}
  .bar-row {{ display: flex; align-items: center; margin-bottom: 8px; }}
  .bar-label {{ width: 100px; font-size: 14px; font-weight: 600; color: #333; }}
  .bar-track {{ flex: 1; height: 24px; background: #f0f0f0; border-radius: 6px; margin: 0 12px; overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 6px; transition: width 0.5s ease; }}
  .bar-value {{ width: 60px; text-align: right; font-size: 14px; font-weight: 600; color: #1a1a1a; }}
  .feat-badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: 600; background: #e7f3ff; color: #0066cc; margin: 2px; }}
  .aeo-badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: 600; margin: 2px; }}
  .aeo-cited {{ background: #e8f5e9; color: #27ae60; }}
  .aeo-mentioned {{ background: #fff3e0; color: #e67e22; }}
  .aeo-missed {{ background: #ffebee; color: #c62828; }}
  .action-item {{ background: #ffffff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 14px; margin-bottom: 12px; }}
  .action-item h4 {{ margin: 0 0 6px 0; font-size: 15px; color: #0066cc; }}
  .action-item p {{ margin: 0; font-size: 14px; color: #555; line-height: 1.5; }}
  .action-item .meta {{ font-size: 12px; color: #999; margin-top: 8px; }}
  .footer {{ margin-top: 30px; padding-top: 15px; border-top: 1px solid #e0e0e0; font-size: 13px; color: #999; text-align: center; }}
  .gsc-note {{ font-size: 13px; color: #888; font-style: italic; margin-bottom: 10px; }}
</style>
</head>
<body>
  <h1>SEO Visibility Report</h1>
  <div class="url-headline"><a href="https://{domain}" target="_blank">{domain}</a></div>
  <div class="subtitle">Generated: {generated_at} · DataForSEO + GSC + Perplexity AEO</div>

  <!-- Executive Summary -->
  <h2>Executive Summary</h2>
  <div class="summary-cards">
    <div class="summary-card"><div class="num">{etv:,.0f}</div><div class="label">Est. Monthly Traffic</div></div>
    <div class="summary-card"><div class="num">{kw_count:,}</div><div class="label">Keywords Ranked</div></div>
    <div class="summary-card"><div class="num">{vi}</div><div class="label">Visibility Index</div></div>
    <div class="summary-card"><div class="num">{avg_pos:.1f}</div><div class="label">Avg Position</div></div>
    <div class="summary-card"><div class="num">{page1:,}</div><div class="label">Page 1 Keywords</div></div>
  </div>

  <div class="section">
    <h3>Position Distribution</h3>
    {pos_bars}
  </div>

  <!-- Keyword Performance -->
  <h2>Keyword Performance</h2>
  <div class="gsc-note">
    {'GSC real data available — showing actual impressions & position alongside SERP features.' if gsc_data else 'No GSC data available — showing SERP features only. Connect GSC via gsc_helper.py for real impression data.'}
  </div>
  <div class="section">
    <table>
      <thead>
        <tr>
          <th>Keyword</th>
          <th>SERP Pos</th>
          <th>GSC Impressions</th>
          <th>GSC CTR</th>
          <th>GSC Avg Pos</th>
          <th>SERP Features</th>
          <th>AI Visibility</th>
        </tr>
      </thead>
      <tbody>
{kw_rows}      </tbody>
    </table>
  </div>

  <!-- Competitor Comparison -->
  <h2>Competitor Comparison</h2>
  <div class="section">
    <table>
      <thead>
        <tr>
          <th>Domain</th>
          <th>Est. Traffic</th>
          <th>Keywords</th>
          <th>Visibility Index</th>
          <th>Avg Position</th>
          <th>Overlap with {domain}</th>
        </tr>
      </thead>
      <tbody>
{comp_rows}      </tbody>
    </table>
  </div>

  <!-- Backlink Profile -->
  <h2>Backlink Profile</h2>
  <div class="section">
    <h3>{domain}</h3>
    <div class="summary-cards" style="grid-template-columns: repeat(5,1fr);">
      <div class="summary-card"><div class="num">{bl_dr}</div><div class="label">Domain Rating</div></div>
      <div class="summary-card"><div class="num">{bl_total:,}</div><div class="label">Total Backlinks</div></div>
      <div class="summary-card"><div class="num">{bl_ref_domains:,}</div><div class="label">Referring Domains</div></div>
      <div class="summary-card"><div class="num">{bl_dofollow:,}</div><div class="label">Dofollow</div></div>
      <div class="summary-card"><div class="num">{bl_broken}</div><div class="label">Broken Links</div></div>
    </div>
    <p style="font-size:14px;color:#666;margin-top:12px;">Referring IPs: {bl_ref_ips:,} · Nofollow: {bl_nofollow:,}</p>

    <h3>Competitor Backlink Comparison</h3>
    <table>
      <thead>
        <tr>
          <th>Domain</th>
          <th>DR</th>
          <th>Total Backlinks</th>
          <th>Referring Domains</th>
          <th>Dofollow</th>
        </tr>
      </thead>
      <tbody>
        <tr style="background:#e7f3ff;font-weight:600">
          <td>{domain}</td>
          <td>{bl_dr}</td>
          <td>{bl_total:,}</td>
          <td>{bl_ref_domains:,}</td>
          <td>{bl_dofollow:,}</td>
        </tr>
{comp_bl_rows}      </tbody>
    </table>
  </div>

  <!-- Action Items -->
  <h2>Action Items & Opportunities</h2>
  <div class="section">
{actions}  </div>

  <div class="footer">
    Zenfusion SEO Visibility Report · Powered by DataForSEO + GSC + Perplexity · {generated_at} · noindex, nofollow
  </div>
</body>
</html>'''

    return html


def _generate_action_items(data):
    """Generate data-grounded action items based on report data."""
    actions = []

    domain = data['domain']
    overview = data.get('overview') or {}
    backlinks = data.get('backlinks') or {}
    intersections = data.get('intersections') or {}
    serp_features = data.get('serp_features') or {}
    aeo_data = data.get('aeo_data') or {}
    competitors = data.get('competitors', [])

    # Action 1: Page 1 gap
    page1 = overview.get('pos_1', 0) + overview.get('pos_2_3', 0) + overview.get('pos_4_10', 0)
    total_kw = overview.get('keywords_count', 0)
    if total_kw > 0:
        page1_pct = (page1 / total_kw) * 100
        actions.append({
            'title': f'Improve Page 1 Coverage ({page1:,} of {total_kw:,} keywords)',
            'why': f'Only {page1_pct:.1f}% of ranked keywords are on page 1. Lifting keywords from positions 11-20 to the top 10 can significantly increase organic traffic.',
            'effort': 'High',
            'impact': 'High',
        })

    # Action 2: Competitor keyword gap
    if intersections:
        max_overlap = max((i.get('intersections', 0) for i in intersections.values()), default=0)
        top_comp = max(intersections.items(), key=lambda x: x[1].get('intersections', 0))[0] if intersections else ''
        if max_overlap > 0:
            actions.append({
                'title': f'Target {max_overlap:,} Overlapping Keywords with {top_comp}',
                'why': f'{top_comp} shares {max_overlap} keywords with {domain}. Analyze which of these they outrank you on and optimize those pages.',
                'effort': 'Medium',
                'impact': 'High',
            })

    # Action 3: Backlink building
    if backlinks:
        dr = backlinks.get('dr', 0)
        ref_domains = backlinks.get('referring_domains', 0)
        if dr < 50 or ref_domains < 1000:
            actions.append({
                'title': f'Strengthen Backlink Profile (DR {dr}, {ref_domains:,} referring domains)',
                'why': f'Domain Rating of {dr} and {ref_domains:,} referring domains indicates room for link building improvement. Focus on acquiring high-quality dofollow links from authoritative domains in your niche.',
                'effort': 'High',
                'impact': 'Medium',
            })

    # Action 4: SERP feature opportunities
    feat_keywords = {}
    for kw, serp in serp_features.items():
        if serp and serp.get('feature_types'):
            for feat in serp['feature_types']:
                feat_keywords.setdefault(feat, []).append(kw)

    if 'featured_snippet' in feat_keywords:
        kws = feat_keywords['featured_snippet']
        actions.append({
            'title': f'Optimize for Featured Snippets ({len(kws)} keywords)',
            'why': f'Featured snippets appear for: {", ".join(kws[:5])}. Structure content with clear Q&A format, concise paragraphs (40-60 words), and definition lists to win snippet positions.',
            'effort': 'Low',
            'impact': 'High',
        })

    if 'people_also_ask' in feat_keywords:
        kws = feat_keywords['people_also_ask']
        actions.append({
            'title': f'Target People Also Ask Questions ({len(kws)} keywords)',
            'why': f'PAA boxes appear for: {", ".join(kws[:5])}. Create FAQ sections answering common questions to capture PAA placements.',
            'effort': 'Low',
            'impact': 'Medium',
        })

    # Action 5: AI Overview / AEO
    if aeo_data:
        cited_count = sum(1 for v in aeo_data.values() if v.get('cited'))
        missed_count = sum(1 for v in aeo_data.values() if not v.get('cited') and not v.get('mentioned'))
        if missed_count > 0:
            actions.append({
                'title': f'Improve AI/LLM Citation Rate ({cited_count}/{len(aeo_data)} cited)',
                'why': f'{missed_count} keywords tested have no AI citation. Create comprehensive, authoritative content that AI models cite as sources. Ensure structured data and clear factual answers.',
                'effort': 'Medium',
                'impact': 'High',
            })
    elif feat_keywords.get('ai_overview'):
        actions.append({
            'title': 'AI Overview Detected — Optimize for GEO',
            'why': f'AI Overview appears for: {", ".join(feat_keywords["ai_overview"][:5])}. Ensure content is structured for AI extraction with clear summaries and factual data.',
            'effort': 'Medium',
            'impact': 'High',
        })

    # Fallback: if no actions generated
    if not actions:
        actions.append({
            'title': 'Continue SEO Monitoring',
            'why': 'No critical issues detected in this report. Continue regular monitoring of keyword positions, backlink profile, and SERP feature changes.',
            'effort': 'Low',
            'impact': 'Low',
        })

    # Build HTML
    html = ''
    for i, action in enumerate(actions, 1):
        html += f'''    <div class="action-item">
      <h4>{i}. {action['title']}</h4>
      <p>{action['why']}</p>
      <div class="meta">Effort: {action['effort']} · Impact: {action['impact']}</div>
    </div>
'''
    return html


# ============================================================
# CLI
# ============================================================
def main():
    parser = argparse.ArgumentParser(description='Generate SEO Visibility Report')
    parser.add_argument('--domain', required=True, help='Target domain (e.g. softprowatersystems.com)')
    parser.add_argument('--competitors', default='', help='Comma-separated competitor domains')
    parser.add_argument('--keyword', action='append', help='Keyword to analyze (can be repeated)')
    parser.add_argument('--keywords', help='Comma-separated keywords')
    parser.add_argument('--output', default='/tmp/seo_visibility_report', help='Output directory')
    parser.add_argument('--location-code', type=int, default=DEFAULT_LOCATION_CODE, help='DataForSEO location code')
    parser.add_argument('--language-code', default=DEFAULT_LANGUAGE_CODE, help='DataForSEO language code')

    args = parser.parse_args()

    # Parse competitors
    competitors = [c.strip() for c in args.competitors.split(',') if c.strip()] if args.competitors else []

    # Parse keywords
    keywords = []
    if args.keyword:
        keywords.extend(args.keyword)
    if args.keywords:
        keywords.extend([k.strip() for k in args.keywords.split(',') if k.strip()])
    if not keywords:
        keywords = ['best water softener']  # default

    # Clean domain
    domain = args.domain.replace('https://', '').replace('http://', '').replace('www.', '').rstrip('/')

    generate_visibility_report(
        domain=domain,
        competitors=competitors,
        keywords=keywords,
        output_path=args.output,
        location_code=args.location_code,
        language_code=args.language_code,
    )


if __name__ == '__main__':
    main()
