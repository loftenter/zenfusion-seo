#!/usr/bin/env python3
"""
Zenfusion Competitive Link Velocity Analyzer

Compares link building speed between a client and its competitors over time.
Shows who's gaining links fastest, who's losing them, and what domains link
to competitors but not to the client (link intercepts for outreach).

Functions:
  1. fetch_velocity_data(domain, days_back=90)   — daily new/lost backlinks + referring domains
  2. compare_velocities(client, competitors, ...) — rank competitors by velocity
  3. identify_link_intercepts(client, competitors) — new linking domains to competitors, not client
  4. generate_velocity_report(...)               — HTML report with charts, tables, recommendations

Usage:
  python3 competitive_link_velocity.py --client softprowatersystems.com \
      --competitors aquasana.com,culligan.com --days 90 --output /tmp/test_link_velocity

DataForSEO endpoints used:
  POST /v3/backlinks/timeseries_summary/live  — daily new/lost backlink counts
  POST /v3/backlinks/summary/live             — aggregate backlink metrics
  POST /v3/backlinks/backlinks/live           — individual backlink details (for intercepts)

Credentials: ~/.hermes/.env  (DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD)
"""

import os, sys, json, base64, urllib.request, argparse, time, html
from datetime import datetime, timedelta
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


# ── 1. fetch_velocity_data ─────────────────────────────────────────────────

def fetch_velocity_data(domain, days_back=90):
    """Call DataForSEO backlinks/timeseries_summary/live for the last N days.

    Returns daily new/lost backlinks and referring domains as a dict:
      {
        'domain': str,
        'series': [ {date, new_backlinks, lost_backlinks, new_referring_domains,
                     lost_referring_domains, referring_domains, backlinks}, ... ],
        'total_new_backlinks': int,
        'total_lost_backlinks': int,
        'total_new_referring_domains': int,
        'total_lost_referring_domains': int,
        'net_change': int,
        'avg_daily_new': float,
        'avg_daily_lost': float,
        'growth_rate': float,   # net per day
        'rd_growth_rate': float,
      }
    """
    domain = normalize_domain(domain)
    date_to = datetime.now().strftime('%Y-%m-%d')
    date_from = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')

    print(f"  -> Fetching velocity data for {domain} ({date_from} to {date_to})...")
    payload = {
        'target': domain,
        'date_from': date_from,
        'date_to': date_to,
        'group_range': 'day',
    }
    data = dfs_api_call('backlinks/timeseries_summary/live', payload)
    items = _extract_items(data)
    if not items:
        print(f"  x No timeseries data returned for {domain}")
        return _empty_velocity(domain)

    series = []
    for it in items:
        d = it.get('date', '') or it.get('datetime_from', '')
        series.append({
            'date': d[:10] if d else '',
            'new_backlinks': it.get('new_backlinks', 0) or 0,
            'lost_backlinks': it.get('lost_backlinks', 0) or 0,
            'new_referring_domains': it.get('new_referring_domains', 0) or 0,
            'lost_referring_domains': it.get('lost_referring_domains', 0) or 0,
            'referring_domains': it.get('referring_domains', 0) or 0,
            'backlinks': it.get('backlinks', 0) or 0,
            'date': it.get('date', '')[:10] if it.get('date') else '',
        })

    # DataForSEO timeseries returns ABSOLUTE counts (total backlinks on each date),
    # not new/lost. Calculate new/lost by subtracting consecutive days.
    for i in range(1, len(series)):
        prev = series[i-1]
        curr = series[i]
        curr['new_backlinks'] = max(0, curr['backlinks'] - prev['backlinks'])
        curr['lost_backlinks'] = max(0, prev['backlinks'] - curr['backlinks'])
        curr['new_referring_domains'] = max(0, curr['referring_domains'] - prev['referring_domains'])
        curr['lost_referring_domains'] = max(0, prev['referring_domains'] - curr['referring_domains'])
    # First day has no prior to compare to
    if series:
        series[0]['new_backlinks'] = 0
        series[0]['lost_backlinks'] = 0
        series[0]['new_referring_domains'] = 0
        series[0]['lost_referring_domains'] = 0
    # Sort by date ascending
    series.sort(key=lambda x: x['date'])

    total_new = sum(s['new_backlinks'] for s in series)
    total_lost = sum(s['lost_backlinks'] for s in series)
    total_new_rd = sum(s['new_referring_domains'] for s in series)
    total_lost_rd = sum(s['lost_referring_domains'] for s in series)
    net = total_new - total_lost
    rd_net = total_new_rd - total_lost_rd
    num_days = len(series) if series else 1

    result = {
        'domain': domain,
        'series': series,
        'total_new_backlinks': total_new,
        'total_lost_backlinks': total_lost,
        'total_new_referring_domains': total_new_rd,
        'total_lost_referring_domains': total_lost_rd,
        'net_change': net,
        'rd_net_change': rd_net,
        'avg_daily_new': round(total_new / num_days, 2),
        'avg_daily_lost': round(total_lost / num_days, 2),
        'growth_rate': round(net / num_days, 2),
        'rd_growth_rate': round(rd_net / num_days, 2),
    }
    print(f"  v {len(series)} days | New: {total_new:,} Lost: {total_lost:,} "
          f"Net: {net:+,} | Growth: {result['growth_rate']:+.1f}/day")
    return result


