#!/usr/bin/env python3
"""
Lost Backlink Recovery — tracks lost backlinks over time and generates
outreach recommendations with an HTML report.

Functions:
  1. fetch_backlink_timeseries(domain, days_back=90)
     → DataForSEO backlinks/timeseries_summary/live
  2. identify_lost_backlinks(domain, days_back=30)
     → DataForSEO backlinks/backlinks/live with is_broken filter
  3. classify_loss_reason(backlink)
     → HTTP-checks referring page, classifies loss reason
  4. generate_recovery_report(domain, timeseries, lost_links, output_path)
     → HTML report with SVG chart, summary cards, table, recommendations

Usage:
  python3 lost_backlink_recovery.py --domain softprowatersystems.com \\
      --competitor aquasana.com --output /tmp/test_lost_recovery

DataForSEO credentials in ~/.hermes/.env:
  DATAFORSEO_LOGIN
  DATAFORSEO_PASSWORD
"""

import os
import sys
import json
import base64
import argparse
import urllib.request
import urllib.error
import ssl
from datetime import datetime, timedelta
from html import escape as html_escape
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ENV_PATH = os.path.expanduser('~/.hermes/.env')
MAJESTIC_CSV = os.path.expanduser('~/Downloads/majestic_million.csv')
DEFAULT_OUTPUT_DIR = '/tmp/lost_backlink_recovery'

# White SaaS theme
BG_COLOR = '#ffffff'
TEXT_COLOR = '#1a1a1a'
LINK_COLOR = '#0066cc'
BODY_FONT_SIZE = '16px'
TITLE_FONT_SIZE = '24px'
SUBTITLE_FONT_SIZE = '20px'

# SSL context for HTTP checks (ignore cert errors for classification)
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------------------
# Credentials & API helpers
# ---------------------------------------------------------------------------

def _load_env():
    """Load key=value pairs from ~/.hermes/.env into a dict."""
    env = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    env[k] = v.strip()
    return env


def get_creds():
    """Return (login, password) from env vars or ~/.hermes/.env."""
    login = os.environ.get('DATAFORSEO_LOGIN')
    password = os.environ.get('DATAFORSEO_PASSWORD')
    if not login or not password:
        env = _load_env()
        login = env.get('DATAFORSEO_LOGIN')
        password = env.get('DATAFORSEO_PASSWORD')
    if not login or not password:
        print("ERROR: DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD not found")
        print("Set them in ~/.hermes/.env or as environment variables")
        sys.exit(1)
    return login, password


def dfs_api_call(endpoint, payload, timeout=120):
    """POST to a DataForSEO v3 live endpoint. Payload is a single task dict;
    it will be wrapped in an array per DataForSEO convention."""
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
        print(f"  Request error: {e}")
        return None


# ---------------------------------------------------------------------------
# 1. fetch_backlink_timeseries
# ---------------------------------------------------------------------------

def fetch_backlink_timeseries(domain, days_back=90):
    """Call DataForSEO backlinks/timeseries_summary/live for the last N days.

    Returns a list of dicts, one per time bucket, each containing:
      date, backlinks, new_backlinks, lost_backlinks,
      referring_domains, new_referring_domains, lost_referring_domains
    """
    date_to = datetime.now().strftime('%Y-%m-%d')
    date_from = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')

    payload = {
        'target': domain,
        'include_subdomains': True,
        'date_from': date_from,
        'date_to': date_to,
        'group_by': 'date',
    }
    print(f"  Fetching timeseries for {domain} ({date_from} → {date_to})…")
    data = dfs_api_call('backlinks/timeseries_summary/live', payload)
    if not data or not data.get('tasks'):
        return []

    task = data['tasks'][0]
    if task.get('status_code') != 20000:
        print(f"  Timeseries task error: {task.get('status_message', 'Unknown')}")
        return []

    result = task.get('result', [{}])[0]
    raw_items = result.get('items', [])

    # Compute new/lost deltas from consecutive buckets
    series = []
    prev_backlinks = None
    prev_ref_domains = None
    for item in raw_items:
        date_str = (item.get('date') or '')[:10]
        backlinks = item.get('backlinks', 0) or 0
        ref_domains = item.get('referring_domains', 0) or 0

        if prev_backlinks is not None:
            new_bl = max(0, backlinks - prev_backlinks)
            lost_bl = max(0, prev_backlinks - backlinks)
        else:
            new_bl = 0
            lost_bl = 0

        if prev_ref_domains is not None:
            new_rd = max(0, ref_domains - prev_ref_domains)
            lost_rd = max(0, prev_ref_domains - ref_domains)
        else:
            new_rd = 0
            lost_rd = 0

        series.append({
            'date': date_str,
            'backlinks': backlinks,
            'new_backlinks': new_bl,
            'lost_backlinks': lost_bl,
            'referring_domains': ref_domains,
            'new_referring_domains': new_rd,
            'lost_referring_domains': lost_rd,
        })
        prev_backlinks = backlinks
        prev_ref_domains = ref_domains

    return series


# ---------------------------------------------------------------------------
# 2. identify_lost_backlinks
# ---------------------------------------------------------------------------

