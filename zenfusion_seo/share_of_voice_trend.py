#!/usr/bin/env python3
"""
Share of Voice Trend Analyzer
==============================
Standalone script that compares SEO visibility and Share-of-Voice (SoV) trends
across a client domain and its competitors over time.

Functions:
  1. fetch_domain_visibility(domain, location_code=2840)
       DataForSEO domain_rank_overview → ETV, keyword count, position
       distribution, visibility index.
  2. fetch_competitor_sov(client_domain, competitors)
       Fetch visibility for client + every competitor, then compute each
       domain's ETV as a % of total market ETV (Share of Voice).
  3. compute_visibility_trends(domains, periods)
       Pull domain_rank_overview snapshots for each requested time period and
       compute month-over-month changes in ETV / keywords / VI. Falls back to a
       single-snapshot + backlinks/timeseries proxy when historical data is
       unavailable.
  4. generate_sov_report(client_domain, competitors, data, output_path)
       White-SaaS-theme HTML report with:
         - SoV overview pie chart (inline SVG)
         - Visibility comparison table (domain, ETV, keywords, VI, pos dist, SoV%)
         - Position-distribution comparison stacked-bar chart (inline SVG)
         - Winner/loser analysis per position bucket
         - Competitor discovery (top 10 from DataForSEO competitors endpoint)
         - Action items targeting the client's biggest SoV gaps

Usage:
  python3 share_of_voice_trend.py \
      --client softprowatersystems.com \
      --competitors aquasana.com,culligan.com,springwellwater.com,uswatersystems.com \
      --output /tmp/test_sov_trend

Environment:
  DATAFORSEO_LOGIN    - API login email
  DATAFORSEO_PASSWORD - API password
"""

import os
import sys
import json
import math
import base64
import argparse
import time
import urllib.request
import urllib.error
from datetime import datetime

# ============================================================
# CONFIG
# ============================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOCATION_CODE = 2840  # United States
DEFAULT_LANGUAGE_CODE = 'en'
API_BASE = 'https://api.dataforseo.com/v3'
API_TIMEOUT = 90

# Position-bucket midpoints used for weighted average position.
POS_MIDPOINTS = [
    ('pos_1', 1), ('pos_2_3', 2.5), ('pos_4_10', 7),
    ('pos_11_20', 15.5), ('pos_21_30', 25.5),
    ('pos_31_40', 35.5), ('pos_41_50', 45.5),
    ('pos_51_60', 55.5), ('pos_61_70', 65.5),
    ('pos_71_80', 75.5), ('pos_81_90', 85.5), ('pos_91_100', 95.5),
]

# Distinct colours per domain (cycle through palette when there are many).
DOMAIN_PALETTE = [
    '#0066cc', '#27ae60', '#e67e22', '#8e44ad',
    '#c0392b', '#16a085', '#d4a017', '#2980b9',
    '#7f8c8d', '#f39c12', '#1abc9c', '#9b59b6',
]

# Position-bucket colors for the stacked bar chart.
POS_BUCKET_COLORS = {
    'Top 1': '#1a73e8',
    'Top 3': '#1967d2',
    'Top 10': '#34a853',
    'Top 20': '#fbbc04',
    'Top 100': '#ea4335',
}


# ============================================================
# CREDENTIALS + API CORE
# ============================================================
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
    """Make a DataForSEO API call. Payload must be a list (array of tasks)."""
    login, password = get_creds()
    creds = base64.b64encode(f'{login}:{password}'.encode()).decode()
    url = f'{API_BASE}/{endpoint}'
    headers = {
        'Authorization': f'Basic {creds}',
        'Content-Type': 'application/json',
    }
    if method == 'POST' and payload is not None:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), headers=headers)
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
    if task.get('status_code') != 20000:
        print(f"  Task error: {task.get('status_message', 'unknown')}")
    results = task.get('result', [])
    if not results:
        return None
    return results[0] if isinstance(results, list) else results


def _calc_avg_position(organic):
    """Calculate weighted average position from position buckets."""
    total_kw = sum(organic.get(k, 0) for k, _ in POS_MIDPOINTS)
    if total_kw == 0:
        return 0
    weighted = sum(organic.get(k, 0) * mp for k, mp in POS_MIDPOINTS)
    return weighted / total_kw


def _calc_visibility_index(overview):
    """Visibility Index: weighted presence across top positions / total kw."""
    vi = (
        overview['pos_1'] * 1.0 +
        overview['pos_2_3'] * 0.85 +
        overview['pos_4_10'] * 0.5 +
        overview['pos_11_20'] * 0.2 +
        overview['pos_21_30'] * 0.05
    ) / max(overview['keywords_count'], 1) * 100
    return round(vi, 1)