def _empty_velocity(domain):
    return {
        'domain': domain,
        'series': [],
        'total_new_backlinks': 0,
        'total_lost_backlinks': 0,
        'total_new_referring_domains': 0,
        'total_lost_referring_domains': 0,
        'net_change': 0,
        'rd_net_change': 0,
        'avg_daily_new': 0,
        'avg_daily_lost': 0,
        'growth_rate': 0,
        'rd_growth_rate': 0,
    }


# ── 2. compare_velocities ──────────────────────────────────────────────────

def compare_velocities(client_domain, competitors, days_back=90):
    """Fetch velocity data for client + all competitors and compute rankings.

    Returns dict with:
      'client': velocity_data dict,
      'competitors': {domain: velocity_data dict},
      'all_domains': [client + competitors],
      'rankings': [ {domain, growth_rate, rd_growth_rate, total_new, total_lost, net, rank}, ... ]
      'fastest_gainer': domain,
      'most_losing': domain,
    """
    client_domain = normalize_domain(client_domain)
    competitors = [normalize_domain(c) for c in competitors if c.strip()]

    print(f"\n=== Comparing link velocities over {days_back} days ===")
    print(f"  Client: {client_domain}")
    print(f"  Competitors: {', '.join(competitors)}")

    client_data = fetch_velocity_data(client_domain, days_back)

    competitor_data = {}
    for comp in competitors:
        time.sleep(0.5)  # be nice to the API
        competitor_data[comp] = fetch_velocity_data(comp, days_back)

    # Build rankings
    all_data = [client_data] + [competitor_data[c] for c in competitors]
    rankings = []
    for d in all_data:
        rankings.append({
            'domain': d['domain'],
            'growth_rate': d['growth_rate'],
            'rd_growth_rate': d['rd_growth_rate'],
            'total_new': d['total_new_backlinks'],
            'total_lost': d['total_lost_backlinks'],
            'net_change': d['net_change'],
            'total_new_rd': d['total_new_referring_domains'],
            'is_client': d['domain'] == client_domain,
        })
    # Sort by growth rate descending
    rankings.sort(key=lambda x: x['growth_rate'], reverse=True)
    for i, r in enumerate(rankings, 1):
        r['rank'] = i

    fastest = rankings[0]['domain'] if rankings else ''
    most_losing = rankings[-1]['domain'] if rankings else ''

    print(f"\n  Fastest gainer: {fastest}")
    print(f"  Most losing:    {most_losing}")

    return {
        'client': client_data,
        'competitors': competitor_data,
        'rankings': rankings,
        'fastest_gainer': fastest,
        'most_losing': most_losing,
        'days_back': days_back,
        'generated_at': datetime.now().isoformat(timespec='seconds'),
    }


# ── 3. identify_link_intercepts ─────────────────────────────────────────────