def identify_lost_backlinks(domain, days_back=30):
    """Call DataForSEO backlinks/backlinks/live with is_broken filter to get
    actual lost/broken backlinks.

    Returns list of dicts with: source_url, target_url, domain_from,
    domain_to, anchor, lost_date, domain_strength, page_from_status_code,
    url_to_status_code, is_broken, dofollow, first_seen, last_seen.
    """
    print(f"  Fetching lost/broken backlinks for {domain}…")
    payload = {
        'target': domain,
        'include_subdomains': True,
        'limit': 200,
        'filters': ['is_broken', '=', True],
        'order_by': ['domain_from_rank,desc'],
    }
    data = dfs_api_call('backlinks/backlinks/live', payload)
    if not data or not data.get('tasks'):
        return []

    task = data['tasks'][0]
    if task.get('status_code') != 20000:
        print(f"  Backlinks task error: {task.get('status_message', 'Unknown')}")
        return []

    result = task.get('result', [{}])[0]
    items = result.get('items', [])
    total = result.get('total_count', 0)
    print(f"    API returned {len(items)} broken backlinks (total: {total})")

    cutoff = datetime.now() - timedelta(days=days_back)
    lost_links = []
    for item in items:
        last_seen_raw = item.get('last_seen', '') or ''
        last_seen_dt = None
        try:
            last_seen_dt = datetime.strptime(last_seen_raw[:19], '%Y-%m-%d %H:%M:%S')
        except (ValueError, TypeError):
            pass

        lost_links.append({
            'source_url': item.get('url_from', ''),
            'target_url': item.get('url_to', ''),
            'domain_from': item.get('domain_from', '').lower(),
            'domain_to': item.get('domain_to', '').lower(),
            'anchor': item.get('anchor', '') or '',
            'lost_date': last_seen_raw[:10],
            'last_seen_dt': last_seen_dt,
            'domain_strength': item.get('domain_from_rank', 0) or 0,
            'page_from_rank': item.get('page_from_rank', 0) or 0,
            'page_from_status_code': item.get('page_from_status_code', 0) or 0,
            'url_to_status_code': item.get('url_to_status_code', 0) or 0,
            'is_broken': item.get('is_broken', False),
            'dofollow': item.get('dofollow', True),
            'first_seen': (item.get('first_seen', '') or '')[:10],
            'last_seen': last_seen_raw[:10],
            'url_to_redirect_target': item.get('url_to_redirect_target', ''),
            'is_indirect_link': item.get('is_indirect_link', False),
        })

    # Filter to last_seen within days_back window (if we can parse dates)
    recent = []
    for ll in lost_links:
        if ll['last_seen_dt'] and ll['last_seen_dt'] < cutoff:
            continue
        recent.append(ll)
    # If filtering removes everything, keep all (API may not have recent data)
    if not recent and lost_links:
        recent = lost_links

    return recent


# ---------------------------------------------------------------------------
# 3. classify_loss_reason
# ---------------------------------------------------------------------------

def _http_check(url, timeout=8):
    """Perform an HTTP HEAD/GET request and return (status_code, final_url).
    Returns (0, url) on failure."""
    if not url or not url.startswith('http'):
        return 0, url
    try:
        req = urllib.request.Request(url, method='HEAD', headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/120.0.0.0 Safari/537.36'
        })
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx) as resp:
            return resp.getcode(), resp.url
    except urllib.error.HTTPError as e:
        return e.code, url
    except Exception:
        # Try GET as fallback (some servers reject HEAD)
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                              'AppleWebKit/537.36 (KHTML, like Gecko) '
                              'Chrome/120.0.0.0 Safari/537.36'
            })
            with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx) as resp:
                return resp.getcode(), resp.url
        except urllib.error.HTTPError as e:
            return e.code, url
        except Exception:
            return 0, url


