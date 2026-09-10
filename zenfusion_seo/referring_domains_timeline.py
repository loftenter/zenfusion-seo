#!/usr/bin/env python3
"""
Zenfusion Referring Domains Timeline Analyzer

Shows when each referring domain first linked to a target domain, visualized
as a monthly acquisition timeline.  Helps SEOs understand link-building
history, identify growth phases, and spot sustained vs. bursty acquisition.

Functions:
  1. fetch_referring_domains_history(domain, limit=1000)
       — DataForSEO backlinks/backlinks/live ordered by first_seen,
         returns backlinks enriched with first_seen dates.
  2. build_timeline(backlinks)
       — group referring domains by month first_seen, compute monthly
         acquisition rate and cumulative totals.
  3. generate_timeline_report(domain, timeline, backlinks, output_path)
       — HTML report: overview cards, SVG timeline chart, monthly
         breakdown table, expandable top referring domains table,
         link-building phase identification, and recommendations.

Usage:
  python3 referring_domains_timeline.py \
      --domain softprowatersystems.com \
      --output /tmp/test_referring_timeline

DataForSEO endpoints used:
  POST /v3/backlinks/backlinks/live   — individual backlink details with first_seen

Credentials: ~/.hermes/.env  (DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD)
"""

import os, sys, json, base64, urllib.request, argparse, time, html
from datetime import datetime, timedelta
from collections import OrderedDict, defaultdict
from urllib.parse import urlparse

ENV_PATH = os.path.expanduser('~/.hermes/.env')

# ── Credentials & API helpers ───────────────────────────────────────────────

def get_creds():
    """Load DataForSEO credentials from environment or ~/.hermes/.env."""
    login = os.environ.get('DATAFORSEO_LOGIN')
    password = os.environ.get('DATAFORSEO_PASSWORD')
    if not login or not password:
        if os.path.exists(ENV_PATH):
            with open(ENV_PATH) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('DATAFORSEO_LOGIN='):
                        login = line.split('=', 1)[1]
                    elif line.startswith('DATAFORSEO_PASSWORD='):
                        password = line.split('=', 1)[1]
    if not login or not password:
        print("ERROR: DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD not found")
        print("Set them in ~/.hermes/.env or as environment variables")
        sys.exit(1)
    return login, password


def dfs_api_call(endpoint, payload, timeout=120):
    """POST to a DataForSEO v3 live endpoint.
    payload is a single task dict; it will be wrapped in an array."""
    login, password = get_creds()
    creds = base64.b64encode(f'{login}:{password}'.encode()).decode()
    url = f'https://api.dataforseo.com/v3/{endpoint}'
    headers = {
        'Authorization': f'Basic {creds}',
        'Content-Type': 'application/json',
    }
    body = json.dumps([payload]).encode()
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        if data.get('status_code') != 20000:
            err = data.get('status_message', 'Unknown error')
            print(f"  API error ({data.get('status_code')}): {err}")
            return None
        return data
    except Exception as e:
        print(f"  Request error calling {endpoint}: {e}")
        return None


def _extract_items(data):
    """Safely extract items list from a DataForSEO response."""
    if not data or not data.get('tasks'):
        return []
    task = data['tasks'][0]
    if task.get('status_code') != 20000:
        print(f"  Task error: {task.get('status_message', 'Unknown')}")
        return []
    result = task.get('result', [{}])[0]
    return result.get('items', []) or []


def normalize_domain(d):
    """Strip www. prefix and protocol."""
    if not d:
        return ''
    d = d.lower().strip()
    if d.startswith('http://') or d.startswith('https://'):
        d = urlparse(d).netloc
    if d.startswith('www.'):
        d = d[4:]
    return d


# ── 1. fetch_referring_domains_history ──────────────────────────────────────