def identify_link_intercepts(client_domain, competitors, days_back=30):
    """Find domains that recently started linking to a competitor but NOT to client.

    Uses backlinks/backlinks/live for each competitor filtered by first_seen
    in the last N days, then cross-checks against the client's referring domains
    via backlinks/summary/live (which includes referring_domains count).

    Returns dict:
      {
        'intercepts': [ {domain, domain_strength, anchor, first_seen, competitor, is_dofollow}, ... ],
        'total_intercepts': int,
        'by_competitor': {comp_domain: [intercept, ...]},
      }
    """
    client_domain = normalize_domain(client_domain)
    competitors = [normalize_domain(c) for c in competitors if c.strip()]

    print(f"\n=== Identifying link intercepts (last {days_back} days) ===")

    # Fetch client's referring domains set via backlinks/live
    client_ref_domains = _get_referring_domains(client_domain)

    date_from = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')

    intercepts = []
    by_competitor = {}

    for comp in competitors:
        print(f"  -> Checking new links to {comp} since {date_from}...")
        time.sleep(0.5)
        comp_backlinks = _fetch_recent_backlinks(comp, date_from, limit=1000)
        comp_intercepts = []
        seen_domains = set()

        for bl in comp_backlinks:
            src_domain = normalize_domain(bl.get('domain_from', ''))
            if not src_domain or src_domain in client_ref_domains:
                continue
            if src_domain in seen_domains:
                continue
            seen_domains.add(src_domain)
            intercept = {
                'domain': src_domain,
                'domain_strength': bl.get('domain_strength', 0) or 0,
                'anchor': bl.get('anchor', ''),
                'first_seen': bl.get('first_seen', ''),
                'competitor': comp,
                'is_dofollow': bl.get('is_dofollow', False),
                'source_url': bl.get('source_url', ''),
            }
            intercepts.append(intercept)
            comp_intercepts.append(intercept)

        # Sort by domain strength descending
        comp_intercepts.sort(key=lambda x: x['domain_strength'], reverse=True)
        by_competitor[comp] = comp_intercepts
        print(f"  v Found {len(comp_intercepts)} intercepts from {comp}")

    # Global sort by domain strength
    intercepts.sort(key=lambda x: x['domain_strength'], reverse=True)

    print(f"\n  Total unique intercepts: {len(intercepts)}")
    return {
        'intercepts': intercepts,
        'total_intercepts': len(intercepts),
        'by_competitor': by_competitor,
        'days_back': days_back,
    }


def _get_referring_domains(domain, limit=1000):
    """Fetch the set of referring domains for a domain via backlinks/live."""
    print(f"  -> Fetching referring domains for {domain}...")
    payload = {
        'target': domain,
        'mode': 'as_is',
        'limit': limit,
        'include_subdomains': True,
    }
    data = dfs_api_call('backlinks/backlinks/live', payload)
    items = _extract_items(data)
    domains = set()
    for item in items:
        d = normalize_domain(item.get('domain_from', ''))
        if d:
            domains.add(d)
    print(f"  v {len(domains)} referring domains found for {domain}")
    return domains


def _fetch_recent_backlinks(domain, date_from, limit=1000):
    """Fetch backlinks to a domain with first_seen >= date_from."""
    payload = {
        'target': domain,
        'mode': 'as_is',
        'limit': limit,
        'include_subdomains': True,
        'filters': ['first_seen', '>=', date_from.replace('-', '')],
    }
    data = dfs_api_call('backlinks/backlinks/live', payload)
    items = _extract_items(data)
    backlinks = []
    for item in items:
        links_type = item.get('type', '')
        backlinks.append({
            'source_url': item.get('url_from', ''),
            'domain_from': item.get('domain_from', ''),
            'anchor': item.get('anchor', ''),
            'first_seen': (item.get('first_seen', '') or '')[:10],
            'domain_strength': item.get('domain_from_rank', 0) or 0,
            'is_dofollow': links_type == 'dofollow',
        })
    return backlinks


# ── 4. generate_velocity_report ─────────────────────────────────────────────