def classify_loss_reason(backlink):
    """Classify why a backlink was likely lost.

    Uses the DataForSEO-provided status codes plus a live HTTP check on the
    referring page to determine the reason.

    Returns a dict: {reason, detail, recoverable, priority_boost}
    """
    source_url = backlink.get('source_url', '')
    target_url = backlink.get('target_url', '')
    api_page_status = backlink.get('page_from_status_code', 0)
    api_target_status = backlink.get('url_to_status_code', 0)
    domain_from = backlink.get('domain_from', '')

    # Live-check the referring page
    live_status, live_url = _http_check(source_url)

    # Determine reason
    # 1. Referring page is gone (404/410/500)
    if live_status in (404, 410) or api_page_status in (404, 410):
        return {
            'reason': 'Page Removed (404)',
            'detail': f'Referring page returns {live_status or api_page_status}. '
                      f'The page containing the link no longer exists.',
            'recoverable': False,
            'priority_boost': 0,
        }

    # 2. Referring page redirected
    if live_status in (301, 302, 307, 308) or api_page_status in (301, 302, 307, 308):
        return {
            'reason': 'Page Redirected',
            'detail': f'Referring page redirected (HTTP {live_status or api_page_status}). '
                      f'Link may have been lost during page consolidation.',
            'recoverable': True,
            'priority_boost': 5,
        }

    # 3. Domain expired (DNS failure, connection refused)
    if live_status == 0 and source_url:
        return {
            'reason': 'Domain Expired / Unreachable',
            'detail': f'Could not reach {domain_from}. Domain may have expired '
                      f'or server is down.',
            'recoverable': False,
            'priority_boost': 0,
        }

    # 4. Target page is gone (404/410 on url_to)
    if api_target_status in (404, 410):
        return {
            'reason': 'Target Page Removed (404)',
            'detail': f'Your page ({target_url}) returns {api_target_status}. '
                      f'Fix the page or set up a redirect to recover the link.',
            'recoverable': True,
            'priority_boost': 15,
        }

    # 5. Target page redirected
    if api_target_status in (301, 302, 307, 308):
        return {
            'reason': 'Target Page Redirected',
            'detail': f'Your page redirected (HTTP {api_target_status}). '
                      f'Link value may be diluted through the redirect chain.',
            'recoverable': True,
            'priority_boost': 10,
        }

    # 6. Page is live but link gone (link was removed)
    if live_status == 200 and api_page_status == 200:
        return {
            'reason': 'Link Removed',
            'detail': f'Referring page is live (200 OK) but the link to your '
                      f'site is no longer present. Outreach recommended.',
            'recoverable': True,
            'priority_boost': 20,
        }

    # 7. Generic broken
    if api_target_status >= 500 or live_status >= 500:
        return {
            'reason': 'Server Error',
            'detail': f'Server error detected (target: {api_target_status}, '
                      f'page: {live_status}). May be temporary.',
            'recoverable': True,
            'priority_boost': 5,
        }

    # 8. Fallback
    return {
        'reason': 'Broken Backlink',
        'detail': f'Backlink flagged as broken. Page status: {live_status}, '
                  f'target status: {api_target_status}.',
        'recoverable': True,
        'priority_boost': 5,
    }


def classify_loss_reason_api_only(backlink):
    """Classify loss reason using only DataForSEO API data (no HTTP checks).

    Faster than classify_loss_reason but less accurate since it doesn't
    live-check the referring page.
    """
    api_page_status = backlink.get('page_from_status_code', 0)
    api_target_status = backlink.get('url_to_status_code', 0)
    domain_from = backlink.get('domain_from', '')
    target_url = backlink.get('target_url', '')

    # 1. Target page is gone (404/410 on url_to)
    if api_target_status in (404, 410):
        return {
            'reason': 'Target Page Removed (404)',
            'detail': f'Your page ({target_url}) returns {api_target_status}. '
                      f'Fix the page or set up a redirect to recover the link.',
            'recoverable': True,
            'priority_boost': 15,
        }

    # 2. Target page redirected
    if api_target_status in (301, 302, 307, 308):
        return {
            'reason': 'Target Page Redirected',
            'detail': f'Your page redirected (HTTP {api_target_status}). '
                      f'Link value may be diluted through the redirect chain.',
            'recoverable': True,
            'priority_boost': 10,
        }

    # 3. Referring page is gone (404/410 on page_from)
    if api_page_status in (404, 410):
        return {
            'reason': 'Page Removed (404)',
            'detail': f'Referring page on {domain_from} returns {api_page_status}. '
                      f'The page containing the link no longer exists.',
            'recoverable': False,
            'priority_boost': 0,
        }

    # 4. Referring page redirected
    if api_page_status in (301, 302, 307, 308):
        return {
            'reason': 'Page Redirected',
            'detail': f'Referring page redirected (HTTP {api_page_status}). '
                      f'Link may have been lost during page consolidation.',
            'recoverable': True,
            'priority_boost': 5,
        }

    # 5. Server errors
    if api_target_status >= 500 or api_page_status >= 500:
        return {
            'reason': 'Server Error',
            'detail': f'Server error detected (target: {api_target_status}, '
                      f'page: {api_page_status}). May be temporary.',
            'recoverable': True,
            'priority_boost': 5,
        }

    # 6. Page is live but link broken (link was removed)
    if api_page_status == 200:
        return {
            'reason': 'Link Removed',
            'detail': f'Referring page is live (200 OK) but the link is broken. '
                      f'Link may have been removed. Outreach recommended.',
            'recoverable': True,
            'priority_boost': 20,
        }

    # 7. Generic broken
    return {
        'reason': 'Broken Backlink',
        'detail': f'Backlink flagged as broken. Page status: {api_page_status}, '
                  f'target status: {api_target_status}.',
        'recoverable': True,
        'priority_boost': 5,
    }


# ---------------------------------------------------------------------------
# Majestic Million lookup
# ---------------------------------------------------------------------------

