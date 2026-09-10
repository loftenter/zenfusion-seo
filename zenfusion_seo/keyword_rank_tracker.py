#!/usr/bin/env python3
"""
Keyword Rank Tracker — standalone Python script.

Tracks keyword rankings over time with position history and delta alerts.
For each keyword, calls the DataForSEO SERP Google Organic Live Advanced
endpoint, finds where target_domain ranks in the organic results, and stores
a timestamped snapshot to ~/.hermes/audits/rank-tracking/{domain}_rankings.json
so re-runs show position changes (deltas) from the previous run.

Functions:
  1. fetch_rankings(keywords, target_domain, location_code=2840)
  2. save_ranking_snapshot(rankings, domain)
  3. compute_deltas(current, previous)
  4. generate_ranking_report(domain, keywords, current, previous, output_path)

Usage:
  python3 keyword_rank_tracker.py \
      --domain softprowatersystems.com \
      --keywords 'best water softener,water softener,water softener system' \
      --output /tmp/test_rank_tracker

Outputs:
  - <output>/ranking_report.html  (white SaaS theme HTML report)
  - ~/.hermes/audits/rank-tracking/{domain}_rankings.json (history JSON)
  - console summary

Environment:
  DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD — in ~/.hermes/.env or env vars
"""

import os
import sys
import json
import base64
import argparse
import urllib.request
import urllib.error
from datetime import datetime
from urllib.parse import urlsplit

# ── Config ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOCATION_CODE = 2840  # United States
DEFAULT_LANGUAGE_CODE = 'en'
API_BASE = 'https://api.dataforseo.com/v3'
API_TIMEOUT = 90
HISTORY_DIR = os.path.expanduser('~/.hermes/audits/rank-tracking')

# SERP item types that count as "features" beyond standard organic results.
SERP_FEATURE_MAP = {
    'ai_overview': 'AI Overview',
    'ai_organic_overview': 'AI Overview',
    'ai_review_overview': 'AI Overview',
    'people_also_ask': 'People Also Ask',
    'featured_snippet': 'Featured Snippet',
    'knowledge_graph': 'Knowledge Graph',
    'local_pack': 'Local Pack',
    'video': 'Video',
    'videos': 'Video',
    'image': 'Image',
    'images': 'Image',
    'news': 'News',
    'top_stories': 'Top Stories',
    'shopping': 'Shopping',
    'twitter': 'Twitter/X',
    'carousel': 'Carousel',
}


# ── Credential management ─────────────────────────────────────────────────────

def get_creds():
    """Load DataForSEO credentials from env vars or ~/.hermes/.env."""
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
        print("ERROR: DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD not found.")
        print("Set them in ~/.hermes/.env or as environment variables.")
        sys.exit(1)
    return login, password