def generate_velocity_report(client_domain, competitors, data, output_path):
    """Generate an HTML report with charts, tables, and recommendations.

    data = output of compare_velocities() + data['intercepts'] = output of identify_link_intercepts()
    """
    client_domain = normalize_domain(client_domain)
    competitors = [normalize_domain(c) for c in competitors if c.strip()]
    intercepts_data = data.get('intercepts', {})
    rankings = data.get('rankings', [])

    client = data.get('client', _empty_velocity(client_domain))
    comp_data = data.get('competitors', {})

    # Build SVG line chart
    svg_chart = _build_svg_chart(client, comp_data, data.get('days_back', 90))

    # Summary cards
    client_growth = client.get('growth_rate', 0)
    top_comp = max(
        (comp_data[c] for c in competitors if c in comp_data),
        key=lambda x: x.get('growth_rate', 0),
        default=_empty_velocity('N/A'),
    )
    top_comp_growth = top_comp.get('growth_rate', 0)
    client_net = client.get('net_change', 0)
    avg_comp_net = (
        sum(comp_data[c].get('net_change', 0) for c in competitors if c in comp_data)
        / len(competitors)
        if competitors else 0
    )

    # Velocity comparison table
    velocity_table_rows = ""
    for r in rankings:
        is_client = r.get('is_client', False)
        row_class = 'client-row' if is_client else ''
        rank_badge = '🥇' if r['rank'] == 1 else ('🥈' if r['rank'] == 2 else ('🥉' if r['rank'] == 3 else f"{r['rank']}"))
        velocity_table_rows += f"""
        <tr class="{row_class}">
          <td class="rank">{rank_badge}</td>
          <td class="domain">{r['domain']}{' <span class="badge client-badge">CLIENT</span>' if is_client else ''}</td>
          <td class="num">{r['total_new']:,}</td>
          <td class="num lost">{r['total_lost']:,}</td>
          <td class="num {'net-pos' if r['net_change'] >= 0 else 'net-neg'}">{r['net_change']:+,}</td>
          <td class="num">{r['total_new_rd']:,}</td>
          <td class="num {'net-pos' if r['growth_rate'] >= 0 else 'net-neg'}">{r['growth_rate']:+.1f}/day</td>
        </tr>"""

    # Link building speed ranking
    speed_ranking_html = ""
    for r in rankings:
        is_client = r.get('is_client', False)
        bar_width = max(2, min(100, abs(r['growth_rate']) / max(1, abs(rankings[0]['growth_rate'])) * 100))
        bar_class = 'bar-pos' if r['growth_rate'] >= 0 else 'bar-neg'
        speed_ranking_html += f"""
        <div class="speed-row {'client-row' if is_client else ''}">
          <span class="speed-rank">#{r['rank']}</span>
          <span class="speed-domain">{r['domain']}{' ⭐' if is_client else ''}</span>
          <div class="speed-bar-container">
            <div class="speed-bar {bar_class}" style="width:{bar_width:.1f}%"></div>
          </div>
          <span class="speed-value {'net-pos' if r['growth_rate'] >= 0 else 'net-neg'}">{r['growth_rate']:+.1f}/day</span>
        </div>"""

    # New link intercepts table
    all_intercepts = intercepts_data.get('intercepts', [])
    intercepts_table_rows = ""
    for i, ic in enumerate(all_intercepts[:200], 1):
        ds = ic.get('domain_strength', 0)
        anchor = html.escape((ic.get('anchor', '') or '')[:60])
        df_badge = '<span class="badge df">DF</span>' if ic.get('is_dofollow') else '<span class="badge nf">NF</span>'
        intercepts_table_rows += f"""
        <tr>
          <td class="rank">{i}</td>
          <td class="domain"><a href="https://{ic['domain']}" target="_blank" rel="noopener noindex nofollow">{ic['domain']}</a></td>
          <td class="num">{ds}</td>
          <td>{df_badge}</td>
          <td class="anchor">{anchor or '—'}</td>
          <td>{ic.get('first_seen', '—')}</td>
          <td class="comp-pill">{ic.get('competitor', '')}</td>
        </tr>"""

    if not intercepts_table_rows:
        intercepts_table_rows = '<tr><td colspan="7" class="empty">No link intercepts found in this period.</td></tr>'

    # Per-competitor intercept summary
    by_comp = intercepts_data.get('by_competitor', {})
    comp_intercept_summary = ""
    for comp, items in by_comp.items():
        count = len(items)
        avg_ds = (sum(i.get('domain_strength', 0) for i in items) / count) if count else 0
        comp_intercept_summary += f"""
        <div class="comp-intercept-card">
          <span class="comp-name">{comp}</span>
          <span class="comp-count">{count} intercepts</span>
          <span class="comp-avg-ds">Avg DS: {avg_ds:.0f}</span>
        </div>"""

    # Recommendations
    recommendations = _build_recommendations(client_domain, competitors, rankings, all_intercepts)

    report_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<title>Link Velocity Report — {client_domain}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f1117; color: #fff; line-height: 1.6; padding: 20px 40px; max-width: 1200px; margin: 0 auto; }}