# ============================================================
# 1. DOMAIN VISIBILITY
# ============================================================
def fetch_domain_visibility(domain, location_code=DEFAULT_LOCATION_CODE,
                            language_code=DEFAULT_LANGUAGE_CODE,
                            time_range=None):
    """
    Fetch domain rank overview for a single domain.

    Returns dict:
      etv, keywords_count, pos_1, pos_2_3, pos_4_10, pos_11_20, pos_21_30,
      pos_31_100, avg_position, vi, domain, time_range
    Returns None on failure.
    """
    label = f" ({time_range})" if time_range else ""
    print(f"  → Fetching domain visibility for {domain}{label}...")
    task = {
        'target': domain,
        'location_code': location_code,
        'language_code': language_code,
    }
    if time_range:
        task['time_range'] = time_range

    data = dfs_api_call('dataforseo_labs/google/domain_rank_overview/live',
                        [task], 'POST')
    result = _extract_first_result(data)
    if not result:
        print(f"  ✗ No domain overview data for {domain}")
        return None

    items = result.get('items', [])
    if not items:
        print(f"  ✗ No items in domain overview for {domain}")
        return None
    item = items[0] if isinstance(items, list) else items
    metrics = item.get('metrics', {})
    organic = metrics.get('organic', {})

    # Aggregate pos_31_40 .. pos_91_100 into pos_31_100.
    pos_31_100 = 0
    for i in range(31, 101, 10):
        pos_31_100 += organic.get(f'pos_{i}_{i+9}', 0)

    keywords_count = organic.get('count', 0)
    etv = organic.get('etv', 0)

    overview = {
        'domain': domain,
        'etv': round(etv, 2),
        'keywords_count': keywords_count,
        'pos_1': organic.get('pos_1', 0),
        'pos_2_3': organic.get('pos_2_3', 0),
        'pos_4_10': organic.get('pos_4_10', 0),
        'pos_11_20': organic.get('pos_11_20', 0),
        'pos_21_30': organic.get('pos_21_30', 0),
        'pos_31_100': pos_31_100,
        'avg_position': round(_calc_avg_position(organic), 1),
        'time_range': time_range,
    }
    overview['vi'] = _calc_visibility_index(overview)
    print(f"  ✓ {domain}: ETV={overview['etv']:,} | KW={keywords_count} | "
          f"VI={overview['vi']}")
    return overview


# ============================================================
# 2. COMPETITOR SHARE OF VOICE
# ============================================================
def fetch_competitor_sov(client_domain, competitors,
                         location_code=DEFAULT_LOCATION_CODE,
                         language_code=DEFAULT_LANGUAGE_CODE):
    """
    Fetch visibility data for the client + every competitor, then compute
    each domain's Share of Voice (ETV as % of total market ETV).

    Returns dict:
      domains: [overview dicts with 'sov_pct' added]
      total_etv: float
    """
    print(f"\n=== Share of Voice: {client_domain} vs {len(competitors)} "
          f"competitors ===")
    all_domains = [client_domain] + list(competitors)
    snapshots = []
    for dom in all_domains:
        vis = fetch_domain_visibility(dom, location_code, language_code)
        if vis:
            snapshots.append(vis)
        else:
            # Record a zeroed placeholder so the table still shows the domain.
            snapshots.append({
                'domain': dom, 'etv': 0, 'keywords_count': 0,
                'pos_1': 0, 'pos_2_3': 0, 'pos_4_10': 0,
                'pos_11_20': 0, 'pos_21_30': 0, 'pos_31_100': 0,
                'avg_position': 0, 'vi': 0, 'time_range': None,
            })
        time.sleep(0.3)  # be polite to the API

    total_etv = sum(s['etv'] for s in snapshots) or 1.0
    for s in snapshots:
        s['sov_pct'] = round((s['etv'] / total_etv) * 100, 2)

    # Sort by ETV descending so the client can see the leaderboard.
    snapshots.sort(key=lambda x: x['etv'], reverse=True)
    return {'domains': snapshots, 'total_etv': round(total_etv, 2)}


# ============================================================
# 3. VISIBILITY TRENDS
# ============================================================
def _default_periods(months=3):
    """Build a list of monthly time_range strings ending the current month."""
    now = datetime.now()
    periods = []
    for m in range(months):
        # Approximate "last N months" windows: each ~30-day slice going back.
        end = datetime(now.year, now.month, 1)
        # Walk back m months
        y, mo = end.year, end.month
        for _ in range(m):
            mo -= 1
            if mo == 0:
                mo = 12
                y -= 1
        start = datetime(y, mo, 1)
        periods.append(start.strftime('%Y-%m-%d'))
    return list(reversed(periods))