def api_call(endpoint, payload):
    """POST to a DataForSEO v3 endpoint. Returns parsed JSON or None."""
    login, password = get_creds()
    creds = base64.b64encode(f'{login}:{password}'.encode()).decode()
    url = f'{API_BASE}/{endpoint}'
    headers = {
        'Authorization': f'Basic {creds}',
        'Content-Type': 'application/json',
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode() if e.fp else ''
        print(f"  API HTTP {e.code} on {endpoint}: {err_body[:200]}")
        return None
    except Exception as e:
        print(f"  API request error on {endpoint}: {e}")
        return None
    if body.get('status_code') != 20000:
        print(f"  API error {body.get('status_code')}: {body.get('status_message', '')}")
        for task in body.get('tasks', []):
            if task.get('status_code') not in (20000, None):
                print(f"    task: {task.get('status_code')} - {task.get('status_message', '')}")
        return None
    return body


# ── URL / domain helpers ───────────────────────────────────────────────────────

def normalize_host(host):
    host = (host or "").lower().strip()
    return host[4:] if host.startswith("www.") else host


def host_of(url):
    try:
        return normalize_host(urlsplit(url if "//" in url else "http://" + url).netloc)
    except Exception:
        return ""


def domain_matches(url, domain, include_subdomains=True):
    """Return True if url belongs to domain (or a subdomain)."""
    host = normalize_host(host_of(url))
    dom = normalize_host(domain)
    if not host or not dom:
        return False
    return host == dom or (include_subdomains and host.endswith("." + dom))


def _escape(text):
    if text is None:
        return ""
    return (str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;').replace("'", '&#39;'))


# ── Function 1: fetch_rankings ────────────────────────────────────────────────

def fetch_rankings(keywords, target_domain, location_code=2840):
    """
    For each keyword, call DataForSEO SERP Google Organic Live Advanced,
    find where target_domain ranks in the organic results.

    Returns a list of dicts:
      {keyword, position, url, title, best_position_found,
       search_volume, cpc, serp_features}
    position is None when the domain is not found in the top 100.
    """
    rankings = []
    target_domain = normalize_host(target_domain)
    total = len(keywords)
    for idx, kw in enumerate(keywords, 1):
        print(f"  [{idx}/{total}] '{kw}' ...", flush=True)
        rec = _fetch_one_ranking(kw, target_domain, location_code)
        rankings.append(rec)
    return rankings


def _fetch_one_ranking(keyword, target_domain, location_code, language_code='en'):
    """Fetch live SERP for a single keyword and locate the target domain."""
    payload = [{
        'keyword': keyword,
        'location_code': location_code,
        'language_code': language_code,
        'device': 'desktop',
        'os': 'windows',
        'depth': 100,
        'use_improved_etv': True,
    }]
    data = api_call('serp/google/organic/live/advanced', payload)
    rec = {
        'keyword': keyword,
        'position': None,
        'url': None,
        'title': None,
        'best_position_found': None,
        'search_volume': None,
        'cpc': None,
        'serp_features': [],
    }
    if not data or not data.get('tasks'):
        return rec
    task = data['tasks'][0]
    if task.get('status_code') not in (20000, None):
        print(f"    SERP task error for '{keyword}': {task.get('status_code')} - {task.get('status_message', '')}")
        return rec
    result = task.get('result', [{}])[0] if task.get('result') else {}
    items = result.get('items', [])

    # Keyword info (search volume / CPC) comes from result.keyword_info
    kw_info = result.get('keyword_info', {}) or {}
    rec['search_volume'] = kw_info.get('search_volume')
    rec['cpc'] = kw_info.get('cpc')

    # Detect SERP features present (across all items, not just organic)
    features_seen = set()
    for it in items:
        itype = it.get('type', '')
        if itype and itype != 'organic' and itype in SERP_FEATURE_MAP:
            features_seen.add(SERP_FEATURE_MAP[itype])
    rec['serp_features'] = sorted(features_seen)

    # Find the target domain's organic result (best/highest position)
    best_pos = None
    best_item = None
    for it in items:
        itype = it.get('type', '')
        if itype not in ('organic', 'featured_snippet'):
            continue
        url = it.get('url') or ''
        if not domain_matches(url, target_domain):
            continue
        pos = it.get('rank_group')
        if pos is None:
            pos = it.get('rank_absolute')
        if pos is None:
            continue
        if best_pos is None or pos < best_pos:
            best_pos = int(pos)
            best_item = it
    rec['best_position_found'] = best_pos
    if best_item is not None:
        rec['position'] = best_pos
        rec['url'] = best_item.get('url')
        rec['title'] = best_item.get('title')
    return rec


# ── Function 2: save_ranking_snapshot ─────────────────────────────────────────

def save_ranking_snapshot(rankings, domain):
    """
    Save current rankings to ~/.hermes/audits/rank-tracking/{domain}_rankings.json.

    The file stores a list of snapshots; each snapshot is:
      {timestamp, rankings: [{keyword, position, url, title, ...}]}
    This call appends a new snapshot and returns the full history list.
    """
    os.makedirs(HISTORY_DIR, exist_ok=True)
    domain_clean = normalize_host(domain)
    path = os.path.join(HISTORY_DIR, f'{domain_clean}_rankings.json')
    timestamp = datetime.now().isoformat(timespec='seconds')

    history = []
    if os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as f:
                history = json.load(f)
            if not isinstance(history, list):
                history = [history] if isinstance(history, dict) else []
        except Exception:
            history = []

    snapshot = {
        'timestamp': timestamp,
        'domain': domain_clean,
        'rankings': rankings,
    }
    history.append(snapshot)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(history, f, indent=2, ensure_ascii=False, default=str)
    print(f"  Snapshot saved: {path} ({len(history)} total snapshots)")
    return history


def load_previous_snapshot(domain):
    """Load the most recent snapshot from history (excluding current run if needed)."""
    domain_clean = normalize_host(domain)
    path = os.path.join(HISTORY_DIR, f'{domain_clean}_rankings.json')
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            history = json.load(f)
    except Exception:
        return None
    if not isinstance(history, list) or not history:
        return None
    return history[-1]


def best_position_ever(domain, keyword):
    """Look up the best (lowest) position ever recorded for a keyword across history."""
    domain_clean = normalize_host(domain)
    path = os.path.join(HISTORY_DIR, f'{domain_clean}_rankings.json')
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            history = json.load(f)
    except Exception:
        return None
    best = None
    for snap in history:
        for r in snap.get('rankings', []):
            if r.get('keyword') == keyword:
                p = r.get('position')
                if p is not None and (best is None or p < best):
                    best = p
    return best


# ── Function 3: compute_deltas ────────────────────────────────────────────────

def compute_deltas(current, previous):
    """
    Compare current rankings to the previous snapshot.

    Each input is either a snapshot dict {timestamp, rankings:[...]} or a
    list of ranking dicts. Returns a dict keyed by keyword:
      {keyword: {current_pos, previous_pos, delta, status}}
    status ∈ improved | declined | new | dropped | unchanged | stable
    delta = previous_pos - current_pos (positive = improved, negative = declined)
    """
    def _rankings_of(snap):
        if snap is None:
            return []
        if isinstance(snap, list):
            return snap
        if isinstance(snap, dict):
            return snap.get('rankings', [])
        return []

    cur_map = {}
    for r in _rankings_of(current):
        cur_map[r['keyword']] = r.get('position')

    prev_map = {}
    for r in _rankings_of(previous):
        prev_map[r['keyword']] = r.get('position')

    deltas = {}
    all_kws = set(cur_map) | set(prev_map)
    for kw in all_kws:
        c = cur_map.get(kw)
        p = prev_map.get(kw)
        if c is not None and p is None:
            status = 'new'
            delta = None
        elif c is None and p is not None:
            status = 'dropped'
            delta = None
        elif c is None and p is None:
            status = 'stable'
            delta = 0
        else:
            delta = p - c  # positive = improved (lower position number)
            if delta > 0:
                status = 'improved'
            elif delta < 0:
                status = 'declined'
            else:
                status = 'unchanged'
        deltas[kw] = {
            'current_pos': c,
            'previous_pos': p,
            'delta': delta,
            'status': status,
        }
    return deltas


# ── Position distribution helpers ─────────────────────────────────────────────

POSITION_BUCKETS = [
    ('1', lambda p: p == 1),
    ('2-3', lambda p: 2 <= p <= 3),
    ('4-10', lambda p: 4 <= p <= 10),
    ('11-20', lambda p: 11 <= p <= 20),
    ('21-50', lambda p: 21 <= p <= 50),
    ('51-100', lambda p: 51 <= p <= 100),
    ('Not ranking', lambda p: p is None),
]

BUCKET_COLORS = {
    '1': '#10b981',
    '2-3': '#22c55e',
    '4-10': '#84cc16',
    '11-20': '#f59e0b',
    '21-50': '#f97316',
    '51-100': '#ef4444',
    'Not ranking': '#94a3b8',
}


def position_bucket(pos):
    if pos is None:
        return 'Not ranking'
    for label, test in POSITION_BUCKETS:
        if test(pos):
            return label
    return 'Not ranking'


def position_distribution(rankings):
    """Return list of (bucket_label, count)."""
    buckets = {label: 0 for label, _ in POSITION_BUCKETS}
    for r in rankings:
        pos = r.get('position')
        buckets[position_bucket(pos)] += 1
    return [(label, buckets[label]) for label, _ in POSITION_BUCKETS]


# ── Function 4: generate_ranking_report ───────────────────────────────────────

def generate_ranking_report(domain, keywords, current, previous, output_path):
    """
    Generate an HTML ranking report with:
      - Summary cards
      - Position change table (expandable)
      - Top movers section
      - New/dropped keywords
      - Position distribution SVG chart
      - SERP features check
      - Recommendations
    Writes <output_path>/ranking_report.html and returns the path.
    """
    os.makedirs(output_path, exist_ok=True)
    domain_clean = normalize_host(domain)
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')

    cur_rankings = current if isinstance(current, list) else (current or {}).get('rankings', [])
    deltas = compute_deltas(current, previous)

    # Build a lookup from current rankings
    cur_by_kw = {r['keyword']: r for r in cur_rankings}

    # Summary metrics
    total_tracked = len(cur_rankings)
    ranking_count = sum(1 for r in cur_rankings if r.get('position') is not None)
    top10 = sum(1 for r in cur_rankings if r.get('position') is not None and r['position'] <= 10)
    top3 = sum(1 for r in cur_rankings if r.get('position') is not None and r['position'] <= 3)
    improved = sum(1 for d in deltas.values() if d['status'] == 'improved')
    declined = sum(1 for d in deltas.values() if d['status'] == 'declined')
    new_kws = [k for k, d in deltas.items() if d['status'] == 'new' and k in cur_by_kw]
    dropped_kws = [k for k, d in deltas.items() if d['status'] == 'dropped']

    # Top movers
    movers = []
    for kw, d in deltas.items():
        if d['status'] in ('improved', 'declined') and d.get('delta') is not None:
            movers.append((kw, d['delta'], d['status'], d['current_pos'], d['previous_pos']))
    biggest_improvers = sorted([m for m in movers if m[2] == 'improved'], key=lambda x: -x[1])[:5]
    biggest_decliners = sorted([m for m in movers if m[2] == 'declined'], key=lambda x: x[1])[:5]

    # Distribution chart
    dist = position_distribution(cur_rankings)

    html = _build_html(
        domain_clean, timestamp, cur_rankings, deltas, cur_by_kw,
        total_tracked, ranking_count, top10, top3, improved, declined,
        new_kws, dropped_kws, biggest_improvers, biggest_decliners,
        dist, previous is not None,
    )
    html_path = os.path.join(output_path, 'ranking_report.html')
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html)
    return html_path


def _delta_badge(d):
    """Return HTML for a delta badge given a delta dict."""
    status = d['status']
    delta = d.get('delta')
    if status == 'improved':
        return f'<span class="badge" style="background:#10b981">▲ +{delta}</span>'
    if status == 'declined':
        return f'<span class="badge" style="background:#ef4444">▼ {delta}</span>'
    if status == 'new':
        return '<span class="badge" style="background:#3b82f6">NEW</span>'
    if status == 'dropped':
        return '<span class="badge" style="background:#6b7280">DROPPED</span>'
    if status == 'unchanged':
        return '<span class="badge" style="background:#cbd5e1;color:#475569">— 0</span>'
    return '<span class="badge" style="background:#cbd5e1;color:#475569">—</span>'


def _build_html(domain, timestamp, cur_rankings, deltas, cur_by_kw,
                total_tracked, ranking_count, top10, top3, improved, declined,
                new_kws, dropped_kws, biggest_improvers, biggest_decliners,
                dist, has_previous):
    # ── Position change table rows ──
    rows = []
    # Sort: ranking keywords first (by position asc), then non-ranking
    sorted_kws = sorted(cur_rankings, key=lambda r: (r.get('position') is None, r.get('position') or 9999))
    for r in sorted_kws:
        kw = r['keyword']
        d = deltas.get(kw, {})
        pos = r.get('position')
        prev_pos = d.get('previous_pos')
        pos_str = f"#{pos}" if pos is not None else "Not ranking"
        prev_str = f"#{prev_pos}" if prev_pos is not None else "—"
        best_ever = best_position_ever(domain, kw)
        best_ever_str = f"#{best_ever}" if best_ever is not None else "—"
        vol = r.get('search_volume')
        vol_str = f"{vol:,}" if vol else "—"
        url = r.get('url') or ''
        url_cell = (f'<a href="{_escape(url)}" target="_blank" rel="nofollow">{_escape(url)}</a>'
                   if url else '<span class="muted">—</span>')
        features = r.get('serp_features') or []
        feat_cell = ', '.join(features) if features else '<span class="muted">—</span>'
        rows.append(f"""
        <tr>
          <td class="keyword">{_escape(kw)}</td>
          <td class="pos-cell">{pos_str}</td>
          <td class="pos-cell">{prev_str}</td>
          <td>{_delta_badge(d)}</td>
          <td class="url-cell">{url_cell}</td>
          <td>{vol_str}</td>
          <td class="pos-cell">{best_ever_str}</td>
          <td class="feat-cell">{feat_cell}</td>
        </tr>""")
    rows_html = '\n'.join(rows) if rows else '<tr><td colspan="8" class="empty">No ranking data.</td></tr>'

    # ── Top movers section ──
    def _mover_rows(movers_list):
        out = []
        for kw, delta, status, cur, prev in movers_list:
            cur_s = f"#{cur}" if cur else "—"
            prev_s = f"#{prev}" if prev else "—"
            badge = _delta_badge({'status': status, 'delta': delta})
            out.append(f'<tr><td class="keyword">{_escape(kw)}</td><td>{prev_s}</td><td>{cur_s}</td><td>{badge}</td></tr>')
        return '\n'.join(out) if out else '<tr><td colspan="4" class="empty">None</td></tr>'

    improvers_html = _mover_rows(biggest_improvers)
    decliners_html = _mover_rows(biggest_decliners)

    # ── New / dropped keywords ──
    def _kw_list(items):
        if not items:
            return '<p class="muted">None</p>'
        return '<ul class="kw-list">' + ''.join(
            f'<li>{_escape(k)}</li>' for k in items) + '</ul>'
    new_html = _kw_list(new_kws)
    dropped_html = _kw_list(dropped_kws)

    # ── Position distribution SVG chart ──
    max_count = max((c for _, c in dist), default=0)
    chart_w = 720
    bar_h = 38
    gap = 12
    label_w = 110
    chart_h = len(dist) * (bar_h + gap) + 40
    bars_svg = []
    for i, (label, count) in enumerate(dist):
        y = 20 + i * (bar_h + gap)
        color = BUCKET_COLORS.get(label, '#94a3b8')
        # scale bar width
        bar_max_w = chart_w - label_w - 60
        bw = int(bar_max_w * (count / max_count)) if max_count else 0
        bars_svg.append(f"""
        <text x="0" y="{y + bar_h/2 + 4}" class="chart-label">{_escape(label)}</text>
        <rect x="{label_w}" y="{y}" width="{bw}" height="{bar_h}" rx="6" fill="{color}" opacity="0.9"/>
        <text x="{label_w + bw + 8}" y="{y + bar_h/2 + 4}" class="chart-val">{count}</text>""")
    bars_svg_str = '\n'.join(bars_svg)

    # ── SERP features check section ──
    feat_rows = []
    for r in sorted_kws:
        feats = r.get('serp_features') or []
        if not feats:
            continue
        pos = r.get('position')
        pos_str = f"#{pos}" if pos is not None else "Not ranking"
        feat_rows.append(f'<tr><td class="keyword">{_escape(r["keyword"])}</td><td class="pos-cell">{pos_str}</td><td>{", ".join(feats)}</td></tr>')
    features_html = ('\n'.join(feat_rows) if feat_rows
                     else '<tr><td colspan="3" class="empty">No SERP features detected in tracked keywords.</td></tr>')

    # ── Recommendations ──
    recs = _build_recommendations(cur_rankings, deltas, cur_by_kw, new_kws, dropped_kws)
    recs_html = ''.join(f'<li>{_escape(rec)}</li>' for rec in recs) if recs else '<li class="muted">No recommendations.</li>'

    # ── Deltas note ──
    deltas_note = ("Comparing to previous snapshot." if has_previous
                   else "First run — no previous snapshot to compare. Deltas will appear on re-run.")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex,nofollow">
<title>Keyword Rank Tracker — {domain}</title>
<style>
  :root {{
    --bg: #ffffff; --card: #ffffff; --border: #e2e8f0;
    --text: #1a1a1a; --muted: #64748b; --accent: #0066cc;
    --green: #10b981; --amber: #f59e0b; --red: #ef4444;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.6;
    font-size: 16px; padding: 24px; max-width: 1400px; margin: 0 auto;
  }}
  h1 {{ font-size: 24px; font-weight: 700; margin-bottom: 4px; }}
  h2 {{ font-size: 20px; font-weight: 700; margin-top: 28px; margin-bottom: 12px; }}
  .subtitle {{ color: var(--muted); font-size: 14px; margin-bottom: 24px; }}
  .stats-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 24px; }}
  @media (min-width: 900px) {{ .stats-grid {{ grid-template-columns: repeat(6, 1fr); }} }}
  .stat-card {{
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 18px 16px; text-align: center;
  }}
  .stat-card .num {{ font-size: 28px; font-weight: 700; }}
  .stat-card .label {{ font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-top: 4px; }}
  .stat-card.improved .num {{ color: var(--green); }}
  .stat-card.declined .num {{ color: var(--red); }}
  .stat-card.top .num {{ color: var(--accent); }}
  table {{
    width: 100%; border-collapse: collapse; background: var(--card);
    border: 1px solid var(--border); border-radius: 12px; overflow: hidden;
    font-size: 14px;
  }}
  th {{
    background: #f8fafc; padding: 10px 12px; text-align: left; font-weight: 600;
    font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em;
    color: var(--muted); border-bottom: 1px solid var(--border); white-space: nowrap;
  }}
  td {{ padding: 10px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover {{ background: #f8fafc; }}
  .badge {{
    display: inline-block; padding: 2px 8px; border-radius: 20px;
    font-size: 12px; font-weight: 600; color: #fff; white-space: nowrap;
  }}
  .keyword {{ font-weight: 600; }}
  .pos-cell {{ text-align: center; font-weight: 600; white-space: nowrap; }}
  .url-cell {{ max-width: 320px; word-break: break-all; font-size: 13px; }}
  .url-cell a {{ color: var(--accent); text-decoration: none; }}
  .url-cell a:hover {{ text-decoration: underline; }}
  .feat-cell {{ font-size: 13px; }}
  .muted {{ color: var(--muted); }}
  .empty {{ text-align: center; padding: 32px; color: var(--muted); }}
  .two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }}
  .movers-card, .nd-card {{
    background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px;
  }}
  .kw-list {{ list-style: disc inside; padding-left: 8px; font-size: 14px; }}
  .kw-list li {{ margin-bottom: 4px; }}
  details summary {{ cursor: pointer; color: var(--accent); font-weight: 600; margin-bottom: 8px; }}
  .chart-wrap {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 20px; }}
  .chart-label {{ font-size: 13px; fill: var(--muted); }}
  .chart-val {{ font-size: 13px; font-weight: 600; fill: var(--text); }}
  .recs {{ background: #f0f9ff; border: 1px solid #bae6fd; border-radius: 12px; padding: 16px 20px; }}
  .recs ul {{ padding-left: 20px; }}
  .recs li {{ margin-bottom: 6px; font-size: 14px; }}
  .footer {{ margin-top: 28px; text-align: center; font-size: 12px; color: var(--muted); }}
</style>
</head>
<body>
  <h1>📈 Keyword Rank Tracker</h1>
  <p class="subtitle">{domain} · Generated {timestamp} · Data: DataForSEO SERP Google Organic Live Advanced</p>
  <p class="subtitle">{deltas_note}</p>

  <div class="stats-grid">
    <div class="stat-card"><div class="num">{total_tracked}</div><div class="label">Total Tracked</div></div>
    <div class="stat-card top"><div class="num">{ranking_count}</div><div class="label">Ranking (Top 100)</div></div>
    <div class="stat-card top"><div class="num">{top10}</div><div class="label">In Top 10</div></div>
    <div class="stat-card top"><div class="num">{top3}</div><div class="label">In Top 3</div></div>
    <div class="stat-card improved"><div class="num">{improved}</div><div class="label">Improved</div></div>
    <div class="stat-card declined"><div class="num">{declined}</div><div class="label">Declined</div></div>
  </div>

  <h2>Position Change Table</h2>
  <details open>
    <summary>Show all {len(cur_rankings)} tracked keywords</summary>
    <table>
      <thead>
        <tr>
          <th>Keyword</th><th>Current</th><th>Previous</th><th>Delta</th>
          <th>URL</th><th>Volume</th><th>Best Ever</th><th>SERP Features</th>
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
  </details>

  <h2>Top Movers</h2>
  <div class="two-col">
    <div class="movers-card">
      <h3 style="font-size:16px;color:var(--green);margin-bottom:8px;">▲ Biggest Improvers</h3>
      <table><thead><tr><th>Keyword</th><th>Prev</th><th>Now</th><th>Change</th></tr></thead>
      <tbody>{improvers_html}</tbody></table>
    </div>
    <div class="movers-card">
      <h3 style="font-size:16px;color:var(--red);margin-bottom:8px;">▼ Biggest Decliners</h3>
      <table><thead><tr><th>Keyword</th><th>Prev</th><th>Now</th><th>Change</th></tr></thead>
      <tbody>{decliners_html}</tbody></table>
    </div>
  </div>

  <h2>New &amp; Dropped Keywords (entered/left Top 100)</h2>
  <div class="two-col">
    <div class="nd-card">
      <h3 style="font-size:16px;color:var(--accent);margin-bottom:8px;">🆕 New in Top 100</h3>
      {new_html}
    </div>
    <div class="nd-card">
      <h3 style="font-size:16px;color:var(--muted);margin-bottom:8px;">❌ Dropped from Top 100</h3>
      {dropped_html}
    </div>
  </div>

  <h2>Position Distribution</h2>
  <div class="chart-wrap">
    <svg width="{chart_w}" height="{chart_h}" xmlns="http://www.w3.org/2000/svg">
      {bars_svg_str}
    </svg>
  </div>

  <h2>SERP Features Check</h2>
  <p class="subtitle">Features (AI Overview, PAA, Featured Snippet) present in the SERP for each tracked keyword.</p>
  <table>
    <thead><tr><th>Keyword</th><th>Position</th><th>Features Present</th></tr></thead>
    <tbody>{features_html}</tbody>
  </table>

  <h2>Recommendations</h2>
  <div class="recs">
    <ul>{recs_html}</ul>
  </div>

  <div class="footer">
    Keyword Rank Tracker · {domain} · {timestamp} · Data: DataForSEO ·
    History stored at ~/.hermes/audits/rank-tracking/{domain}_rankings.json
  </div>
</body>
</html>"""


def _build_recommendations(cur_rankings, deltas, cur_by_kw, new_kws, dropped_kws):
    """Generate a list of actionable recommendation strings."""
    recs = []
    ranking = [r for r in cur_rankings if r.get('position') is not None]
    # Closest to page 1 (positions 11-20) — biggest opportunity
    close_to_p1 = sorted(
        [r for r in ranking if 11 <= r['position'] <= 20],
        key=lambda r: r['position']
    )
    for r in close_to_p1[:5]:
        vol_str = f" (vol ~{r['search_volume']:,})" if r.get('search_volume') else ""
        recs.append(
            f"Focus on '{r['keyword']}': currently #{r['position']}{vol_str} — "
            f"closest to page 1, optimize on-page + build links to push into top 10."
        )
    # Top 3 opportunities — defend / improve
    top3_kws = [r for r in ranking if r['position'] <= 3]
    for r in top3_kws:
        d = deltas.get(r['keyword'], {})
        if d.get('status') == 'declined':
            recs.append(
                f"Defend '{r['keyword']}': was #{d['previous_pos']}, now #{r['position']} — "
                f"slipping from top 3, audit the ranking page and refresh content."
            )
    # Declined keywords need attention
    declined = [(kw, d) for kw, d in deltas.items() if d['status'] == 'declined']
    for kw, d in sorted(declined, key=lambda x: x[1].get('delta') or 0)[:5]:
        r = cur_by_kw.get(kw, {})
        pos = r.get('position')
        pos_str = f" (now #{pos})" if pos else " (no longer ranking)"
        recs.append(
            f"Attention on '{kw}': dropped {abs(d['delta'])} positions"
            f" (#{d['previous_pos']} → #{pos}){pos_str} — check for algorithm updates, "
            f"content decay, or new competitors."
        )
    # New keywords entering top 100
    for kw in new_kws[:3]:
        r = cur_by_kw.get(kw, {})
        pos = r.get('position')
        recs.append(
            f"'{kw}' entered the top 100 at #{pos} — momentum opportunity, "
            f"double down with content + links to climb further."
        )
    # Dropped keywords
    for kw in dropped_kws[:3]:
        prev = deltas.get(kw, {}).get('previous_pos')
        recs.append(
            f"'{kw}' dropped out of top 100 (was #{prev}) — investigate ranking page "
            f"indexation, redirects, or lost backlinks."
        )
    # Not ranking at all
    not_ranking = [r for r in cur_rankings if r.get('position') is None]
    if not_ranking:
        names = ', '.join(r['keyword'] for r in not_ranking[:3])
        recs.append(
            f"{len(not_ranking)} keyword(s) not ranking in top 100 ({names}"
            f"{'…' if len(not_ranking) > 3 else ''}). Create or strengthen dedicated "
            f"landing pages for these terms."
        )
    if not recs:
        recs.append("All keywords stable — continue monitoring and content optimization.")
    return recs


# ── Console summary ───────────────────────────────────────────────────────────

def print_console_summary(domain, rankings, deltas, has_previous):
    domain_clean = normalize_host(domain)
    ranking = sum(1 for r in rankings if r.get('position') is not None)
    top10 = sum(1 for r in rankings if r.get('position') is not None and r['position'] <= 10)
    top3 = sum(1 for r in rankings if r.get('position') is not None and r['position'] <= 3)
    improved = sum(1 for d in deltas.values() if d['status'] == 'improved')
    declined = sum(1 for d in deltas.values() if d['status'] == 'declined')

    print(f"\n{'='*60}")
    print(f"KEYWORD RANK TRACKER — {domain_clean}")
    print(f"{'='*60}")
    print(f"Tracked: {len(rankings)} keywords · {ranking} ranking · {top10} in top 10 · {top3} in top 3")
    if has_previous:
        print(f"Deltas:   {improved} improved · {declined} declined")
    else:
        print("Deltas:   (first run — no previous snapshot)")
    print()
    print(f"  {'Keyword':<40} {'Pos':<8} {'Vol':<10} {'Best':<8}")
    print(f"  {'-'*70}")
    for r in sorted(rankings, key=lambda x: (x.get('position') is None, x.get('position') or 9999)):
        pos = f"#{r['position']}" if r.get('position') is not None else "—"
        vol = f"{r['search_volume']:,}" if r.get('search_volume') else "—"
        best = f"#{r['best_position_found']}" if r.get('best_position_found') else "—"
        print(f"  {r['keyword'][:38]:<40} {pos:<8} {vol:<10} {best:<8}")
    print(f"\n{'='*60}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Track keyword rankings over time with DataForSEO SERP data.')
    parser.add_argument('--domain', required=True, help='Target domain (e.g. softprowatersystems.com)')
    parser.add_argument('--keywords', required=True,
                        help='Comma-separated keywords, or path to a .txt/.csv file (one per line)')
    parser.add_argument('--output', required=True, help='Output directory for the HTML report')
    parser.add_argument('--location-code', type=int, default=2840, help='DataForSEO location code (default 2840=US)')
    parser.add_argument('--language', default='en', help='Language code (default en)')
    args = parser.parse_args()

    # Parse keywords
    if os.path.exists(args.keywords):
        with open(args.keywords) as f:
            keywords = [line.strip() for line in f if line.strip() and not line.startswith('#')]
    else:
        keywords = [k.strip() for k in args.keywords.split(',') if k.strip()]

    if not keywords:
        print("ERROR: No keywords provided.")
        sys.exit(1)

    print(f"\nFetching rankings for {len(keywords)} keywords on {args.domain}...\n")

    # 1. Fetch current rankings
    rankings = fetch_rankings(keywords, args.domain, location_code=args.location_code)

    # 2. Load previous snapshot (before saving current)
    previous = load_previous_snapshot(args.domain)

    # 3. Save current snapshot
    save_ranking_snapshot(rankings, args.domain)

    # 4. Compute deltas
    current_snap = {'timestamp': datetime.now().isoformat(timespec='seconds'),
                    'domain': normalize_host(args.domain), 'rankings': rankings}
    deltas = compute_deltas(current_snap, previous)

    # 5. Generate report
    report_path = generate_ranking_report(
        args.domain, keywords, current_snap, previous, args.output
    )

    # Also write raw JSON
    json_path = os.path.join(args.output, 'rankings_raw.json')
    os.makedirs(args.output, exist_ok=True)
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump({
            'domain': normalize_host(args.domain),
            'timestamp': current_snap['timestamp'],
            'rankings': rankings,
            'deltas': deltas,
            'has_previous': previous is not None,
        }, f, indent=2, ensure_ascii=False, default=str)

    # Console summary
    print_console_summary(args.domain, rankings, deltas, previous is not None)
    print(f"\nReport: {report_path}")
    print(f"JSON:   {json_path}")
    print(f"History: {os.path.join(HISTORY_DIR, normalize_host(args.domain) + '_rankings.json')}")
    print(f"\nData: DataForSEO")


if __name__ == '__main__':
    main()