a {{ color: #4A90D9; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
h1 {{ font-size: 28px; margin-bottom: 4px; }}
h2 {{ color: #4A90D9; font-size: 20px; margin: 36px 0 16px; border-bottom: 1px solid #2a3142; padding-bottom: 8px; }}
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
td.num.lost {{ color: #e74c3c; }}
td.net-pos {{ color: #27ae60; font-weight: 700; }}
td.net-neg {{ color: #e74c3c; font-weight: 700; }}
td.domain {{ font-weight: 600; }}
td.anchor {{ color: #b0b8c8; max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
tr.client-row {{ background: rgba(74,144,217,0.08); }}
.comp-pill {{ font-size: 11px; color: #8892a6; }}

/* Badges */
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 10px; font-weight: 700; }}
.badge.client-badge {{ background: rgba(74,144,217,0.2); color: #4A90D9; margin-left: 6px; }}
.badge.df {{ background: rgba(39,174,96,0.15); color: #27ae60; }}
.badge.nf {{ background: rgba(127,140,141,0.15); color: #7f8c8d; }}

/* Speed ranking bars */
.speed-row {{ display: flex; align-items: center; gap: 12px; padding: 10px 0; border-bottom: 1px solid #1e2230; }}
.speed-rank {{ color: #5c6578; font-weight: 700; min-width: 30px; font-size: 14px; }}
.speed-domain {{ min-width: 200px; font-weight: 600; font-size: 14px; }}
.speed-bar-container {{ flex: 1; background: #1e2230; border-radius: 6px; height: 24px; overflow: hidden; }}
.speed-bar {{ height: 100%; border-radius: 6px; transition: width 0.5s ease; }}
.speed-bar.bar-pos {{ background: linear-gradient(90deg, #27ae60, #2ecc71); }}
.speed-bar.bar-neg {{ background: linear-gradient(90deg, #e74c3c, #c0392b); }}
.speed-value {{ min-width: 80px; text-align: right; font-weight: 700; font-size: 14px; }}

/* Intercept cards */
.comp-intercept-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; margin-bottom: 20px; }}
.comp-intercept-card {{ background: #1a1d28; border: 1px solid #2a3142; border-radius: 8px; padding: 14px; display: flex; flex-direction: column; gap: 4px; }}
.comp-intercept-card .comp-name {{ font-size: 14px; font-weight: 700; color: #4A90D9; }}
.comp-intercept-card .comp-count {{ font-size: 13px; color: #27ae60; font-weight: 600; }}
.comp-intercept-card .comp-avg-ds {{ font-size: 12px; color: #8892a6; }}

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
</head>
<body>

<h1>📊 Link Velocity Report: {client_domain}</h1>
<p class="subtitle">Competitor comparison over {data.get('days_back', 90)} days</p>
<p class="generated">Generated: {data.get('generated_at', datetime.now().isoformat(timespec='seconds'))}</p>

<!-- Summary Cards -->
<div class="cards">
  <div class="card">
    <div class="card-label">Client Growth Rate</div>
    <div class="card-value {'pos' if client_growth >= 0 else 'neg'}">{client_growth:+.1f}</div>
    <div class="card-sub">new links / day</div>
  </div>
  <div class="card">
    <div class="card-label">Top Competitor Growth</div>
    <div class="card-value {'pos' if top_comp_growth >= 0 else 'neg'}">{top_comp_growth:+.1f}</div>
    <div class="card-sub">{top_comp.get('domain', '—')}</div>
  </div>
  <div class="card">
    <div class="card-label">Client Net Link Change</div>
    <div class="card-value {'pos' if client_net >= 0 else 'neg'}">{client_net:+,}</div>
    <div class="card-sub">over {data.get('days_back', 90)} days</div>
  </div>
  <div class="card">
    <div class="card-label">Avg Competitor Net Change</div>
    <div class="card-value {'pos' if avg_comp_net >= 0 else 'neg'}">{avg_comp_net:+,.0f}</div>
    <div class="card-sub">competitor average</div>
  </div>
  <div class="card">
    <div class="card-label">Link Intercepts</div>
    <div class="card-value">{intercepts_data.get('total_intercepts', 0):,}</div>
    <div class="card-sub">outreach opportunities</div>
  </div>
</div>

<!-- Velocity Chart -->
<h2>📈 Link Velocity Comparison</h2>
<div class="chart-container">
  <div class="chart-title">New Backlinks Per Day — {client_domain} vs Competitors</div>
  {svg_chart}
</div>

<!-- Velocity Comparison Table -->
<h2>📋 Velocity Comparison Table</h2>
<table>
  <thead>
    <tr>
      <th>Rank</th>
      <th>Domain</th>
      <th>New Links</th>
      <th>Lost Links</th>
      <th>Net Change</th>
      <th>New Ref Domains</th>
      <th>Growth Rate</th>
    </tr>
  </thead>
  <tbody>
    {velocity_table_rows or '<tr><td colspan="7" class="empty">No data available.</td></tr>'}
  </tbody>
</table>

<!-- Link Building Speed Ranking -->
<h2>🏆 Link Building Speed Ranking</h2>
<div class="chart-container">
  <div class="chart-title">Who's Building Links Fastest? (net new links per day)</div>
  {speed_ranking_html or '<div class="empty">No ranking data available.</div>'}
</div>

<!-- Link Intercepts -->
<h2>🎯 New Link Intercepts</h2>
<p class="subtitle">Domains linking to competitors in the last {intercepts_data.get('days_back', 30)} days but NOT to {client_domain}</p>

{comp_intercept_summary if comp_intercept_summary else ''}

<span class="expand-toggle" onclick="document.getElementById('intercepts-table').classList.toggle('show')">
  ▼ Show/Hide {intercepts_data.get('total_intercepts', 0)} Intercept Domains
</span>
<div id="intercepts-table" class="expandable">
  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>Domain</th>
        <th>Domain Strength</th>
        <th>Type</th>
        <th>Anchor Text</th>
        <th>First Seen</th>
        <th>Competitor</th>
      </tr>
    </thead>
    <tbody>
      {intercepts_table_rows}
    </tbody>
  </table>
</div>

<!-- Recommendations -->
<h2>💡 Recommendations</h2>
{recommendations or '<div class="empty">No recommendations generated.</div>'}

<div class="footer">
  Generated by Zenfusion Competitive Link Velocity Analyzer | DataForSEO Backlinks API | noindex, nofollow
</div>

<script>
// Expandable intercepts table
</script>
</body>
</html>"""

    # Ensure output directory exists
    os.makedirs(output_path, exist_ok=True)
    report_file = os.path.join(output_path, 'link_velocity_report.html')
    with open(report_file, 'w') as f:
        f.write(report_html)
    print(f"\n  Report saved to: {report_file}")
    return report_file


def _build_svg_chart(client, comp_data, days_back):
    """Build an SVG line chart showing new backlinks per day for client vs competitors."""
    # Collect all series
    all_series = {}
    all_series[client.get('domain', 'client')] = client.get('series', [])
    for domain, d in comp_data.items():
        all_series[domain] = d.get('series', [])

    if not any(all_series.values()):
        return '<p style="color:#5c6578;padding:20px;">No time series data available for chart.</p>'

    # Chart dimensions
    width = 900
    height = 360
    margin = {'top': 20, 'right': 200, 'bottom': 40, 'left': 50}
    plot_w = width - margin['left'] - margin['right']
    plot_h = height - margin['top'] - margin['bottom']

    # Find max value across all series
    max_val = 1
    for series in all_series.values():
        for point in series:
            val = point.get('new_backlinks', 0)
            if val > max_val:
                max_val = val

    # Collect all dates
    all_dates = set()
    for series in all_series.values():
        for point in series:
            if point.get('date'):
                all_dates.add(point['date'])
    all_dates = sorted(all_dates)
    num_dates = len(all_dates) or 1

    # Colors for each domain
    colors = ['#4A90D9', '#e74c3c', '#f39c12', '#9b59b6', '#1abc9c', '#e67e22']
    domain_list = list(all_series.keys())

    # Build polylines
    def date_to_x(d):
        if not all_dates:
            return 0
        idx = all_dates.index(d) if d in all_dates else 0
        return margin['left'] + (idx / max(1, num_dates - 1)) * plot_w

    def val_to_y(v):
        return margin['top'] + plot_h - (v / max_val) * plot_h

    polylines = ""
    legend_items = ""
    for i, domain in enumerate(domain_list):
        series = all_series[domain]
        if not series:
            continue
        color = colors[i % len(colors)]
        points_str = " ".join(
            f"{date_to_x(p.get('date', '')):.1f},{val_to_y(p.get('new_backlinks', 0)):.1f}"
            for p in series
        )
        polylines += f'<polyline points="{points_str}" fill="none" stroke="{color}" stroke-width="2" opacity="0.85"/>'

        # Legend
        legend_y = margin['top'] + i * 22
        legend_items += f"""
        <rect x="{width - margin['right'] + 10}" y="{legend_y}" width="12" height="12" fill="{color}" rx="2"/>
        <text x="{width - margin['right'] + 28}" y="{legend_y + 10}" fill="#b0b8c8" font-size="12" font-family="sans-serif">{domain[:25]}</text>"""

    # Y-axis labels
    y_labels = ""
    for i in range(5):
        val = int(max_val * i / 4)
        y = val_to_y(val)
        y_labels += f'<text x="{margin["left"] - 8}" y="{y + 4}" fill="#5c6578" font-size="11" text-anchor="end" font-family="sans-serif">{val}</text>'
        y_labels += f'<line x1="{margin["left"]}" y1="{y}" x2="{width - margin["right"]}" y2="{y}" stroke="#1e2230" stroke-width="1"/>'

    # X-axis labels (first, middle, last)
    x_labels = ""
    if all_dates:
        for label_date in [all_dates[0], all_dates[len(all_dates) // 2], all_dates[-1]]:
            x = date_to_x(label_date)
            x_labels += f'<text x="{x}" y="{height - margin["bottom"] + 15}" fill="#5c6578" font-size="10" text-anchor="middle" font-family="sans-serif">{label_date[5:]}</text>'

    svg = f"""<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg" style="max-width:100%;">
  <rect width="{width}" height="{height}" fill="#0f1117" rx="8"/>
  {y_labels}
  {polylines}
  <line x1="{margin['left']}" y1="{margin['top'] + plot_h}" x2="{width - margin['right']}" y2="{margin['top'] + plot_h}" stroke="#2a3142" stroke-width="1.5"/>
  {x_labels}
  {legend_items}
</svg>"""
    return svg


def _build_recommendations(client_domain, competitors, rankings, intercepts):
    """Build actionable recommendations from the velocity data and intercepts."""
    if not rankings:
        return '<div class="recommendation"><div class="rec-title">No data</div><div class="rec-body">Insufficient data to generate recommendations.</div></div>'

    recs = ""

    # Recommendation 1: Compare growth rates
    client_rank = next((r for r in rankings if r.get('is_client')), None)
    if client_rank:
        rank_pos = client_rank.get('rank', 999)
        if rank_pos == 1:
            recs += f"""<div class="recommendation">
  <div class="rec-title">✅ You're Leading the Pack</div>
  <div class="rec-body">{client_domain} is building links faster than all competitors at {client_rank['growth_rate']:+.1f} links/day. Maintain this pace and consider scaling outreach to widen the gap further.</div>
</div>"""
        else:
            leader = rankings[0]
            gap = leader['growth_rate'] - client_rank['growth_rate']
            recs += f"""<div class="recommendation">
  <div class="rec-title">⚠️ You're #{rank_pos} in Link Velocity</div>
  <div class="rec-body">{leader['domain']} is building links {gap:.1f}/day faster than {client_domain}. Increase outreach velocity to close the gap. Current rate: {client_rank['growth_rate']:+.1f}/day vs leader: {leader['growth_rate']:+.1f}/day.</div>
</div>"""

    # Recommendation 2: Target intercept domains
    if intercepts:
        top_intercepts = intercepts[:10]
        top_domains = [i['domain'] for i in top_intercepts]
        recs += f"""<div class="recommendation">
  <div class="rec-title">🎯 Target {len(intercepts)} Intercept Domains for Outreach</div>
  <div class="rec-body">These domains recently started linking to competitors but NOT to {client_domain}. Top targets by domain strength: {', '.join(top_domains[:5])}. Prioritize dofollow links from high domain strength sites.</div>
</div>"""

    # Recommendation 3: Per-competitor targeting
    by_comp = {}
    for ic in intercepts:
        comp = ic.get('competitor', '')
        if comp not in by_comp:
            by_comp[comp] = []
        by_comp[comp].append(ic)

    for comp, items in sorted(by_comp.items(), key=lambda x: len(x[1]), reverse=True):
        avg_ds = (sum(i.get('domain_strength', 0) for i in items) / len(items)) if items else 0
        recs += f"""<div class="recommendation">
  <div class="rec-title">📡 Outreach Target: {comp}'s New Linking Domains</div>
  <div class="rec-body">{len(items)} domains linked to {comp} but not to {client_domain} in the last 30 days (avg domain strength: {avg_ds:.0f}). These sites are proven linkers in your niche — they've already linked to a competitor, so they're warm prospects.</div>
</div>"""

    # Recommendation 4: Losing links warning
    for r in rankings:
        if r.get('total_lost', 0) > r.get('total_new', 0) and r.get('is_client'):
            recs += f"""<div class="recommendation">
  <div class="rec-title">🚨 You're Losing More Links Than You're Gaining</div>
  <div class="rec-body">{client_domain} lost {r['total_lost']:,} links while gaining {r['total_new']:,} (net {r['net_change']:+,}). Audit lost backlinks — check for broken pages, removed content, or expired partnerships. Reclaim lost links before building new ones.</div>
</div>"""
            break

    return recs


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Compare link building velocity between a client and competitors.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Example:
  python3 competitive_link_velocity.py --client softprowatersystems.com \\
      --competitors aquasana.com,culligan.com --days 90 --output /tmp/test_link_velocity
""")
    parser.add_argument('--client', required=True, help='Client domain')
    parser.add_argument('--competitors', required=True, help='Comma-separated competitor domains')
    parser.add_argument('--days', type=int, default=90, help='Days of history to analyze (default: 90)')
    parser.add_argument('--output', required=True, help='Output directory for the report')

    args = parser.parse_args()

    client = normalize_domain(args.client)
    competitors = [normalize_domain(c) for c in args.competitors.split(',') if c.strip()]

    if not competitors:
        print("ERROR: At least one competitor required")
        sys.exit(1)

    # Step 1+2: Compare velocities
    velocity_data = compare_velocities(client, competitors, args.days)

    # Step 3: Identify link intercepts
    intercept_data = identify_link_intercepts(client, competitors, days_back=min(30, args.days))
    velocity_data['intercepts'] = intercept_data

    # Step 4: Generate report
    report_path = generate_velocity_report(client, competitors, velocity_data, args.output)

    print(f"\n{'='*60}")
    print(f"  Report: {report_path}")
    print(f"  Intercepts: {intercept_data['total_intercepts']} domains")
    ranking_str = ', '.join(
        f"{r['rank']}.{r['domain']}({r['growth_rate']:+.1f}/d)"
        for r in velocity_data['rankings']
    )
    print(f"  Rankings: {ranking_str}")
    print("=" * 60)


if __name__ == '__main__':
    main()