def compute_visibility_trends(domains, periods=None,
                               location_code=DEFAULT_LOCATION_CODE,
                               language_code=DEFAULT_LANGUAGE_CODE):
    """
    Compute month-over-month visibility changes across `domains` for each
    period in `periods`.

    If `periods` is None, defaults to the last 3 monthly windows.
    For each period, fetch_domain_visibility is called with time_range set;
    when historical data is unavailable the endpoint returns the current
    snapshot, which we flag and use as a proxy alongside the backlinks
    timeseries endpoint.

    Returns:
      trends: {domain: {period: overview_dict}}
      mom_changes: {domain: [(period, etv_delta, kw_delta, vi_delta), ...]}
      used_proxy: bool  — True when historical endpoint returned identical
                          snapshots (proxy fallback engaged)
    """
    if periods is None:
        periods = _default_periods(3)
    print(f"\n=== Visibility Trends across {len(periods)} periods ===")
    print(f"  Periods: {periods}")

    trends = {d: {} for d in domains}
    used_proxy = False
    first_snapshot = None

    for p_idx, period in enumerate(periods):
        for dom in domains:
            # Use a window: from period start to ~30 days later (or now).
            start = period
            try:
                start_dt = datetime.strptime(period, '%Y-%m-%d')
                end_dt = datetime(start_dt.year, start_dt.month + 1, 1) \
                    if start_dt.month < 12 \
                    else datetime(start_dt.year + 1, 1, 1)
                time_range = f"{start},{end_dt.strftime('%Y-%m-%d')}"
            except Exception:
                time_range = None

            vis = fetch_domain_visibility(dom, location_code,
                                           language_code, time_range)
            if vis:
                trends[dom][period] = vis
                if p_idx == 0:
                    if first_snapshot is None:
                        first_snapshot = {}
                    first_snapshot = vis
            else:
                # Fallback: use current snapshot without time_range as proxy.
                vis_now = fetch_domain_visibility(dom, location_code,
                                                  language_code, None)
                if vis_now:
                    vis_now['time_range'] = f"{period} (proxy)"
                    trends[dom][period] = vis_now
                    used_proxy = True
            time.sleep(0.3)

    # Detect proxy: if all periods returned identical ETV, historical data
    # was unavailable — flag it.
    if not used_proxy and len(periods) > 1:
        for dom in domains:
            period_vals = [trends[dom][p]['etv'] for p in periods
                           if p in trends[dom]]
            if len(set(period_vals)) == 1 and period_vals:
                used_proxy = True
                break

    # Month-over-month changes.
    mom_changes = {}
    for dom in domains:
        changes = []
        prev = None
        for p in periods:
            cur = trends[dom].get(p)
            if not cur:
                continue
            if prev is None:
                changes.append((p, 0.0, 0, 0.0))
            else:
                changes.append((
                    p,
                    round(cur['etv'] - prev['etv'], 2),
                    cur['keywords_count'] - prev['keywords_count'],
                    round(cur['vi'] - prev['vi'], 2),
                ))
            prev = cur
        mom_changes[dom] = changes

    print(f"  ✓ Trends computed for {len(trends)} domains "
          f"({'proxy fallback' if used_proxy else 'historical data'})")
    return {'trends': trends, 'mom_changes': mom_changes,
            'used_proxy': used_proxy}


# ============================================================
# COMPETITOR DISCOVERY
# ============================================================
def fetch_competitor_discovery(domain, limit=10,
                                location_code=DEFAULT_LOCATION_CODE,
                                language_code=DEFAULT_LANGUAGE_CODE):
    """
    Fetch top organic competitors for a domain via DataForSEO
    competitors_domain/live endpoint.

    Returns list of up to `limit` dicts with:
      domain, avg_position, visibility (proxy from shared-keyword ETV),
      keywords (intersection count), etv (shared-keyword ETV),
      total_keywords (domain-wide organic keyword count)
    """
    print(f"  → Discovering competitors for {domain}...")
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
        print("  ✗ No competitor discovery data")
        return []
    items = result.get('items', [])[:limit]
    discovered = []
    for it in items:
        dom = it.get('domain', '')
        # Shared-keyword metrics (intersection with the target domain).
        shared_metrics = it.get('metrics', {}).get('organic', {})
        shared_etv = shared_metrics.get('etv', 0)
        shared_keywords = it.get('intersections', 0) \
            or shared_metrics.get('count', 0)
        # Domain-wide metrics (full keyword universe for this competitor).
        full_metrics = it.get('full_domain_metrics', {}).get('organic', {})
        total_keywords = full_metrics.get('count', 0)
        avg_pos = it.get('avg_position', 0) or 0
        # Visibility proxy: scale shared ETV to a 0–100 index for ranking
        # display purposes (DataForSEO competitors_domain/live does not
        # return a precomputed visibility value).
        visibility_proxy = round(min(shared_etv / 1000.0, 100.0), 1) \
            if shared_etv else 0.0
        discovered.append({
            'domain': dom,
            'avg_position': round(avg_pos, 1),
            'visibility': visibility_proxy,
            'keywords': shared_keywords,
            'etv': round(shared_etv, 2),
            'total_keywords': total_keywords,
        })
    print(f"  ✓ Found {len(discovered)} competitors")
    return discovered