def fetch_referring_domains_history(domain, limit=1000):
    """Call DataForSEO backlinks/backlinks/live with order_by first_seen.

    Returns a list of backlink dicts, each containing:
      domain, source_url, anchor, first_seen, domain_strength,
      is_dofollow, target_url
    """
    domain = normalize_domain(domain)
    print(f"  -> Fetching backlinks history for {domain} (limit {limit}, ordered by first_seen)...")

    payload = {
        'target': domain,
        'mode': 'as_is',
        'limit': limit,
        'include_subdomains': True,
    }
    data = dfs_api_call('backlinks/backlinks/live', payload)
    items = _extract_items(data)

    backlinks = []
    for item in items:
        links_type = item.get('type', '')
        first_seen_raw = item.get('first_seen', '') or ''
        first_seen = first_seen_raw[:10] if first_seen_raw else ''
        backlinks.append({
            'domain': normalize_domain(item.get('domain_from', '')),
            'source_url': item.get('url_from', ''),
            'anchor': item.get('anchor', ''),
            'first_seen': first_seen,
            'first_seen_raw': first_seen_raw,
            'domain_strength': item.get('domain_from_rank', 0) or 0,
            'is_dofollow': links_type == 'dofollow',
            'target_url': item.get('url_to', ''),
        })

    # Sort by first_seen ascending (in case the API doesn't honour order_by)
    backlinks.sort(key=lambda x: x.get('first_seen', '9999-99-99') or '9999-99-99')

    print(f"  v {len(backlinks)} backlinks retrieved for {domain}")
    return backlinks


# ── 2. build_timeline ───────────────────────────────────────────────────────

def build_timeline(backlinks):
    """Group referring domains by month of first_seen and compute acquisition rate.

    Returns dict:
      {
        'months': OrderedDict[ 'YYYY-MM': {'month': str, 'new_domains': int,
                          'cumulative': int, 'domains': [domain,...],
                          'notable': [backlink,...]} ],
        'earliest_date': str,
        'latest_date': str,
        'total_referring_domains': int,
        'total_months': int,
        'avg_domains_per_month': float,
        'peak_month': {'month': str, 'new_domains': int},
        'phases': [ {'name', 'start_month', 'end_month', 'type', 'avg_per_month'} ],
      }
    """
    # Deduplicate by referring domain — keep earliest first_seen
    domain_first_seen = {}   # domain -> first_seen date string
    domain_info = {}         # domain -> backlink dict (earliest one)

    for bl in backlinks:
        d = bl.get('domain', '')
        fs = bl.get('first_seen', '')
        if not d or not fs:
            continue
        if d not in domain_first_seen or fs < domain_first_seen[d]:
            domain_first_seen[d] = fs
            domain_info[d] = bl

    if not domain_first_seen:
        return _empty_timeline()

    # Group by month
    month_domains = defaultdict(list)   # 'YYYY-MM' -> [domain,...]
    month_notable = defaultdict(list)   # 'YYYY-MM' -> [backlink,...]

    for domain, fs in sorted(domain_first_seen.items(), key=lambda x: x[1]):
        month_key = fs[:7]  # 'YYYY-MM'
        month_domains[month_key].append(domain)
        month_notable[month_key].append(domain_info[domain])

    # Build ordered month list with cumulative
    months = OrderedDict()
    cumulative = 0
    for month_key in sorted(month_domains.keys()):
        new_count = len(month_domains[month_key])
        cumulative += new_count
        # Notable domains: top 5 by domain_strength in this month
        notable = sorted(month_notable[month_key],
                         key=lambda x: x.get('domain_strength', 0), reverse=True)[:5]
        months[month_key] = {
            'month': month_key,
            'new_domains': new_count,
            'cumulative': cumulative,
            'domains': month_domains[month_key],
            'notable': notable,
        }

    # Overview stats
    earliest_date = min(domain_first_seen.values())
    latest_date = max(domain_first_seen.values())
    total_domains = len(domain_first_seen)
    total_months = len(months)
    avg_per_month = round(total_domains / total_months, 2) if total_months else 0

    # Peak month
    peak_month_key = max(months.values(), key=lambda m: m['new_domains'])['month']
    peak_count = months[peak_month_key]['new_domains']

    # Link building phases
    phases = _identify_phases(months)

    return {
        'months': months,
        'earliest_date': earliest_date,
        'latest_date': latest_date,
        'total_referring_domains': total_domains,
        'total_months': total_months,
        'avg_domains_per_month': avg_per_month,
        'peak_month': {'month': peak_month_key, 'new_domains': peak_count},
        'phases': phases,
    }