def load_majestic_domains():
    """Load Majestic Million CSV and return a set of domain names + dict of
    domain→rank. Returns ({}, {}) if file not found."""
    domains = set()
    ranks = {}
    if not os.path.exists(MAJESTIC_CSV):
        return domains, ranks
    try:
        import csv
        with open(MAJESTIC_CSV, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                d = row.get('Domain', '').lower().strip()
                if d:
                    domains.add(d)
                    try:
                        ranks[d] = int(row.get('GlobalRank', 0))
                    except (ValueError, TypeError):
                        pass
    except Exception:
        pass
    return domains, ranks


# ---------------------------------------------------------------------------
# 4. generate_recovery_report
# ---------------------------------------------------------------------------

def _svg_timeline_chart(series, competitor_series=None):
    """Generate an SVG chart showing new vs lost backlinks over time.

    Returns an SVG string."""
    if not series:
        return '<p style="color:#666;font-size:16px;">No timeseries data available.</p>'

    width = 900
    height = 300
    margin = {'top': 20, 'right': 120, 'bottom': 50, 'left': 60}
    plot_w = width - margin['left'] - margin['right']
    plot_h = height - margin['top'] - margin['bottom']

    all_vals = []
    for s in series:
        all_vals.append(s.get('new_backlinks', 0))
        all_vals.append(s.get('lost_backlinks', 0))
    if competitor_series:
        for s in competitor_series:
            all_vals.append(s.get('lost_backlinks', 0))
    max_val = max(all_vals) if all_vals else 1
    if max_val == 0:
        max_val = 1

    n = len(series)
    bar_width = max(8, min(40, plot_w / (n * 3))) if n > 0 else 20
    group_width = bar_width * 2 + 4
    total_width = n * group_width
    if total_width < plot_w:
        offset = (plot_w - total_width) / 2
    else:
        offset = 0

    def y(val):
        return margin['top'] + plot_h - (val / max_val) * plot_h

    def x_bar(i, which):
        return margin['left'] + offset + i * group_width + (bar_width if which == 'lost' else 0)

    parts = [
        f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg" '
        f'style="max-width:100%;height:auto;">',
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
    ]

    # Y-axis grid lines
    for frac in [0, 0.25, 0.5, 0.75, 1.0]:
        gy = margin['top'] + plot_h - frac * plot_h
        val = int(frac * max_val)
        parts.append(
            f'<line x1="{margin["left"]}" y1="{gy:.0f}" '
            f'x2="{width - margin["right"]}" y2="{gy:.0f}" '
            f'stroke="#e0e0e0" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{margin["left"] - 8}" y="{gy + 4:.0f}" '
            f'text-anchor="end" font-size="12" fill="#666">{val}</text>'
        )

    # Bars
    for i, s in enumerate(series):
        new_v = s.get('new_backlinks', 0)
        lost_v = s.get('lost_backlinks', 0)

        # New backlinks bar (green)
        if new_v > 0:
            h = (new_v / max_val) * plot_h
            parts.append(
                f'<rect x="{x_bar(i, "new"):.0f}" y="{y(new_v):.0f}" '
                f'width="{bar_width}" height="{h:.0f}" fill="#27ae60" '
                f'rx="2"><title>New: {new_v} on {s["date"]}</title></rect>'
            )

        # Lost backlinks bar (red)
        if lost_v > 0:
            h = (lost_v / max_val) * plot_h
            parts.append(
                f'<rect x="{x_bar(i, "lost"):.0f}" y="{y(lost_v):.0f}" '
                f'width="{bar_width}" height="{h:.0f}" fill="#e74c3c" '
                f'rx="2"><title>Lost: {lost_v} on {s["date"]}</title></rect>'
            )

        # X-axis label
        label_x = margin['left'] + offset + i * group_width + bar_width
        parts.append(
            f'<text x="{label_x:.0f}" y="{height - margin["bottom"] + 20}" '
            f'text-anchor="middle" font-size="11" fill="#666">'
            f'{s["date"][:7]}</text>'
        )

    # Competitor line (lost backlinks) if provided
    if competitor_series and len(competitor_series) > 1:
        comp_pts = []
        for i, s in enumerate(competitor_series):
            cx = margin['left'] + offset + i * group_width + bar_width
            cy = y(s.get('lost_backlinks', 0))
            comp_pts.append(f'{cx:.0f},{cy:.0f}')
        if len(comp_pts) >= 2:
            parts.append(
                f'<polyline points="{" ".join(comp_pts)}" '
                f'fill="none" stroke="#f39c12" stroke-width="2" '
                f'stroke-dasharray="5,3"><title>Competitor lost backlinks</title></polyline>'
            )

    # Axes
    parts.append(
        f'<line x1="{margin["left"]}" y1="{margin["top"]}" '
        f'x2="{margin["left"]}" y2="{margin["top"] + plot_h}" '
        f'stroke="#333" stroke-width="1.5"/>'
    )
    parts.append(
        f'<line x1="{margin["left"]}" y1="{margin["top"] + plot_h}" '
        f'x2="{width - margin["right"]}" y2="{margin["top"] + plot_h}" '
        f'stroke="#333" stroke-width="1.5"/>'
    )

    # Legend
    ly = margin['top'] + 5
    parts.append(f'<rect x="{width - margin["right"] + 10}" y="{ly}" width="14" height="14" fill="#27ae60" rx="2"/>')
    parts.append(f'<text x="{width - margin["right"] + 30}" y="{ly + 12}" font-size="13" fill="#333">New Backlinks</text>')
    parts.append(f'<rect x="{width - margin["right"] + 10}" y="{ly + 22}" width="14" height="14" fill="#e74c3c" rx="2"/>')
    parts.append(f'<text x="{width - margin["right"] + 30}" y="{ly + 34}" font-size="13" fill="#333">Lost Backlinks</text>')
    if competitor_series:
        parts.append(f'<line x1="{width - margin["right"] + 10}" y1="{ly + 49}" x2="{width - margin["right"] + 24}" y2="{ly + 49}" stroke="#f39c12" stroke-width="2" stroke-dasharray="5,3"/>')
        parts.append(f'<text x="{width - margin["right"] + 30}" y="{ly + 53}" font-size="13" fill="#333">Competitor Lost</text>')

    parts.append('</svg>')
    return '\n'.join(parts)


def _summary_card(label, value, color='#1a1a1a', bg='#ffffff', border='#e0e0e0'):
    """Generate an HTML summary card."""
    return f"""    <div class="summary-card" style="background:{bg};border:1px solid {border};">
      <div class="summary-label">{html_escape(label)}</div>
      <div class="summary-value" style="color:{color};">{html_escape(str(value))}</div>
    </div>"""


def _email_template(domain_from, target_url, anchor, brand_domain):
    """Generate an outreach email template for a lost backlink."""
    return f"""Subject: Quick question about the link to {brand_domain} on your site

Hi there,

I noticed that the link to {brand_domain} on your page ({domain_from}) is no longer working. The link previously pointed to:

{target_url}

It looks like the page on our end may have been moved or removed. I'd be happy to help you find the right replacement page so your readers aren't sent to a broken link.

The original anchor text was: "{anchor}"

Could you let me know if you'd like the updated URL? I can also provide a redirect if that's easier.

Thanks for your time,
[Your Name]
[Your Brand]"""


def generate_recovery_report(domain, timeseries, lost_links, output_path,
                              competitor=None, competitor_timeseries=None):
    """Generate an HTML report showing lost backlink analysis.

    Args:
        domain: the target domain
        timeseries: list of dicts from fetch_backlink_timeseries
        lost_links: list of dicts from identify_lost_backlinks (with classification)
        output_path: directory to write the HTML report
        competitor: optional competitor domain name
        competitor_timeseries: optional timeseries for competitor

    Returns:
        Path to the generated HTML file.
    """
    os.makedirs(output_path, exist_ok=True)
    report_file = os.path.join(output_path, 'lost_backlink_recovery_report.html')

    # Aggregate stats
    total_lost = sum(s.get('lost_backlinks', 0) for s in timeseries)
    total_new = sum(s.get('new_backlinks', 0) for s in timeseries)
    net_change = total_new - total_lost
    recovery_count = sum(1 for ll in lost_links if ll.get('classification', {}).get('recoverable'))
    high_value_count = sum(1 for ll in lost_links if ll.get('domain_strength', 0) > 50)

    # Sort lost links by domain strength
    sorted_links = sorted(lost_links, key=lambda x: x.get('domain_strength', 0), reverse=True)

    # Build SVG chart
    svg_chart = _svg_timeline_chart(timeseries, competitor_timeseries)

    # Build summary cards
    cards_html = []
    cards_html.append(_summary_card('Total Lost Backlinks', f'{total_lost:,}', color='#e74c3c'))
    cards_html.append(_summary_card('Total New Backlinks', f'{total_new:,}', color='#27ae60'))
    net_color = '#e74c3c' if net_change < 0 else '#27ae60'
    cards_html.append(_summary_card('Net Change', f'{net_change:+,}', color=net_color))
    cards_html.append(_summary_card('Recovery Opportunities', f'{recovery_count}', color='#f39c12'))
    cards_html.append(_summary_card('High-Value Lost Links', f'{high_value_count}', color='#0066cc'))

    # Build lost backlinks table rows
    table_rows = []
    for i, ll in enumerate(sorted_links, 1):
        cls = ll.get('classification', {})
        reason = cls.get('reason', 'Unknown')
        detail = cls.get('detail', '')
        recoverable = cls.get('recoverable', False)
        ds = ll.get('domain_strength', 0)
        is_high_value = ds > 50

        row_class = 'high-value' if is_high_value else ''
        reason_badge_class = 'badge-recoverable' if recoverable else 'badge-unrecoverable'
        reason_text = f'<span class="badge {reason_badge_class}">{html_escape(reason)}</span>'

        domain_from = ll.get('domain_from', '')
        source_url = ll.get('source_url', '')
        target_url = ll.get('target_url', '')
        anchor = ll.get('anchor', '') or '—'
        lost_date = ll.get('lost_date', '—')
        dofollow = 'DoFollow' if ll.get('dofollow', True) else 'NoFollow'

        # Expandable details
        detail_id = f"detail-{i}"
        email_tmpl = _email_template(domain_from, target_url, anchor, domain)
        email_escaped = html_escape(email_tmpl)

        table_rows.append(f"""
        <tr class="{row_class}">
          <td class="num">{i}</td>
          <td class="domain">
            <a href="https://{html_escape(domain_from)}" target="_blank" rel="noopener noindex nofollow">{html_escape(domain_from)}</a>
            {'⭐' if is_high_value else ''}
          </td>
          <td class="url-cell">
            <a href="{html_escape(source_url)}" target="_blank" rel="noopener noindex nofollow">{html_escape(source_url[:80])}</a>
          </td>
          <td class="anchor">{html_escape(anchor[:50])}</td>
          <td class="num">{ds}</td>
          <td class="num">{html_escape(lost_date)}</td>
          <td>{reason_text}</td>
          <td>
            <details>
              <summary class="details-toggle">Details</summary>
              <div class="detail-content">
                <p style="margin:8px 0;font-size:14px;color:#555;">{html_escape(detail)}</p>
                <p style="margin:4px 0;font-size:14px;"><strong>Target URL:</strong> <a href="{html_escape(target_url)}" target="_blank" rel="noopener noindex nofollow">{html_escape(target_url[:80])}</a></p>
                <p style="margin:4px 0;font-size:14px;"><strong>Link Type:</strong> {dofollow}</p>
                <p style="margin:4px 0;font-size:14px;"><strong>Recoverable:</strong> {'Yes' if recoverable else 'No'}</p>
                <details>
                  <summary class="details-toggle" style="margin-top:8px;">Email Template</summary>
                  <pre class="email-template">{email_escaped}</pre>
                </details>
              </div>
            </details>
          </td>
        </tr>""")

    # Recovery recommendations
    recommendations = []
    recoverable_links = [ll for ll in sorted_links
                          if ll.get('classification', {}).get('recoverable')]
    for i, ll in enumerate(recoverable_links[:10], 1):
        ds = ll.get('domain_strength', 0)
        reason = ll.get('classification', {}).get('reason', '')
        domain_from = ll.get('domain_from', '')
        source_url = ll.get('source_url', '')
        target_url = ll.get('target_url', '')
        anchor = ll.get('anchor', '') or 'N/A'
        email_tmpl = _email_template(domain_from, target_url, anchor, domain)
        email_escaped = html_escape(email_tmpl)

        recommendations.append(f"""
        <div class="rec-card">
          <div class="rec-rank">#{i}</div>
          <div class="rec-body">
            <div class="rec-domain">
              <a href="https://{html_escape(domain_from)}" target="_blank" rel="noopener noindex nofollow">{html_escape(domain_from)}</a>
              {('⭐ High Value' if ds > 50 else '')}
            </div>
            <div class="rec-meta">
              Domain Strength: <strong>{ds}</strong> ·
              Reason: <strong>{html_escape(reason)}</strong> ·
              Lost Date: <strong>{html_escape(ll.get('lost_date', '—'))}</strong>
            </div>
            <div class="rec-anchor">Anchor: &quot;{html_escape(anchor[:80])}&quot;</div>
            <div class="rec-url"><a href="{html_escape(source_url)}" target="_blank" rel="noopener noindex nofollow">{html_escape(source_url[:120])}</a></div>
            <details>
              <summary class="details-toggle">Outreach Email Template</summary>
              <pre class="email-template">{email_escaped}</pre>
            </details>
          </div>
        </div>""")

    # Competitor comparison section
    competitor_html = ''
    if competitor and competitor_timeseries:
        comp_total_lost = sum(s.get('lost_backlinks', 0) for s in competitor_timeseries)
        comp_total_new = sum(s.get('new_backlinks', 0) for s in competitor_timeseries)
        comp_net = comp_total_new - comp_total_lost

        comp_cards = []
        comp_cards.append(_summary_card(f'{competitor} Lost', f'{comp_total_lost:,}', color='#e74c3c'))
        comp_cards.append(_summary_card(f'{competitor} New', f'{comp_total_new:,}', color='#27ae60'))
        comp_net_color = '#e74c3c' if comp_net < 0 else '#27ae60'
        comp_cards.append(_summary_card(f'{competitor} Net', f'{comp_net:+,}', color=comp_net_color))

        # Comparison table
        comp_rows = []
        for i in range(max(len(timeseries), len(competitor_timeseries))):
            ts = timeseries[i] if i < len(timeseries) else {}
            cs = competitor_timeseries[i] if i < len(competitor_timeseries) else {}
            comp_rows.append(f"""
            <tr>
              <td>{html_escape(ts.get('date', '—'))}</td>
              <td class="num">{ts.get('backlinks', 0):,}</td>
              <td class="num">{ts.get('new_backlinks', 0):,}</td>
              <td class="num">{ts.get('lost_backlinks', 0):,}</td>
              <td class="num">{cs.get('backlinks', 0):,}</td>
              <td class="num">{cs.get('new_backlinks', 0):,}</td>
              <td class="num">{cs.get('lost_backlinks', 0):,}</td>
            </tr>""")

        competitor_html = f"""
    <h2 class="section-title">Competitor Comparison: {html_escape(domain)} vs {html_escape(competitor)}</h2>
    <div class="cards">{''.join(comp_cards)}</div>
    <details open>
      <summary class="details-toggle section-toggle">Timeline Comparison Table</summary>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Date</th>
              <th>{html_escape(domain)} Backlinks</th>
              <th>{html_escape(domain)} New</th>
              <th>{html_escape(domain)} Lost</th>
              <th>{html_escape(competitor)} Backlinks</th>
              <th>{html_escape(competitor)} New</th>
              <th>{html_escape(competitor)} Lost</th>
            </tr>
          </thead>
          <tbody>{''.join(comp_rows)}</tbody>
        </table>
      </div>
    </details>"""

    # Generate full HTML
    gen_date = datetime.now().strftime('%Y-%m-%d %H:%M')
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<title>Lost Backlink Recovery Report: {html_escape(domain)}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
    background: {BG_COLOR};
    color: {TEXT_COLOR};
    font-size: {BODY_FONT_SIZE};
    line-height: 1.6;
    padding: 20px 40px;
    max-width: 1200px;
    margin: 0 auto;
  }}
  a {{ color: {LINK_COLOR}; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  h1 {{
    font-size: 28px;
    font-weight: 700;
    color: {TEXT_COLOR};
    margin-bottom: 4px;
  }}
  .subtitle {{
    color: #666;
    font-size: 15px;
    margin-bottom: 8px;
  }}
  .generated {{
    color: #999;
    font-size: 13px;
    margin-bottom: 30px;
  }}

  /* Summary cards */
  .cards {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 16px;
    margin-bottom: 36px;
  }}
  .summary-card {{
    border-radius: 10px;
    padding: 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05);
  }}
  .summary-label {{
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #666;
    margin-bottom: 8px;
  }}
  .summary-value {{
    font-size: 32px;
    font-weight: 700;
  }}

  /* Section titles */
  .section-title {{
    font-size: {SUBTITLE_FONT_SIZE};
    font-weight: 700;
    color: {TEXT_COLOR};
    margin: 36px 0 16px;
    padding-bottom: 8px;
    border-bottom: 2px solid #e0e0e0;
  }}

  /* SVG chart */
  .chart-container {{
    background: {BG_COLOR};
    border: 1px solid #e0e0e0;
    border-radius: 10px;
    padding: 20px;
    margin-bottom: 36px;
    overflow-x: auto;
  }}

  /* Table */
  .table-wrap {{
    overflow-x: auto;
    margin-bottom: 36px;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 14px;
    background: {BG_COLOR};
  }}
  thead th {{
    background: #f5f5f5;
    color: {TEXT_COLOR};
    padding: 12px 10px;
    text-align: left;
    border-bottom: 2px solid #e0e0e0;
    font-weight: 600;
    font-size: 13px;
  }}
  tbody td {{
    padding: 10px;
    border-bottom: 1px solid #eee;
    color: {TEXT_COLOR};
  }}
  tr.high-value {{
    background: #fff8e1;
  }}
  tr.high-value td {{
    border-bottom: 1px solid #ffe082;
  }}
  tr:hover td {{ background: #f8f9fa; }}
  tr.high-value:hover td {{ background: #fff3c4; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  td.domain {{ font-weight: 600; }}
  td.url-cell {{ max-width: 250px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  td.anchor {{ max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #555; }}

  /* Badges */
  .badge {{
    display: inline-block;
    padding: 3px 10px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 600;
    white-space: nowrap;
  }}
  .badge-recoverable {{ background: #e3f2fd; color: #1565c0; }}
  .badge-unrecoverable {{ background: #fbe9e7; color: #c62828; }}

  /* Details/expandable */
  .details-toggle {{
    cursor: pointer;
    color: {LINK_COLOR};
    font-size: 14px;
    font-weight: 600;
    padding: 4px 0;
    list-style: none;
  }}
  .details-toggle:hover {{ text-decoration: underline; }}
  .section-toggle {{
    font-size: 16px;
    margin-bottom: 12px;
  }}
  .detail-content {{
    padding: 8px 0;
    font-size: 14px;
  }}
  .email-template {{
    background: #f5f5f5;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    padding: 16px;
    font-size: 13px;
    white-space: pre-wrap;
    margin-top: 8px;
    font-family: 'SF Mono', 'Monaco', 'Consolas', monospace;
    color: {TEXT_COLOR};
  }}

  /* Recommendation cards */
  .rec-card {{
    display: flex;
    gap: 16px;
    background: {BG_COLOR};
    border: 1px solid #e0e0e0;
    border-radius: 10px;
    padding: 20px;
    margin-bottom: 16px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05);
  }}
  .rec-rank {{
    font-size: 28px;
    font-weight: 700;
    color: {LINK_COLOR};
    min-width: 40px;
  }}
  .rec-body {{ flex: 1; }}
  .rec-domain {{
    font-size: 17px;
    font-weight: 600;
    margin-bottom: 4px;
    color: {TEXT_COLOR};
  }}
  .rec-meta {{
    font-size: 14px;
    color: #555;
    margin-bottom: 6px;
  }}
  .rec-anchor {{
    font-size: 14px;
    color: #666;
    margin-bottom: 4px;
  }}
  .rec-url {{
    font-size: 13px;
    margin-bottom: 8px;
    word-break: break-all;
  }}

  /* Footer */
  .footer {{
    margin-top: 40px;
    padding-top: 20px;
    border-top: 1px solid #e0e0e0;
    color: #999;
    font-size: 13px;
  }}
</style>
</head>
<body>

<h1>Lost Backlink Recovery Report</h1>
<div class="subtitle">Domain: <strong>{html_escape(domain)}</strong>
{(' · Competitor: <strong>' + html_escape(competitor) + '</strong>') if competitor else ''}</div>
<div class="generated">Generated: {gen_date}</div>

<h2 class="section-title">Summary</h2>
<div class="cards">
{''.join(cards_html)}
</div>

<h2 class="section-title">Backlink Timeline (New vs Lost)</h2>
<div class="chart-container">
{svg_chart}
</div>

{competitor_html}

<h2 class="section-title">Lost Backlinks ({len(sorted_links)} total)</h2>
<details open>
  <summary class="details-toggle section-toggle">Show/Hide Lost Backlinks Table</summary>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>Domain</th>
          <th>Source URL</th>
          <th>Anchor Text</th>
          <th>Domain Strength</th>
          <th>Lost Date</th>
          <th>Reason</th>
          <th>Details</th>
        </tr>
      </thead>
      <tbody>
{''.join(table_rows) if table_rows else '<tr><td colspan="8" style="text-align:center;padding:30px;color:#666;">No lost backlinks found in this period.</td></tr>'}
      </tbody>
    </table>
  </div>
</details>

<h2 class="section-title">Recovery Recommendations</h2>
{''.join(recommendations) if recommendations else '<p style="font-size:16px;color:#666;">No recoverable lost backlinks found. All lost links are due to page removals or domain expiration.</p>'}

<div class="footer">
  <p>Report generated by Zenfusion Lost Backlink Recovery Tool</p>
  <p>Data source: DataForSEO Backlinks API · Generated: {gen_date}</p>
</div>

</body>
</html>"""

    with open(report_file, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f"  Report written to: {report_file}")
    return report_file


# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Track lost backlinks and generate recovery recommendations.'
    )
    parser.add_argument('--domain', required=True,
                        help='Target domain to analyze')
    parser.add_argument('--competitor', default=None,
                        help='Optional competitor domain for benchmarking')
    parser.add_argument('--output', default=DEFAULT_OUTPUT_DIR,
                        help='Output directory for the report')
    parser.add_argument('--days-back', type=int, default=90,
                        help='Days of history for timeseries (default: 90)')
    parser.add_argument('--lost-days', type=int, default=30,
                        help='Days to look back for lost backlinks (default: 30)')
    parser.add_argument('--skip-classification', action='store_true',
                        help='Skip HTTP classification (faster, less detail)')
    args = parser.parse_args()

    domain = args.domain
    print(f"\n{'='*60}")
    print(f"  Lost Backlink Recovery: {domain}")
    print(f"{'='*60}\n")

    # 1. Fetch timeseries
    print("Step 1: Fetching backlink timeseries…")
    timeseries = fetch_backlink_timeseries(domain, days_back=args.days_back)
    print(f"  Got {len(timeseries)} time points")

    # 1b. Competitor timeseries
    competitor_ts = None
    if args.competitor:
        print(f"\nStep 1b: Fetching competitor timeseries ({args.competitor})…")
        competitor_ts = fetch_backlink_timeseries(args.competitor, days_back=args.days_back)
        print(f"  Got {len(competitor_ts)} time points")

    # 2. Identify lost backlinks
    print(f"\nStep 2: Identifying lost/broken backlinks…")
    lost_links = identify_lost_backlinks(domain, days_back=args.lost_days)
    print(f"  Found {len(lost_links)} lost/broken backlinks")

    # 3. Classify loss reasons
    if lost_links and args.skip_classification:
        # Lightweight classification from API data only (no HTTP checks)
        print(f"\nStep 3: Quick classification from API data (no HTTP checks)…")
        for ll in lost_links:
            ll['classification'] = classify_loss_reason_api_only(ll)
        print(f"  Classification complete ({len(lost_links)} links)")
    elif lost_links:
        print(f"\nStep 3: Classifying loss reasons (HTTP checks, top 50 by strength)…")
        # Only HTTP-check top 50 links by domain strength to stay within time budget
        sorted_for_cls = sorted(lost_links,
                                key=lambda x: x.get('domain_strength', 0), reverse=True)
        for i, ll in enumerate(sorted_for_cls[:50]):
            cls = classify_loss_reason(ll)
            ll['classification'] = cls
            if (i + 1) % 10 == 0:
                print(f"    Classified {i + 1}/50…")
        # Remaining links get API-only classification
        for ll in sorted_for_cls[50:]:
            ll['classification'] = classify_loss_reason_api_only(ll)
        print(f"  Classification complete ({len(lost_links)} links)")
    else:
        print(f"\nStep 3: No lost backlinks to classify.")

    # 4. Generate report
    print(f"\nStep 4: Generating recovery report…")
    report_path = generate_recovery_report(
        domain, timeseries, lost_links, args.output,
        competitor=args.competitor,
        competitor_timeseries=competitor_ts,
    )

    # Print summary
    total_lost = sum(s.get('lost_backlinks', 0) for s in timeseries)
    total_new = sum(s.get('new_backlinks', 0) for s in timeseries)
    recoverable = sum(1 for ll in lost_links if ll.get('classification', {}).get('recoverable'))

    print(f"\n{'='*60}")
    print(f"  SUMMARY")
    print(f"{'='*60}")
    print(f"  Domain:           {domain}")
    print(f"  Total Lost:       {total_lost:,}")
    print(f"  Total New:        {total_new:,}")
    print(f"  Net Change:       {total_new - total_lost:+,}")
    print(f"  Broken Backlinks: {len(lost_links)}")
    print(f"  Recoverable:      {recoverable}")
    if args.competitor:
        comp_lost = sum(s.get('lost_backlinks', 0) for s in (competitor_ts or []))
        print(f"  Competitor Lost:  {comp_lost:,} ({args.competitor})")
    print(f"  Report:           {report_path}")
    print(f"{'='*60}\n")

    return report_path


if __name__ == '__main__':
    main()