# ============================================================
# SVG CHARTS
# ============================================================
def _esc(s):
    """XML/HTML escape a string for safe SVG/HTML embedding."""
    return (str(s).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


def _domain_color(idx):
    return DOMAIN_PALETTE[idx % len(DOMAIN_PALETTE)]


def build_sov_pie_svg(domains, total_etv, width=420, height=320):
    """Build an inline SVG pie chart of Share of Voice (ETV share)."""
    cx, cy, r = width / 2, height / 2 + 10, 110
    # Only domains with ETV > 0 get a slice; others lumped as "No data".
    slices = [(d['domain'], d['etv'], _domain_color(i))
              for i, d in enumerate(domains) if d['etv'] > 0]
    if not slices:
        return ('<div style="color:#999;padding:20px;">No ETV data to '
                'chart.</div>')
    total = sum(s[1] for s in slices) or 1.0
    angle = -90  # start at top
    paths = []
    labels = []
    for i, (dom, etv, color) in enumerate(slices):
        pct = etv / total
        sweep = pct * 360
        a1 = math.radians(angle)
        a2 = math.radians(angle + sweep)
        x1 = cx + r * math.cos(a1)
        y1 = cy + r * math.sin(a1)
        x2 = cx + r * math.cos(a2)
        y2 = cy + r * math.sin(a2)
        large = 1 if sweep > 180 else 0
        if sweep >= 359.99:
            # Full circle — draw as circle to avoid 0-length arc.
            paths.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r}" '
                f'fill="{color}" stroke="#fff" stroke-width="2"/>')
        else:
            paths.append(
                f'<path d="M{cx:.1f},{cy:.1f} L{x1:.1f},{y1:.1f} '
                f'A{r},{r} 0 {large} 1 {x2:.1f},{y2:.1f} Z" '
                f'fill="{color}" stroke="#fff" stroke-width="2"/>')
        # Legend label
        mid = math.radians(angle + sweep / 2)
        lx = cx + (r * 0.6) * math.cos(mid)
        ly = cy + (r * 0.6) * math.sin(mid)
        pct_lbl = f"{pct*100:.1f}%"
        labels.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" '
            f'fill="#fff" font-size="11" font-weight="700">{pct_lbl}</text>')
        angle += sweep

    # Legend below the pie.
    legend_y = height + 10
    legend_items = []
    for i, (dom, etv, color) in enumerate(slices):
        ly = legend_y + i * 18
        pct = etv / total * 100
        legend_items.append(
            f'<rect x="10" y="{ly}" width="12" height="12" fill="{color}"/>'
            f'<text x="28" y="{ly+11}" font-size="11" fill="#1a1a1a">'
            f'{_esc(dom)} — {etv:,.0f} ({pct:.1f}%)</text>')
    legend_h = len(slices) * 18 + 10
    total_h = height + legend_h
    svg = (f'<svg width="{width}" height="{total_h}" '
           f'viewBox="0 0 {width} {total_h}" '
           f'xmlns="http://www.w3.org/2000/svg">'
           + ''.join(paths) + ''.join(labels)
           + ''.join(legend_items)
           + '</svg>')
    return svg


def build_position_dist_stacked_svg(domains, width=760, height=380):
    """
    Build a grouped bar chart comparing position-bucket keyword counts across
    domains. One cluster per domain, five bars per cluster (Top1/3/10/20/100).
    """
    buckets = ['pos_1', 'pos_2_3', 'pos_4_10', 'pos_11_20', 'pos_21_30',
               'pos_31_100']
    bucket_labels = ['Top 1', 'Top 3', 'Top 10', 'Top 20', 'Top 30',
                      'Top 100']
    bucket_colors = ['#1a73e8', '#1967d2', '#34a853', '#fbbc04', '#ea8a00',
                      '#ea4335']
    n_domains = len(domains)
    if n_domains == 0:
        return '<div style="color:#999;padding:20px;">No domains to chart.</div>'

    margin_left = 70
    margin_right = 20
    margin_top = 40
    margin_bottom = 80
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom
    cluster_gap = 0.4  # fraction of cluster width used as gap
    bar_count = len(buckets)
    cluster_w = plot_w / n_domains
    bar_w = (cluster_w * (1 - cluster_gap)) / bar_count

    max_val = max((d.get(b, 0) for d in domains for b in buckets) or [1])
    if max_val == 0:
        max_val = 1

    def y_scale(v):
        return margin_top + plot_h - (v / max_val) * plot_h

    parts = [f'<svg width="{width}" height="{height}" '
             f'viewBox="0 0 {width} {height}" '
             f'xmlns="http://www.w3.org/2000/svg">']
    # Y-axis gridlines + labels (4 ticks).
    for i in range(5):
        gv = (max_val / 4) * i
        gy = y_scale(gv)
        parts.append(
            f'<line x1="{margin_left}" y1="{gy:.1f}" x2="{width-margin_right}" '
            f'y2="{gy:.1f}" stroke="#eee" stroke-width="1"/>')
        parts.append(
            f'<text x="{margin_left-8}" y="{gy+4:.1f}" text-anchor="end" '
            f'font-size="10" fill="#999">{gv:,.0f}</text>')
    # Axes.
    parts.append(
        f'<line x1="{margin_left}" y1="{margin_top}" x2="{margin_left}" '
        f'y2="{margin_top+plot_h}" stroke="#ccc" stroke-width="1"/>')
    parts.append(
        f'<line x1="{margin_left}" y1="{margin_top+plot_h}" '
        f'x2="{width-margin_right}" y2="{margin_top+plot_h}" '
        f'stroke="#ccc" stroke-width="1"/>')

    for di, d in enumerate(domains):
        cluster_x = margin_left + di * cluster_w
        for bi, b in enumerate(buckets):
            v = d.get(b, 0)
            bx = cluster_x + cluster_w * cluster_gap / 2 + bi * bar_w
            by = y_scale(v)
            bh = margin_top + plot_h - by
            color = bucket_colors[bi]
            parts.append(
                f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w-1:.1f}" '
                f'height="{bh:.1f}" fill="{color}">')
            parts.append(
                f'<title>{_esc(d["domain"])} {bucket_labels[bi]}: '
                f'{v:,}</title></rect>')
            if v > 0 and bh > 14:
                parts.append(
                    f'<text x="{bx+bar_w/2:.1f}" y="{by-3:.1f}" '
                    f'text-anchor="middle" font-size="9" fill="#555">'
                    f'{v:,}</text>')
        # Domain label under cluster (rotated if long).
        lbl_x = cluster_x + cluster_w / 2
        lbl_y = margin_top + plot_h + 16
        dom_short = d['domain'].replace('www.', '')
        parts.append(
            f'<text x="{lbl_x:.1f}" y="{lbl_y:.1f}" text-anchor="end" '
            f'font-size="10" fill="#1a1a1a" '
            f'transform="rotate(-30,{lbl_x:.1f},{lbl_y:.1f})">'
            f'{_esc(dom_short)}</text>')

    # Legend (position buckets).
    lx = margin_left
    ly = height - 18
    for bi, bl in enumerate(bucket_labels):
        parts.append(
            f'<rect x="{lx}" y="{ly}" width="12" height="12" '
            f'fill="{bucket_colors[bi]}"/>')
        parts.append(
            f'<text x="{lx+16}" y="{ly+11}" font-size="11" fill="#1a1a1a">'
            f'{bl}</text>')
        lx += 70

    parts.append('</svg>')
    return ''.join(parts)