def _identify_phases(months):
    """Identify growth phases (burst, steady, decline) from monthly data.

    A phase is a consecutive run of months with similar acquisition rates.
    'burst'  = rate > 1.5× average
    'steady'  = rate within 0.5×–1.5× average
    'decline' = rate < 0.5× average
    """
    month_list = list(months.values())
    if not month_list:
        return []

    avg = sum(m['new_domains'] for m in month_list) / len(month_list)
    if avg == 0:
        avg = 1  # avoid div by zero

    phases = []
    current_type = None
    phase_start = None
    phase_end = None
    phase_count = 0
    phase_total = 0

    def classify(count):
        if count > avg * 1.5:
            return 'burst'
        elif count < avg * 0.5:
            return 'decline'
        else:
            return 'steady'

    for m in month_list:
        mtype = classify(m['new_domains'])
        if current_type is None:
            current_type = mtype
            phase_start = m['month']
            phase_end = m['month']
            phase_count = 1
            phase_total = m['new_domains']
        elif mtype == current_type:
            phase_end = m['month']
            phase_count += 1
            phase_total += m['new_domains']
        else:
            phases.append({
                'name': f"{current_type.capitalize()} ({phase_start} → {phase_end})",
                'start_month': phase_start,
                'end_month': phase_end,
                'type': current_type,
                'months_in_phase': phase_count,
                'avg_per_month': round(phase_total / phase_count, 2),
                'total_domains': phase_total,
            })
            current_type = mtype
            phase_start = m['month']
            phase_end = m['month']
            phase_count = 1
            phase_total = m['new_domains']

    # Don't forget the last phase
    if current_type is not None:
        phases.append({
            'name': f"{current_type.capitalize()} ({phase_start} → {phase_end})",
            'start_month': phase_start,
            'end_month': phase_end,
            'type': current_type,
            'months_in_phase': phase_count,
            'avg_per_month': round(phase_total / phase_count, 2),
            'total_domains': phase_total,
        })

    return phases


def _empty_timeline():
    return {
        'months': OrderedDict(),
        'earliest_date': '',
        'latest_date': '',
        'total_referring_domains': 0,
        'total_months': 0,
        'avg_domains_per_month': 0,
        'peak_month': {'month': '', 'new_domains': 0},
        'phases': [],
    }


# ── 3. generate_timeline_report ────────────────────────────────────────────

def generate_timeline_report(domain, timeline, backlinks, output_path):
    """Generate an HTML report with charts, tables, and recommendations.

    output_path: directory to write the report into (will be created).
    Returns the path to the generated HTML file.
    """
    domain = normalize_domain(domain)
    os.makedirs(output_path, exist_ok=True)
    report_file = os.path.join(output_path, 'referring_domains_timeline.html')

    months = timeline.get('months', {})
    phases = timeline.get('phases', [])
    total_rd = timeline.get('total_referring_domains', 0)
    earliest = timeline.get('earliest_date', '—')
    latest = timeline.get('latest_date', '—')
    avg_pm = timeline.get('avg_domains_per_month', 0)
    peak = timeline.get('peak_month', {})

    # Build SVG timeline chart
    svg_chart = _build_svg_timeline_chart(months)

    # Summary cards
    overview_cards = _build_overview_cards(domain, timeline)

    # Monthly breakdown table
    monthly_table_rows = _build_monthly_table(months)

    # Top referring domains table (expandable)
    top_domains_rows = _build_top_domains_table(backlinks)

    # Link building phases
    phases_html = _build_phases_html(phases)

    # Recommendations
    recommendations = _build_recommendations(domain, timeline, phases)

    generated_at = datetime.now().strftime('%Y-%m-%d %H:%M')

    report_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<title>Referring Domains Timeline — {html.escape(domain)}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f1117; color: #fff; line-height: 1.6; padding: 20px 40px; max-width: 1200px; margin: 0 auto; }}