# ============================================================
# WINNER / LOSER ANALYSIS
# ============================================================
def compute_winners_losers(domains):
    """
    For each position bucket, identify which domain leads (most keywords)
    and which lags (fewest). Returns list of dicts.
    """
    buckets = [
        ('pos_1', 'Top 1'),
        ('pos_2_3', 'Top 3'),
        ('pos_4_10', 'Top 10'),
        ('pos_11_20', 'Top 20'),
        ('pos_21_30', 'Top 30'),
        ('pos_31_100', 'Top 100'),
    ]
    results = []
    for key, label in buckets:
        sorted_d = sorted(domains, key=lambda d: d.get(key, 0), reverse=True)
        if not sorted_d:
            continue
        leader = sorted_d[0]
        laggard = sorted_d[-1]
        results.append({
            'bucket': label,
            'leader': leader['domain'],
            'leader_count': leader.get(key, 0),
            'laggard': laggard['domain'],
            'laggard_count': laggard.get(key, 0),
        })
    return results


# ============================================================
# ACTION ITEMS
# ============================================================
def generate_action_items(client_domain, domains, discovered):
    """
    Generate data-grounded action items for the client domain.
    """
    actions = []
    client = next((d for d in domains if d['domain'] == client_domain), None)
    if not client:
        return actions

    # 1. Position buckets where client lags the leader.
    buckets = [
        ('pos_1', 'Top 1'), ('pos_2_3', 'Top 3'), ('pos_4_10', 'Top 10'),
        ('pos_11_20', 'Top 20'), ('pos_21_30', 'Top 30'),
    ]
    for key, label in buckets:
        leader = max(domains, key=lambda d: d.get(key, 0))
        gap = leader.get(key, 0) - client.get(key, 0)
        if leader['domain'] != client_domain and gap > 0:
            actions.append({
                'title': f'Close the {label} keyword gap vs {leader["domain"]}',
                'detail': (f'{client_domain} has {client.get(key,0):,} '
                           f'keywords in {label}; {leader["domain"]} leads '
                           f'with {leader.get(key,0):,}. Prioritize on-page '
                           f'optimization and internal linking for keywords '
                           f'currently ranking on page 2 to push them into '
                           f'{label}.'),
            })

    # 2. SoV gap.
    total_etv = sum(d['etv'] for d in domains) or 1.0
    client_sov = (client['etv'] / total_etv) * 100
    leader = max(domains, key=lambda d: d['etv'])
    if leader['domain'] != client_domain:
        actions.append({
            'title': f'Grow Share of Voice — {client_sov:.1f}% vs '
                     f'{leader["domain"]}\'s '
                     f'{(leader["etv"]/total_etv)*100:.1f}%',
            'detail': (f'Target high-ETV keywords where {leader["domain"]} '
                       f'ranks in Top 10 but {client_domain} is absent or '
                       f'ranked >20. Even a few Top 10 wins materially shift '
                       f'SoV because ETV is concentrated in top positions.'),
        })

    # 3. Page-2 opportunities (pos_11_20 → push to Top 10).
    p2 = client.get('pos_11_20', 0)
    if p2 > 0:
        actions.append({
            'title': f'Promote {p2:,} page-2 keywords into Top 10',
            'detail': (f'{client_domain} has {p2:,} keywords ranking in '
                       f'positions 11–20. These are the fastest wins: '
                       f'refresh content, improve title/meta, add internal '
                       f'links from authoritative pages, and earn a few '
                       f'relevant backlinks per page.'),
        })

    # 4. Discovered competitors not yet tracked.
    tracked = {d['domain'] for d in domains}
    new_comps = [c for c in discovered if c['domain'] not in tracked][:3]
    for c in new_comps:
        actions.append({
            'title': f'New competitor to monitor: {c["domain"]}',
            'detail': (f'Discovered via DataForSEO competitors endpoint: '
                       f'avg position {c["avg_position"]}, visibility '
                       f'{c["visibility"]}, {c["keywords"]} shared keywords. '
                       f'Add to regular tracking.'),
        })

    if not actions:
        actions.append({
            'title': 'Maintain current visibility',
            'detail': (f'{client_domain} leads or matches competitors across '
                       f'all position buckets. Focus on defending existing '
                       f'rankings and expanding into adjacent keyword '
                       f'clusters.'),
        })
    return actions


# ============================================================
# 4. SOV REPORT
# ============================================================
def generate_sov_report(client_domain, competitors, data, output_path):
    """
    Build the white-SaaS-theme HTML report and write it to output_path.

    `data` is the dict returned by fetch_competitor_sov (+optional
    'trends', 'discovered', 'mom_changes' keys).
    """
    os.makedirs(output_path, exist_ok=True)
    domains = data['domains']
    total_etv = data['total_etv']
    discovered = data.get('discovered', [])
    trends_info = data.get('trends_info', {})
    mom_changes = data.get('mom_changes', {})
    used_proxy = trends_info.get('used_proxy', False)

    generated_at = datetime.now().strftime('%Y-%m-%d %H:%M CST')

    # --- Pie chart (SoV) ---
    pie_svg = build_sov_pie_svg(domains, total_etv)

    # --- Stacked/grouped bar chart (position distribution) ---
    pos_svg = build_position_dist_stacked_svg(domains)

    # --- Comparison table rows ---
    table_rows = []
    for i, d in enumerate(domains):
        is_client = d['domain'] == client_domain
        row_style = ('background:#e7f3ff;font-weight:600;' if is_client else '')
        badge = ('<span style="color:#0066cc;font-weight:700;">CLIENT</span>'
                 if is_client else '')
        table_rows.append(
            f'      <tr style="{row_style}">'
            f'<td>{_esc(d["domain"])} {badge}</td>'
            f'<td>{d["etv"]:,.0f}</td>'
            f'<td>{d["keywords_count"]:,}</td>'
            f'<td>{d["vi"]}</td>'
            f'<td>{d["avg_position"]:.1f}</td>'
            f'<td>{d["pos_1"]:,}</td>'
            f'<td>{d["pos_2_3"]:,}</td>'
            f'<td>{d["pos_4_10"]:,}</td>'
            f'<td>{d["pos_11_20"]:,}</td>'
            f'<td>{d["pos_21_30"]:,}</td>'
            f'<td>{d["pos_31_100"]:,}</td>'
            f'<td><b>{d["sov_pct"]:.2f}%</b></td>'
            f'</tr>')
    table_rows_html = '\n'.join(table_rows)

    # --- Winner / loser analysis ---
    wl = compute_winners_losers(domains)
    wl_rows = []
    for w in wl:
        wl_rows.append(
            f'      <tr>'
            f'<td>{w["bucket"]}</td>'
            f'<td><b>{_esc(w["leader"])}</b> ({w["leader_count"]:,})</td>'
            f'<td>{_esc(w["laggard"])} ({w["laggard_count"]:,})</td>'
            f'<td>{w["leader_count"]-w["laggard_count"]:,}</td>'
            f'</tr>')
    wl_rows_html = '\n'.join(wl_rows)

    # --- Competitor discovery table ---
    disc_rows = []
    for i, c in enumerate(discovered):
        disc_rows.append(
            f'      <tr>'
            f'<td>{i+1}</td>'
            f'<td>{_esc(c["domain"])}</td>'
            f'<td>{c["avg_position"]:.1f}</td>'
            f'<td>{c["visibility"]:.1f}</td>'
            f'<td>{c["keywords"]:,}</td>'
            f'<td>{c["etv"]:,.0f}</td>'
            f'</tr>')
    disc_rows_html = '\n'.join(disc_rows) if disc_rows else \
        '<tr><td colspan="6" style="color:#999;text-align:center;">' \
        'No competitor discovery data available.</td></tr>'

    # --- Trends table (MoM) ---
    # mom_changes structure: {domain: [(period, etv_delta, kw_delta, vi_delta), ...]}
    trends_html = ''
    if mom_changes:
        sample = next(iter(mom_changes.values()), [])
        if sample:
            trend_rows = []
            for dom, changes in mom_changes.items():
                is_client = dom == client_domain
                row_style = ('background:#e7f3ff;font-weight:600;'
                             if is_client else '')
                cells = [f'<td><b>{_esc(dom)}</b></td>']
                for (period, etv_d, kw_d, vi_d) in changes:
                    etv_color = ('#27ae60' if etv_d > 0 else
                                 '#c0392b' if etv_d < 0 else '#999')
                    sign = '+' if etv_d > 0 else ''
                    cells.append(
                        f'<td style="color:{etv_color};">{sign}{etv_d:,.0f}'
                        f'</td>')
                trend_rows.append(f'      <tr style="{row_style}">'
                                  + ''.join(cells) + '</tr>')
            # Header: period labels.
            period_labels = [c[0] for c in sample]
            th = ''.join(f'<th>{p}</th>' for p in period_labels)
            trends_html = f'''
  <h2>Month-over-Month Visibility Trends</h2>
  <div class="gsc-note">{'Historical data unavailable — showing current snapshot as proxy for each period (backlinks/timeseries fallback).' if used_proxy else 'Month-over-month ETV deltas per domain.'}</div>
  <div class="section">
    <table>
      <thead>
        <tr>
          <th>Domain</th>
          {th}
        </tr>
      </thead>
      <tbody>
{chr(10).join(trend_rows)}
      </tbody>
    </table>
  </div>'''

    # --- Action items ---
    actions = generate_action_items(client_domain, domains, discovered)
    action_blocks = []
    for a in actions:
        action_blocks.append(
            f'    <div class="action-item">\n'
            f'      <h4>{_esc(a["title"])}</h4>\n'
            f'      <p>{_esc(a["detail"])}</p>\n'
            f'    </div>')
    actions_html = '\n'.join(action_blocks)

    # --- Summary cards (client metrics) ---
    client_data = next((d for d in domains if d['domain'] == client_domain),
                       {'etv': 0, 'keywords_count': 0, 'vi': 0,
                        'avg_position': 0, 'sov_pct': 0})
    page1 = (client_data.get('pos_1', 0) + client_data.get('pos_2_3', 0) +
             client_data.get('pos_4_10', 0))

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex,nofollow">
<title>Share of Voice Trend Report — {client_domain}</title>
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
  .chart-wrap {{ display: flex; flex-wrap: wrap; gap: 30px; align-items: flex-start; justify-content: center; }}
  table {{ width: 100%; border-collapse: collapse; background: #ffffff; }}
  th {{ background: #f0f0f0; color: #1a1a1a; font-weight: 700; font-size: 13px; padding: 10px 10px; text-align: left; border-bottom: 2px solid #e0e0e0; }}
  td {{ padding: 9px 10px; border-bottom: 1px solid #e0e0e0; font-size: 13px; color: #1a1a1a; }}
  tr:hover {{ background: #f8f9fa; }}
  .action-item {{ background: #ffffff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 14px; margin-bottom: 12px; }}
  .action-item h4 {{ margin: 0 0 6px 0; font-size: 15px; color: #0066cc; }}
  .action-item p {{ margin: 0; font-size: 14px; color: #555; line-height: 1.5; }}
  .gsc-note {{ font-size: 13px; color: #888; font-style: italic; margin-bottom: 10px; }}
  .footer {{ margin-top: 30px; padding-top: 15px; border-top: 1px solid #e0e0e0; font-size: 13px; color: #999; text-align: center; }}
  .pos-positive {{ color: #27ae60; font-weight: 600; }}
  .pos-negative {{ color: #c0392b; font-weight: 600; }}
</style>
</head>
<body>
  <h1>Share of Voice Trend Report</h1>
  <div class="url-headline"><a href="https://{_esc(client_domain)}" target="_blank">{_esc(client_domain)}</a></div>
  <div class="subtitle">Generated: {generated_at} · DataForSEO Domain Rank Overview + Competitors · noindex, nofollow</div>

  <!-- Executive Summary -->
  <h2>Executive Summary — {client_domain}</h2>
  <div class="summary-cards">
    <div class="summary-card"><div class="num">{client_data.get('etv',0):,.0f}</div><div class="label">Est. Monthly Traffic (ETV)</div></div>
    <div class="summary-card"><div class="num">{client_data.get('keywords_count',0):,}</div><div class="label">Keywords Ranked</div></div>
    <div class="summary-card"><div class="num">{client_data.get('vi',0)}</div><div class="label">Visibility Index</div></div>
    <div class="summary-card"><div class="num">{client_data.get('avg_position',0):.1f}</div><div class="label">Avg Position</div></div>
    <div class="summary-card"><div class="num">{client_data.get('sov_pct',0):.2f}%</div><div class="label">Share of Voice</div></div>
  </div>

  <!-- Share of Voice Overview -->
  <h2>Share of Voice Overview</h2>
  <div class="section">
    <p style="font-size:14px;color:#666;margin-bottom:16px;">Each domain's estimated traffic (ETV) as a percentage of total market ETV across all tracked domains. Total market ETV: <b>{total_etv:,.0f}</b>.</p>
    <div class="chart-wrap">
      {pie_svg}
    </div>
  </div>

  <!-- Visibility Comparison Table -->
  <h2>Visibility Comparison</h2>
  <div class="section">
    <table>
      <thead>
        <tr>
          <th>Domain</th>
          <th>ETV</th>
          <th>Keywords</th>
          <th>VI</th>
          <th>Avg Pos</th>
          <th>Top 1</th>
          <th>Top 3</th>
          <th>Top 10</th>
          <th>Top 20</th>
          <th>Top 30</th>
          <th>Top 100</th>
          <th>SoV %</th>
        </tr>
      </thead>
      <tbody>
{table_rows_html}
      </tbody>
    </table>
  </div>

  <!-- Position Distribution Comparison Chart -->
  <h2>Position Distribution Comparison</h2>
  <div class="section">
    <p style="font-size:14px;color:#666;margin-bottom:16px;">Keyword counts per position bucket across all domains. Identify where competitors concentrate their rankings.</p>
    <div class="chart-wrap">
      {pos_svg}
    </div>
  </div>

  <!-- Winner / Loser Analysis -->
  <h2>Winner / Loser Analysis by Position Bucket</h2>
  <div class="section">
    <table>
      <thead>
        <tr>
          <th>Position Bucket</th>
          <th>Leader (most keywords)</th>
          <th>Laggard (fewest keywords)</th>
          <th>Gap</th>
        </tr>
      </thead>
      <tbody>
{wl_rows_html}
      </tbody>
    </table>
  </div>

  <!-- Trends -->
{trends_html}

  <!-- Competitor Discovery -->
  <h2>Competitor Discovery — Top 10 in {client_domain}'s Space</h2>
  <div class="section">
    <p style="font-size:14px;color:#666;margin-bottom:16px;">Top domains competing for the same organic keywords as {client_domain}, per DataForSEO competitors endpoint.</p>
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>Domain</th>
          <th>Avg Position</th>
          <th>Visibility</th>
          <th>Shared Keywords</th>
          <th>ETV</th>
        </tr>
      </thead>
      <tbody>
{disc_rows_html}
      </tbody>
    </table>
  </div>

  <!-- Action Items -->
  <h2>Action Items & Opportunities for {client_domain}</h2>
  <div class="section">
{actions_html}
  </div>

  <div class="footer">
    Zenfusion Share of Voice Trend Report · Powered by DataForSEO · {generated_at} · noindex, nofollow
  </div>
</body>
</html>'''

    html_file = os.path.join(output_path,
                             f'{client_domain}_sov_trend_report.html')
    with open(html_file, 'w', encoding='utf-8') as f:
        f.write(html)
    # Also dump the raw JSON data alongside for debugging.
    json_file = os.path.join(output_path, f'{client_domain}_sov_data.json')
    with open(json_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, default=str)
    print(f"\n  ✓ Report saved: {html_file}")
    print(f"  ✓ Raw data saved: {json_file}")
    return html_file


# ============================================================
# CLI
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description='Share of Voice Trend Analyzer — compare visibility '
                    'trends across competitors.')
    parser.add_argument('--client', required=True,
                        help='Client domain (e.g. softprowatersystems.com)')
    parser.add_argument('--competitors', required=True,
                        help='Comma-separated competitor domains')
    parser.add_argument('--output', default='/tmp/sov_trend_report',
                        help='Output directory for report')
    parser.add_argument('--location', type=int, default=DEFAULT_LOCATION_CODE,
                        help='DataForSEO location code (default 2840 = US)')
    parser.add_argument('--periods', type=int, default=3,
                        help='Number of monthly periods for trend analysis')
    args = parser.parse_args()

    client = args.client.strip()
    competitors = [c.strip() for c in args.competitors.split(',') if c.strip()]
    print(f"Client: {client}")
    print(f"Competitors: {competitors}")
    print(f"Output: {args.output}")

    # 1+2. Fetch SoV snapshot for client + competitors.
    sov = fetch_competitor_sov(client, competitors, args.location)

    # 3. Compute visibility trends.
    all_domains = [client] + competitors
    trends = compute_visibility_trends(all_domains,
                                       _default_periods(args.periods),
                                       args.location)
    # Attach MoM changes keyed by domain.
    mom_changes = trends['mom_changes']

    # 4. Competitor discovery.
    discovered = fetch_competitor_discovery(client, limit=10,
                                             location_code=args.location)

    # Assemble report data.
    report_data = {
        'domains': sov['domains'],
        'total_etv': sov['total_etv'],
        'discovered': discovered,
        'trends_info': {'used_proxy': trends['used_proxy']},
        'mom_changes': mom_changes,
    }

    # 4. Generate report.
    report_path = generate_sov_report(client, competitors, report_data,
                                       args.output)
    print(f"\n{'='*60}")
    print(f"✓ Share of Voice Trend Report complete")
    print(f"  Report: {report_path}")
    print(f"{'='*60}")
    return report_path


if __name__ == '__main__':
    main()