a {{ color: #4A90D9; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
h1 {{ font-size: 28px; margin-bottom: 4px; }}
h2 {{ color: #4A90D9; font-size: 20px; margin: 36px 0 16px; border-bottom: 1px solid #2a3142; padding-bottom: 8px; }}
h3 {{ color: #b0b8c8; font-size: 16px; margin: 20px 0 10px; }}
.subtitle {{ color: #8892a6; font-size: 14px; margin-bottom: 8px; }}
.generated {{ color: #5c6578; font-size: 12px; margin-bottom: 30px; }}

/* Summary cards */
.cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 16px; margin-bottom: 36px; }}
.card {{ background: #1a1d28; border: 1px solid #2a2e3c; border-radius: 10px; padding: 20px; }}
.card-label {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; color: #8892a6; margin-bottom: 8px; }}
.card-value {{ font-size: 28px; font-weight: 700; color: #fff; }}
.card-sub {{ font-size: 13px; color: #5c6578; margin-top: 4px; }}
.card-value.pos {{ color: #27ae60; }}
.card-value.neg {{ color: #e74c3c; }}

/* SVG chart */
.chart-container {{ background: #1a1d28; border: 1px solid #2a2e3c; border-radius: 10px; padding: 20px; margin-bottom: 36px; overflow-x: auto; }}
.chart-title {{ font-size: 14px; color: #8892a6; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.05em; }}

/* Tables */
table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; }}
th {{ text-align: left; padding: 10px 12px; background: #1a1d28; color: #8892a6; font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 1px solid #2a3142; }}
td {{ padding: 8px 12px; border-bottom: 1px solid #1e2230; font-size: 13px; }}
td.rank {{ color: #5c6578; font-weight: 700; width: 30px; }}
td.num {{ text-align: right; color: #4A90D9; font-weight: 600; }}
td.cumulative {{ text-align: right; color: #27ae60; font-weight: 700; }}
td.domain {{ font-weight: 600; }}
td.anchor {{ color: #b0b8c8; max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
td.notable {{ color: #b0b8c8; font-size: 12px; max-width: 300px; }}

/* Badges */
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 10px; font-weight: 700; }}
.badge.df {{ background: rgba(39,174,96,0.15); color: #27ae60; }}
.badge.nf {{ background: rgba(127,140,141,0.15); color: #7f8c8d; }}
.badge.burst {{ background: rgba(39,174,96,0.15); color: #27ae60; }}
.badge.steady {{ background: rgba(74,144,217,0.15); color: #4A90D9; }}
.badge.decline {{ background: rgba(231,76,60,0.15); color: #e74c3c; }}

/* Phase cards */
.phase-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; margin-bottom: 20px; }}
.phase-card {{ background: #1a1d28; border: 1px solid #2a3142; border-radius: 8px; padding: 14px 18px; }}
.phase-card .phase-name {{ font-size: 14px; font-weight: 700; margin-bottom: 6px; }}
.phase-card .phase-detail {{ font-size: 12px; color: #8892a6; }}
.phase-card .phase-avg {{ font-size: 13px; color: #4A90D9; font-weight: 600; margin-top: 4px; }}

/* Recommendations */
.recommendation {{ background: #1a1d28; border: 1px solid #2a3142; border-left: 4px solid #4A90D9; border-radius: 8px; padding: 16px 20px; margin-bottom: 12px; }}
.recommendation .rec-title {{ font-size: 15px; font-weight: 700; color: #fff; margin-bottom: 4px; }}
.recommendation .rec-body {{ font-size: 13px; color: #b0b8c8; }}

/* Expandable */
.expandable {{ display: none; }}
.expandable.show {{ display: block; }}
.expand-toggle {{ color: #4A90D9; cursor: pointer; font-size: 14px; font-weight: 600; margin-bottom: 12px; display: inline-block; }}
.expand-toggle:hover {{ text-decoration: underline; }}

.empty {{ color: #5c6578; font-size: 13px; padding: 12px; text-align: center; }}
.footer {{ text-align: center; margin-top: 40px; color: #5c6578; font-size: 11px; border-top: 1px solid #2a3142; padding-top: 20px; }}
</style>
<script>
function toggleExpand(id) {{
  var el = document.getElementById(id);
  el.classList.toggle('show');
  var toggle = el.previousElementSibling;
  if (el.classList.contains('show')) {{
    toggle.textContent = toggle.textContent.replace('Show', 'Hide');
  }} else {{
    toggle.textContent = toggle.textContent.replace('Hide', 'Show');
  }}
}}
</script>
</head>
<body>

<h1>📅 Referring Domains Timeline: {html.escape(domain)}</h1>
<p class="subtitle">When each referring domain first linked — link acquisition history</p>
<p class="generated">Generated {generated_at} · DataForSEO Backlinks API</p>

{overview_cards}

<h2>📈 Timeline Chart: Referring Domains Acquired Per Month</h2>
<div class="chart-container">
  <p class="chart-title">Monthly New Referring Domains Over Time</p>
  {svg_chart}
</div>

<h2>📋 Monthly Breakdown</h2>
<table>
  <thead>
    <tr>
      <th>Month</th>
      <th class="num">New Domains</th>
      <th class="num">Cumulative Total</th>
      <th>Notable Domains Acquired</th>
    </tr>
  </thead>
  <tbody>
    {monthly_table_rows}
  </tbody>
</table>

<h2>🏆 Top Referring Domains</h2>
<span class="expand-toggle" onclick="toggleExpand('top-domains-table')">Show Top 50 Referring Domains</span>
<div id="top-domains-table" class="expandable">
  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>Domain</th>
        <th>First Seen</th>
        <th class="num">Domain Strength</th>
        <th>Type</th>
        <th>Anchor Text</th>
      </tr>
    </thead>
    <tbody>
      {top_domains_rows}
    </tbody>
  </table>
</div>

<h2>📊 Link Building Phases</h2>
{phases_html}

<h2>💡 Recommendations</h2>
{recommendations}

<div class="footer">
  Referring Domains Timeline Report — {html.escape(domain)} · Generated by Zenfusion SEO · {generated_at}
</div>

</body>
</html>"""

    with open(report_file, 'w', encoding='utf-8') as f:
        f.write(report_html)
    print(f"  v Report written to {report_file}")
    return report_file


def _build_overview_cards(domain, timeline):
    """Build the summary card HTML."""
    earliest = timeline.get('earliest_date', '—') or '—'
    total_rd = timeline.get('total_referring_domains', 0)
    avg_pm = timeline.get('avg_domains_per_month', 0)
    peak = timeline.get('peak_month', {})
    peak_month = peak.get('month', '—') or '—'
    peak_count = peak.get('new_domains', 0)

    return f"""<div class="cards">
  <div class="card">
    <div class="card-label">Earliest Link Date</div>
    <div class="card-value">{html.escape(earliest)}</div>
    <div class="card-sub">First referring domain discovered</div>
  </div>
  <div class="card">
    <div class="card-label">Total Referring Domains</div>
    <div class="card-value">{total_rd:,}</div>
    <div class="card-sub">Unique domains linking to target</div>
  </div>
  <div class="card">
    <div class="card-label">Avg Domains / Month</div>
    <div class="card-value">{avg_pm}</div>
    <div class="card-sub">Overall acquisition rate</div>
  </div>
  <div class="card">
    <div class="card-label">Peak Month</div>
    <div class="card-value">{html.escape(peak_month)}</div>
    <div class="card-sub">{peak_count} new domains acquired</div>
  </div>
</div>"""


def _build_svg_timeline_chart(months):
    """Build an SVG bar chart showing new referring domains per month."""
    if not months:
        return '<p style="color:#5c6578;padding:20px;">No timeline data available for chart.</p>'

    month_list = list(months.values())
    max_val = max((m['new_domains'] for m in month_list), default=1)
    if max_val == 0:
        max_val = 1

    width = 920
    height = 360
    margin = {'top': 20, 'right': 30, 'bottom': 50, 'left': 50}
    plot_w = width - margin['left'] - margin['right']
    plot_h = height - margin['top'] - margin['bottom']

    num_months = len(month_list)
    bar_w = max(2, min(40, (plot_w / max(1, num_months)) - 2))
    gap = (plot_w / max(1, num_months)) - bar_w

    # Y-axis labels and gridlines
    y_labels = ""
    for i in range(5):
        val = int(max_val * i / 4)
        y = margin['top'] + plot_h - (val / max_val) * plot_h
        y_labels += f'<text x="{margin["left"] - 8}" y="{y + 4:.1f}" fill="#5c6578" font-size="11" text-anchor="end" font-family="sans-serif">{val}</text>'
        y_labels += f'<line x1="{margin["left"]}" y1="{y:.1f}" x2="{width - margin["right"]}" y2="{y:.1f}" stroke="#1e2230" stroke-width="1"/>'

    # Bars + x labels
    bars = ""
    x_labels = ""
    label_interval = max(1, num_months // 12)  # show ~12 labels max
    for i, m in enumerate(month_list):
        x = margin['left'] + i * (bar_w + gap) + gap / 2
        h = (m['new_domains'] / max_val) * plot_h
        y = margin['top'] + plot_h - h
        # Color gradient: newer = brighter
        intensity = 0.5 + 0.5 * (i / max(1, num_months - 1))
        bar_color = f'rgb({int(74 * intensity):d},{int(144 * intensity):d},{int(217 * intensity):d})'
        bars += f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" fill="{bar_color}" rx="2"/>'
        bars += f'<text x="{x + bar_w/2:.1f}" y="{y - 4:.1f}" fill="#b0b8c8" font-size="9" text-anchor="middle" font-family="sans-serif">{m["new_domains"]}</text>'

        if i % label_interval == 0 or i == num_months - 1:
            label_x = x + bar_w / 2
            x_labels += f'<text x="{label_x:.1f}" y="{height - margin["bottom"] + 15:.1f}" fill="#5c6578" font-size="9" text-anchor="middle" font-family="sans-serif" transform="rotate(-45 {label_x:.1f} {height - margin["bottom"] + 15:.1f})">{m["month"]}</text>'

    svg = f"""<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg" style="max-width:100%;">
  <rect width="{width}" height="{height}" fill="#0f1117" rx="8"/>
  {y_labels}
  {bars}
  <line x1="{margin['left']}" y1="{margin['top'] + plot_h}" x2="{width - margin['right']}" y2="{margin['top'] + plot_h}" stroke="#2a3142" stroke-width="1.5"/>
  {x_labels}
  <text x="{margin['left']}" y="{margin['top'] - 6}" fill="#8892a6" font-size="11" font-family="sans-serif">New Referring Domains</text>
</svg>"""
    return svg


def _build_monthly_table(months):
    """Build HTML rows for the monthly breakdown table."""
    if not months:
        return '<tr><td colspan="4" class="empty">No monthly data available.</td></tr>'

    rows = ""
    for m in months.values():
        notable_names = [html.escape(n.get('domain', '')) for n in m.get('notable', [])]
        notable_str = ', '.join(notable_names[:3])
        if len(notable_names) > 3:
            notable_str += f' +{len(notable_names) - 3} more'
        rows += f"""<tr>
  <td><strong>{html.escape(m['month'])}</strong></td>
  <td class="num">{m['new_domains']}</td>
  <td class="cumulative">{m['cumulative']:,}</td>
  <td class="notable">{notable_str or '—'}</td>
</tr>"""
    return rows


def _build_top_domains_table(backlinks):
    """Build HTML rows for the top referring domains table (expandable)."""
    if not backlinks:
        return '<tr><td colspan="6" class="empty">No referring domains data.</td></tr>'

    # Deduplicate by domain, keep earliest first_seen
    seen = set()
    unique = []
    for bl in sorted(backlinks, key=lambda x: x.get('first_seen', '9999-99-99') or '9999-99-99'):
        d = bl.get('domain', '')
        if d and d not in seen:
            seen.add(d)
            unique.append(bl)

    # Sort by domain strength descending, take top 50
    unique.sort(key=lambda x: x.get('domain_strength', 0), reverse=True)
    top = unique[:50]

    rows = ""
    for i, bl in enumerate(top, 1):
        ds = bl.get('domain_strength', 0)
        df_badge = '<span class="badge df">DF</span>' if bl.get('is_dofollow') else '<span class="badge nf">NF</span>'
        anchor = html.escape((bl.get('anchor', '') or '')[:80])
        domain = html.escape(bl.get('domain', ''))
        first_seen = html.escape(bl.get('first_seen', '—') or '—')
        rows += f"""<tr>
  <td class="rank">{i}</td>
  <td class="domain"><a href="https://{domain}" target="_blank" rel="noopener noindex nofollow">{domain}</a></td>
  <td>{first_seen}</td>
  <td class="num">{ds}</td>
  <td>{df_badge}</td>
  <td class="anchor">{anchor or '—'}</td>
</tr>"""
    return rows


def _build_phases_html(phases):
    """Build HTML for link building phases section."""
    if not phases:
        return '<div class="empty">No distinct phases identified.</div>'

    cards = ""
    for phase in phases:
        badge_class = phase.get('type', 'steady')
        icon = '🔥' if phase['type'] == 'burst' else ('📉' if phase['type'] == 'decline' else '📈')
        cards += f"""<div class="phase-card">
  <div class="phase-name">{icon} {html.escape(phase['name'])} <span class="badge {badge_class}">{phase['type']}</span></div>
  <div class="phase-detail">{phase['months_in_phase']} months · {phase['total_domains']} domains acquired</div>
  <div class="phase-avg">Avg: {phase['avg_per_month']}/month</div>
</div>"""
    return f'<div class="phase-grid">{cards}</div>'


def _build_recommendations(domain, timeline, phases):
    """Build actionable recommendations based on timeline patterns."""
    total_rd = timeline.get('total_referring_domains', 0)
    avg_pm = timeline.get('avg_domains_per_month', 0)
    peak = timeline.get('peak_month', {})
    earliest = timeline.get('earliest_date', '')
    latest = timeline.get('latest_date', '')

    recs = ""

    # No data
    if total_rd == 0:
        return '<div class="recommendation"><div class="rec-title">No Referring Domains Found</div><div class="rec-body">No referring domains were detected for this domain. This may indicate a new site, a domain with no backlinks, or an API issue.</div></div>'

    # Overall profile strength
    if total_rd < 50:
        recs += f"""<div class="recommendation">
  <div class="rec-title">⚠️ Small Backlink Profile ({total_rd} domains)</div>
  <div class="rec-body">{domain} has a relatively small referring domain count. Prioritize building high-quality editorial links through content marketing, digital PR, and targeted outreach to industry publications.</div>
</div>"""
    elif total_rd < 200:
        recs += f"""<div class="recommendation">
  <div class="rec-title">📊 Moderate Backlink Profile ({total_rd} domains)</div>
  <div class="rec-body">A solid foundation, but there's room to grow. Focus on diversifying referring domains through guest posting, resource page outreach, and HARO/journalist queries. Aim for 5-10 new domains per month.</div>
</div>"""
    else:
        recs += f"""<div class="recommendation">
  <div class="rec-title">✅ Strong Backlink Profile ({total_rd:,} domains)</div>
  <div class="rec-body">{domain} has a well-established link profile. Maintain link velocity through consistent content publication and brand monitoring. Look for high-authority domains and build topical authority through cluster campaigns.</div>
</div>"""

    # Peak month analysis
    peak_count = peak.get('new_domains', 0)
    if peak_count > 0 and avg_pm > 0 and peak_count > avg_pm * 3:
        recs += f"""<div class="recommendation">
  <div class="rec-title">🔥 Significant Link Spike in {html.escape(peak.get('month', ''))}</div>
  <div class="rec-body">A peak of {peak_count} new domains was detected, which is {peak_count / max(1, avg_pm):.1f}× the monthly average. Investigate what drove this spike (viral content, PR campaign, guest posting push) and replicate the strategy.</div>
</div>"""

    # Phase analysis
    burst_phases = [p for p in phases if p['type'] == 'burst']
    decline_phases = [p for p in phases if p['type'] == 'decline']

    if decline_phases:
        last_decline = decline_phases[-1]
        recs += f"""<div class="recommendation">
  <div class="rec-title">📉 Link Building Decline Detected</div>
  <div class="rec-body">A decline phase was identified ({html.escape(last_decline['start_month'])} to {html.escape(last_decline['end_month'])}) with only {last_decline['avg_per_month']} domains/month. If this is recent, reinvigorate outreach efforts. If historical, the site may have had a period of inactivity.</div>
</div>"""

    if burst_phases:
        first_burst = burst_phases[0]
        recs += f"""<div class="recommendation">
  <div class="rec-title">🚀 Burst Phase Identified ({html.escape(first_burst['start_month'])} to {html.escape(first_burst['end_month'])})</div>
  <div class="rec-body">During this period, {first_burst['total_domains']} domains were acquired at {first_burst['avg_per_month']}/month. Analyze the content and campaigns from this period to understand what drove accelerated link acquisition and recreate similar conditions.</div>
</div>"""

    # Consistency
    if avg_pm > 0 and total_rd > 0:
        recs += f"""<div class="recommendation">
  <div class="rec-title">📅 Maintain Consistent Link Acquisition</div>
  <div class="rec-body">The overall average is {avg_pm} domains/month. Search engines reward consistent, natural link growth. Set a target of at least {max(1, int(avg_pm))} new referring domains per month through a mix of content marketing, digital PR, and relationship building.</div>
</div>"""

    # Dofollow ratio
    df_count = sum(1 for bl in timeline.get('_backlinks', []) if bl.get('is_dofollow'))
    total = len(timeline.get('_backlinks', []))
    if total > 0:
        df_ratio = df_count / total
        if df_ratio < 0.5:
            recs += f"""<div class="recommendation">
  <div class="rec-title">🔗 Low Dofollow Ratio ({df_ratio*100:.0f}%)</div>
  <div class="rec-body">Only {df_ratio*100:.0f}% of links are dofollow. While a natural mix is healthy, consider prioritizing dofollow links in outreach to pass more link equity. Target editorial placements and guest posts on authoritative sites.</div>
</div>"""

    if not recs:
        recs = '<div class="recommendation"><div class="rec-title">No specific recommendations</div><div class="rec-body">The link profile appears balanced. Continue regular outreach and monitoring.</div></div>'

    return recs


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Referring Domains Timeline Analyzer — Zenfusion SEO'
    )
    parser.add_argument('--domain', required=True, help='Target domain to analyze')
    parser.add_argument('--limit', type=int, default=1000,
                        help='Max backlinks to fetch (default: 1000)')
    parser.add_argument('--output', default='/tmp/referring_timeline',
                        help='Output directory for HTML report')
    args = parser.parse_args()

    domain = normalize_domain(args.domain)
    print(f"\n{'='*60}")
    print(f"  Referring Domains Timeline: {domain}")
    print(f"{'='*60}")

    # Step 1: Fetch backlinks history
    print(f"\n[1/3] Fetching referring domains history...")
    backlinks = fetch_referring_domains_history(domain, limit=args.limit)
    if not backlinks:
        print("  x No backlinks found. Generating empty report.")

    # Step 2: Build timeline
    print(f"\n[2/3] Building timeline...")
    timeline = build_timeline(backlinks)
    timeline['_backlinks'] = backlinks  # for dofollow ratio in recommendations
    print(f"  v {timeline['total_referring_domains']} referring domains")
    print(f"  v {timeline['total_months']} months of data")
    print(f"  v Earliest: {timeline['earliest_date']} | Latest: {timeline['latest_date']}")
    print(f"  v Peak: {timeline['peak_month']['month']} ({timeline['peak_month']['new_domains']} domains)")
    print(f"  v {len(timeline['phases'])} phases identified")

    # Step 3: Generate report
    print(f"\n[3/3] Generating timeline report...")
    report_path = generate_timeline_report(domain, timeline, backlinks, args.output)
    print(f"\n{'='*60}")
    print(f"  Done! Report: {report_path}")
    print(f"{'='*60}")
    return report_path


if __name__ == '__main__':
    main()
