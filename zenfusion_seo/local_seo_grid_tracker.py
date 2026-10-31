#!/usr/bin/env python3
"""
Local SEO Grid Tracker — Zenfusion Hermes Agent
Tracks local search rankings across a geographic grid (Local Falcon / SEMrush
Map Rank Tracker style).

Builds a grid of lat/lng points around a business, fetches the local SERP at each
point via DataForSEO, records where the target business ranks, and emits an HTML
report with an SVG heatmap, competitor analysis, and recommendations.

Functions
---------
  fetch_local_serp(keyword, latitude, longitude, radius=1000)
  run_grid_scan(keyword, center_lat, center_lng, grid_size=5, step_km=1)
  compute_grid_metrics(grid_results, business_name)
  generate_grid_report(keyword, business_name, grid_results, output_path)

Usage
-----
  python3 local_seo_grid_tracker.py \
      --keyword 'water softener' \
      --business 'SoftPro Water Systems' \
      --lat 40.7128 --lng -74.0060 \
      --output /tmp/test_local_grid

Environment
-----------
  DATAFORSEO_LOGIN    - API login email  (in ~/.hermes/.env)
  DATAFORSEO_PASSWORD - API password    (in ~/.hermes/.env)
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
import math
import base64
import urllib.request
import urllib.error
import argparse
import datetime
import html
from collections import Counter

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def get_creds():
    """Load DataForSEO credentials from env or ~/.hermes/.env."""
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
        return None, None
    return login, password


def _api_call(endpoint, payload, timeout=60):
    """POST a payload to a DataForSEO v3 endpoint. Returns parsed JSON or None."""
    login, password = get_creds()
    if not login:
        return {'_error': 'No DataForSEO credentials found in env or ~/.hermes/.env'}

    creds = base64.b64encode(f'{login}:{password}'.encode()).decode()
    url = f'https://api.dataforseo.com/v3/{endpoint}'
    headers = {
        'Authorization': f'Basic {creds}',
        'Content-Type': 'application/json',
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _fix_goto(json.loads(resp.read()), endpoint)
    except urllib.error.HTTPError as e:
        body = ''
        try:
            body = e.read().decode('utf-8', 'ignore')[:500]
        except Exception:
            pass
        return {'_error': f'HTTP {e.code} {e.reason}: {body}'}
    except Exception as e:
        return {'_error': f'Request error: {e}'}


# ---------------------------------------------------------------------------
# 1. fetch_local_serp
# ---------------------------------------------------------------------------

def fetch_local_serp(keyword, latitude, longitude, radius=1000):
    """Fetch local business results from DataForSEO for a single lat/lng point.

    Calls serp/google/organic/live/advanced with local_latitude/local_longitude/
    local_radius, then optionally serp/google/maps/live/advanced for richer
    Maps results. Returns a dict:

        {
          "organic": [ {rank, name, domain, title, url, rating, ...}, ... ],
          "maps":    [ {rank, name, domain, rating, ...}, ... ],
          "raw_status": {...},
          "errors": [str, ...]
        }

    Gracefully handles missing creds, API errors, and unavailable endpoints —
    any failing source contributes an error string and an empty list, so the
    caller can still produce a partial grid.
    """
    out = {'organic': [], 'maps': [], 'raw_status': {}, 'errors': []}

    # --- Organic / live / advanced with local params ----------------------
    organic_payload = [{
        'keyword': keyword,
        'language_code': 'en',
        'location_code': 2840,  # United States (local params override geo)
        'device': 'desktop',
        'os': 'windows',
        'local_latitude': round(float(latitude), 6),
        'local_longitude': round(float(longitude), 6),
        'local_radius': int(radius),
    }]
    org = _api_call('serp/google/organic/live/advanced', organic_payload)
    if org is None or '_error' in org:
        out['errors'].append(f"organic: {org.get('_error', 'no data') if org else 'no response'}")
    else:
        out['raw_status']['organic_status'] = org.get('status_code')
        tasks = org.get('tasks') or []
        task0 = tasks[0] if tasks and isinstance(tasks[0], dict) else {}
        if task0.get('status_code') != 20000:
            out['errors'].append(
                f"organic task: {task0.get('status_code')} "
                f"{task0.get('status_message', '')}"
            )
        else:
            results = task0.get('result') or []
            if results and isinstance(results[0], dict):
                items = results[0].get('items', []) or []
                rank_seen = 0
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    t = it.get('type', '')
                    # Organic results + local_pack items + local_pack listings
                    if t in ('organic', 'local_pack', 'local_pack_listing',
                             'local_pack_item', 'map_results'):
                        rank_seen += 1
                        out['organic'].append({
                            'rank': it.get('rank_group') or it.get('rank_absolute') or rank_seen,
                            'name': it.get('title') or it.get('name') or '',
                            'domain': it.get('domain', ''),
                            'url': it.get('url', ''),
                            'title': it.get('title', ''),
                            'rating': it.get('rating', {}).get('value') if isinstance(it.get('rating'), dict) else it.get('rating'),
                            'type': t,
                        })

    # --- Maps / live / advanced (best-effort, may not be on the plan) -----
    maps_payload = [{
        'keyword': keyword,
        'language_code': 'en',
        'location_coordinate': f'{latitude},{longitude}',
        'device': 'desktop',
    }]
    try:
        mp = _api_call('serp/google/maps/live/advanced', maps_payload, timeout=30)
    except Exception as e:
        mp = {'_error': str(e)}
    if mp is None or '_error' in mp:
        out['errors'].append(f"maps: {mp.get('_error', 'no data') if mp else 'no response'}")
    else:
        out['raw_status']['maps_status'] = mp.get('status_code')
        tasks = mp.get('tasks') or []
        task0 = tasks[0] if tasks and isinstance(tasks[0], dict) else {}
        if task0.get('status_code') != 20000:
            out['errors'].append(
                f"maps task: {task0.get('status_code')} "
                f"{task0.get('status_message', '')}"
            )
        else:
            results = task0.get('result') or []
            if results and isinstance(results[0], dict):
                items = results[0].get('items', []) or []
                for i, it in enumerate(items, 1):
                    if not isinstance(it, dict):
                        continue
                    out['maps'].append({
                        'rank': it.get('rank_group') or it.get('rank_absolute') or i,
                        'name': it.get('title') or it.get('name') or '',
                        'domain': it.get('domain', ''),
                        'rating': it.get('rating', {}).get('value') if isinstance(it.get('rating'), dict) else it.get('rating'),
                        'address': it.get('address', ''),
                        'type': it.get('type', 'maps'),
                    })

    return out


# ---------------------------------------------------------------------------
# 2. run_grid_scan
# ---------------------------------------------------------------------------

def _grid_points(center_lat, center_lng, grid_size=5, step_km=1.0):
    """Yield (lat, lng) tuples for a grid_size x grid_size grid centered on
    the business, each point step_km apart."""
    half = (grid_size - 1) / 2.0
    km_per_deg_lat = 111.32
    km_per_deg_lng = 111.32 * math.cos(math.radians(center_lat))
    for r in range(grid_size):
        for c in range(grid_size):
            dlat = (r - half) * step_km / km_per_deg_lat
            dlng = (c - half) * step_km / max(km_per_deg_lng, 1e-6)
            yield round(center_lat + dlat, 6), round(center_lng + dlng, 6)


def run_grid_scan(keyword, center_lat, center_lng, grid_size=5, step_km=1):
    """Scan a grid of lat/lng points, fetching local SERP at each.

    Returns a list of dicts (one per grid point):
        {
          "grid_index": int,
          "lat": float, "lng": float,
          "serp": <fetch_local_serp output>,
          "errors": [str, ...],
        }
    """
    points = list(_grid_points(center_lat, center_lng, grid_size, step_km))
    results = []
    for i, (lat, lng) in enumerate(points, 1):
        print(f'  [{i}/{len(points)}] ({lat}, {lng}) ...', flush=True)
        serp = fetch_local_serp(keyword, lat, lng)
        results.append({
            'grid_index': i,
            'lat': lat,
            'lng': lng,
            'serp': serp,
            'errors': serp.get('errors', []),
        })
    return results


# ---------------------------------------------------------------------------
# 3. compute_grid_metrics
# ---------------------------------------------------------------------------

def _rank_of_business(serp_point, business_name):
    """Return the 1-based rank of `business_name` at this grid point, or None
    if not found. Case-insensitive substring match on name. Prefers maps, then
    organic."""
    bn = business_name.lower().strip()
    if not bn:
        return None
    serp = serp_point.get('serp', {})
    for src in ('maps', 'organic'):
        for item in serp.get(src, []) or []:
            name = (item.get('name') or '').lower()
            if bn in name or name in bn:
                try:
                    return int(item.get('rank') or 0) or None
                except (TypeError, ValueError):
                    return None
    return None


def compute_grid_metrics(grid_results, business_name):
    """Compute per-point ranks and aggregate metrics for the grid.

    Returns:
        {
          "per_point": [ {grid_index, lat, lng, rank, found}, ... ],
          "average_rank": float,            # among found points
          "top3_coverage": float,          # 0..1
          "top10_coverage": float,         # 0..1
          "total_points": int,
          "found_count": int,
          "not_found_count": int,
          "rank_distribution": {str:int},   # "1","2","3",...,"11-20","not_found"
          "heatmap": [rank_or_None, ...],  # row-major, len = total_points
          "competitor_frequency": [ {name, count, avg_rank}, ... ],
        }
    """
    total = len(grid_results)
    per_point = []
    ranks_found = []
    top3 = 0
    top10 = 0
    dist = Counter()
    heatmap = []
    competitor_counter = Counter()
    competitor_ranks = {}

    for pt in grid_results:
        rank = _rank_of_business(pt, business_name)
        found = rank is not None
        per_point.append({
            'grid_index': pt['grid_index'],
            'lat': pt['lat'],
            'lng': pt['lng'],
            'rank': rank,
            'found': found,
        })
        heatmap.append(rank)
        if found:
            ranks_found.append(rank)
            if rank <= 3:
                top3 += 1
            if rank <= 10:
                top10 += 1
            if rank == 1:
                dist['1'] += 1
            elif rank <= 3:
                dist['2-3'] += 1
            elif rank <= 10:
                dist['4-10'] += 1
            else:
                dist['11-20'] += 1
        else:
            dist['not_found'] += 1

        # Competitor tracking: count other businesses that appeared here
        serp = pt.get('serp', {})
        for src in ('maps', 'organic'):
            for item in serp.get(src, []) or []:
                name = (item.get('name') or '').strip()
                if not name:
                    continue
                if business_name and business_name.lower() in name.lower():
                    continue
                competitor_counter[name] += 1
                try:
                    r = int(item.get('rank') or 0)
                    if r:
                        competitor_ranks.setdefault(name, []).append(r)
                except (TypeError, ValueError):
                    pass

    found_count = len(ranks_found)
    avg_rank = round(sum(ranks_found) / found_count, 2) if ranks_found else None

    comp_freq = []
    for name, count in competitor_counter.most_common(15):
        rs = competitor_ranks.get(name, [])
        comp_freq.append({
            'name': name,
            'count': count,
            'avg_rank': round(sum(rs) / len(rs), 2) if rs else None,
        })

    return {
        'per_point': per_point,
        'average_rank': avg_rank,
        'top3_coverage': round(top3 / total, 4) if total else 0,
        'top10_coverage': round(top10 / total, 4) if total else 0,
        'total_points': total,
        'found_count': found_count,
        'not_found_count': total - found_count,
        'rank_distribution': dict(dist),
        'heatmap': heatmap,
        'competitor_frequency': comp_freq,
    }


# ---------------------------------------------------------------------------
# 4. generate_grid_report
# ---------------------------------------------------------------------------

def _rank_color(rank):
    """Map a rank (or None) to a heatmap color."""
    if rank is None:
        return '#e74c3c'          # red — not found
    if rank == 1:
        return '#27ae60'          # green — #1
    if rank <= 3:
        return '#2ecc71'          # light green
    if rank <= 10:
        return '#f1c40f'          # yellow
    return '#e67e22'              # orange — 11-20


def _rank_label(rank):
    if rank is None:
        return 'Not found'
    return f'#{rank}'


def generate_grid_report(keyword, business_name, grid_results, output_path):
    """Write an HTML grid report to `output_path` (a directory; the .html file
    is written inside it). Returns the absolute path of the written report."""
    metrics = compute_grid_metrics(grid_results, business_name)
    os.makedirs(output_path, exist_ok=True)
    safe_kw = ''.join(ch if ch.isalnum() or ch in '-_' else '_' for ch in keyword)
    fname = f'local_grid_{safe_kw}_{datetime.datetime.now():%Y%m%d_%H%M%S}.html'
    fpath = os.path.join(output_path, fname)

    # SVG heatmap ----------------------------------------------------------
    grid_size = int(math.sqrt(len(grid_results))) or 5
    cell = 70
    pad = 10
    svg_w = grid_size * cell + pad * 2
    svg_h = grid_size * cell + pad * 2 + 30
    svg_cells = []
    for idx, rank in enumerate(metrics['heatmap']):
        r = idx // grid_size
        c = idx % grid_size
        x = pad + c * cell
        y = pad + r * cell
        color = _rank_color(rank)
        label = _rank_label(rank)
        short_label = '#' + str(rank) if rank else '—'
        svg_cells.append(
            f'<g class="cell" data-idx="{idx + 1}">'
            f'<rect x="{x}" y="{y}" width="{cell - 4}" height="{cell - 4}" '
            f'rx="6" fill="{color}" stroke="#fff" stroke-width="2"/>'
            f'<text x="{x + (cell - 4)/2}" y="{y + (cell - 4)/2 - 4}" '
            f'text-anchor="middle" font-size="11" fill="#fff" font-weight="700">'
            f'{idx + 1}</text>'
            f'<text x="{x + (cell - 4)/2}" y="{y + (cell - 4)/2 + 12}" '
            f'text-anchor="middle" font-size="13" fill="#fff" font-weight="700">'
            f'{short_label}</text>'
            f'<title>Grid #{idx + 1}: {label}</title>'
            f'</g>'
        )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{svg_w}" height="{svg_h}" '
        f'viewBox="0 0 {svg_w} {svg_h}">'
        f'<text x="{svg_w/2}" y="18" text-anchor="middle" font-size="13" '
        f'font-weight="700" fill="#2c3e50">Rank Heatmap — "{html.escape(keyword)}"</text>'
        + ''.join(svg_cells) +
        '</svg>'
    )

    # Per-point table rows -------------------------------------------------
    rows_html = []
    for pt in metrics['per_point']:
        rank = pt['rank']
        found = pt['found']
        rows_html.append(
            '<tr>'
            f'<td>{pt["grid_index"]}</td>'
            f'<td>{pt["lat"]:.5f}</td>'
            f'<td>{pt["lng"]:.5f}</td>'
            f'<td style="color:{_rank_color(rank)};font-weight:700">{_rank_label(rank)}</td>'
            f'<td>{"✓" if found else "✗"}</td>'
            '</tr>'
        )

    # Competitor analysis --------------------------------------------------
    comp_rows = []
    for comp in metrics['competitor_frequency'][:10]:
        comp_rows.append(
            '<tr>'
            f'<td>{html.escape(comp["name"])}</td>'
            f'<td>{comp["count"]}</td>'
            f'<td>{comp["avg_rank"] if comp["avg_rank"] is not None else "—"}</td>'
            '</tr>'
        )

    # Recommendations -------------------------------------------------------
    recs = _recommendations(metrics, business_name)

    # Summary cards --------------------------------------------------------
    avg = metrics['average_rank']
    avg_disp = f'{avg}' if avg is not None else 'N/A'
    top3_pct = metrics['top3_coverage'] * 100
    top10_pct = metrics['top10_coverage'] * 100
    not_found = metrics['not_found_count']
    total = metrics['total_points']
    cards = (
        f'<div class="card"><div class="card-label">Keyword</div>'
        f'<div class="card-value">{html.escape(keyword)}</div></div>'
        f'<div class="card"><div class="card-label">Business</div>'
        f'<div class="card-value">{html.escape(business_name)}</div></div>'
        f'<div class="card"><div class="card-label">Grid Size</div>'
        f'<div class="card-value">{grid_size}×{grid_size} ({total})</div></div>'
        f'<div class="card"><div class="card-label">Average Rank</div>'
        f'<div class="card-value">{avg_disp}</div></div>'
        f'<div class="card"><div class="card-label">Top 3 Coverage</div>'
        f'<div class="card-value">{top3_pct:.0f}%</div></div>'
        f'<div class="card"><div class="card-label">Top 10 Coverage</div>'
        f'<div class="card-value">{top10_pct:.0f}%</div></div>'
        f'<div class="card"><div class="card-label">Not Found</div>'
        f'<div class="card-value">{not_found}/{total}</div></div>'
    )

    # Error notice if any ---------------------------------------------------
    errors = []
    for pt in grid_results:
        for e in pt.get('errors', []):
            if e not in errors:
                errors.append(e)
    error_block = ''
    if errors:
        error_block = (
            '<div class="error-box"><strong>Note:</strong> Some DataForSEO '
            'endpoints returned errors and were skipped gracefully. The grid '
            'still reflects available data; affected points are marked '
            '"Not found".<ul>' +
            ''.join(f'<li>{html.escape(e)}</li>' for e in errors[:8]) +
            '</ul></div>'
        )

    # Distribution bars ----------------------------------------------------
    dist = metrics['rank_distribution']
    dist_order = ['1', '2-3', '4-10', '11-20', 'not_found']
    dist_labels = {'1': '#1', '2-3': '#2-3', '4-10': '#4-10',
                   '11-20': '#11-20', 'not_found': 'Not found'}
    dist_colors = {'1': '#27ae60', '2-3': '#2ecc71', '4-10': '#f1c40f',
                   '11-20': '#e67e22', 'not_found': '#e74c3c'}
    max_d = max((dist.get(k, 0) for k in dist_order), default=1) or 1
    dist_bars = ''.join(
        f'<div class="dist-row"><span class="dist-label">{dist_labels[k]}</span>'
        f'<div class="dist-track"><div class="dist-fill" '
        f'style="width:{(dist.get(k,0)/max_d*100):.0f}%;background:{dist_colors[k]}">'
        f'</div></div><span class="dist-count">{dist.get(k,0)}</span></div>'
        for k in dist_order
    )

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="robots" content="noindex,nofollow">
<title>Local SEO Grid Tracker — {html.escape(keyword)}</title>
<style>
  :root {{
    --bg:#f7f9fc; --card:#ffffff; --ink:#1f2d3d; --muted:#6b7c93;
    --accent:#2e7be6; --line:#e3e8ef; --shadow:0 1px 3px rgba(0,0,0,.08);
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:32px; background:var(--bg); color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  .sub {{ color:var(--muted); font-size:13px; margin-bottom:24px; }}
  .cards {{ display:flex; flex-wrap:wrap; gap:12px; margin-bottom:28px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
    padding:14px 18px; box-shadow:var(--shadow); min-width:130px; flex:1 1 130px; }}
  .card-label {{ font-size:11px; text-transform:uppercase; letter-spacing:.06em;
    color:var(--muted); margin-bottom:6px; }}
  .card-value {{ font-size:20px; font-weight:700; }}
  .grid-wrap {{ display:flex; gap:32px; flex-wrap:wrap; margin-bottom:28px; }}
  .heatmap-box {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
    padding:18px; box-shadow:var(--shadow); }}
  .legend {{ margin-top:10px; display:flex; gap:14px; font-size:12px; color:var(--muted); }}
  .legend span {{ display:inline-flex; align-items:center; gap:6px; }}
  .legend i {{ width:12px; height:12px; border-radius:3px; display:inline-block; }}
  section {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
    padding:20px; box-shadow:var(--shadow); margin-bottom:24px; }}
  h2 {{ font-size:16px; margin:0 0 14px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th,td {{ text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); }}
  th {{ font-size:11px; text-transform:uppercase; color:var(--muted); }}
  details summary {{ cursor:pointer; color:var(--accent); font-weight:600;
    padding:6px 0; }}
  .dist-row {{ display:flex; align-items:center; gap:10px; margin:6px 0; }}
  .dist-label {{ width:70px; font-size:12px; color:var(--muted); }}
  .dist-track {{ flex:1; background:#eef2f7; border-radius:6px; height:18px; overflow:hidden; }}
  .dist-fill {{ height:100%; border-radius:6px; }}
  .dist-count {{ width:30px; text-align:right; font-size:12px; font-weight:700; }}
  .error-box {{ background:#fff7e6; border:1px solid #ffd591; border-radius:8px;
    padding:12px 16px; margin-bottom:20px; font-size:13px; color:#874d00; }}
  .error-box ul {{ margin:6px 0 0 18px; padding:0; }}
  ul.recs {{ margin:0; padding-left:20px; }}
  ul.recs li {{ margin:6px 0; }}
  .footer {{ color:var(--muted); font-size:11px; margin-top:24px; }}
</style>
</head>
<body>
  <h1>Local SEO Grid Tracker</h1>
  <div class="sub">Generated {datetime.datetime.now():%Y-%m-%d %H:%M:%S} — keyword: <strong>{html.escape(keyword)}</strong> · business: <strong>{html.escape(business_name)}</strong></div>

  {error_block}

  <div class="cards">{cards}</div>

  <div class="grid-wrap">
    <div class="heatmap-box">
      {svg}
      <div class="legend">
        <span><i style="background:#27ae60"></i>#1</span>
        <span><i style="background:#2ecc71"></i>#2-3</span>
        <span><i style="background:#f1c40f"></i>#4-10</span>
        <span><i style="background:#e67e22"></i>#11-20</span>
        <span><i style="background:#e74c3c"></i>Not found</span>
      </div>
    </div>
    <section style="flex:1; min-width:280px; margin:0">
      <h2>Rank Distribution</h2>
      {dist_bars}
    </section>
  </div>

  <section>
    <h2>Grid Data</h2>
    <details open>
      <summary>Show all {total} grid points</summary>
      <table>
        <thead><tr><th>#</th><th>Latitude</th><th>Longitude</th><th>Rank</th><th>Found</th></tr></thead>
        <tbody>{''.join(rows_html)}</tbody>
      </table>
    </details>
  </section>

  <section>
    <h2>Competitor Analysis</h2>
    {('<table><thead><tr><th>Competitor</th><th>Appearances</th><th>Avg Rank</th></tr></thead>'
      '<tbody>' + ''.join(comp_rows) + '</tbody></table>')
      if comp_rows
      else '<p class="sub">No competitor data available (all SERP calls errored or returned no listings).</p>'}
  </section>

  <section>
    <h2>Local SEO Recommendations</h2>
    <ul class="recs">
      {''.join(f'<li>{html.escape(r)}</li>' for r in recs)}
    </ul>
  </section>

  <div class="footer">Zenfusion Local SEO Grid Tracker · DataForSEO serp/google/organic &amp; maps live/advanced · noindex,nofollow</div>
</body>
</html>"""

    with open(fpath, 'w', encoding='utf-8') as f:
        f.write(html_doc)
    return os.path.abspath(fpath)


def _recommendations(metrics, business_name):
    """Generate plain-text local SEO recommendations from the metrics."""
    recs = []
    total = metrics['total_points']
    found = metrics['found_count']
    top3 = metrics['top3_coverage']
    top10 = metrics['top10_coverage']
    avg = metrics['average_rank']
    not_found = metrics['not_found_count']

    if total == 0:
        return ['No grid points were scanned — cannot generate recommendations.']

    if found == 0:
        recs.append(
            f'"{business_name}" was not found in any of the {total} grid points. '
            'Verify the Google Business Profile is published, the business name '
            'matches exactly (including suffixes), and the keyword is one the '
            'business actually competes for locally.'
        )
    else:
        if avg is not None and avg <= 3:
            recs.append(
                f'Strong showing — average rank #{avg} across the grid. Focus on '
                'defending edge grid points where rank slips to maintain full '
                'coverage.'
            )
        elif avg is not None and avg <= 10:
            recs.append(
                f'Average rank #{avg}. Improve by adding the keyword to GBP '
                'primary category, services, and posts; collect recent reviews '
                'mentioning the keyword; and build location landing pages for '
                'neighborhoods in weak grid points.'
            )
        elif avg is not None:
            recs.append(
                f'Average rank #{avg} is outside the local pack. Prioritize '
                'GBP optimization (complete every field, add photos weekly, '
                'post updates), review velocity with keyword mentions, and '
                'citations/landing pages for the specific neighborhoods in the '
                'weakest grid points.'
            )

    if top3 < 0.25 and found > 0:
        recs.append(
            f'Top-3 coverage is only {top3*100:.0f}%. The local pack (top 3) '
            'drives most calls — concentrate link and review building on the '
            'grid points closest to the business first.'
        )
    if top10 < 0.5:
        recs.append(
            f'Top-10 coverage is {top10*100:.0f}%. Points where the business is '
            'absent usually sit far from the business address — add '
            'neighborhood/area landing pages and service-area pages to expand '
            'relevance radius.'
        )
    if not_found > total * 0.4:
        recs.append(
            f'{not_found}/{total} grid points have no ranking. Increase '
            'review cadence, ensure GBP categories match the keyword, and '
            'create city/neighborhood landing pages to widen the relevance '
            'radius.'
        )

    # Competitor-driven recs
    top_comp = metrics['competitor_frequency'][:3]
    if top_comp:
        names = ', '.join(c['name'] for c in top_comp)
        recs.append(
            f'Top competitors appearing most often: {names}. Audit their GBP '
            'profiles, categories, review count, and photo cadence to find '
            'quick gaps to close.'
        )

    recs.append(
        'Re-scan this grid weekly or after any major GBP change (new reviews, '
        'category change, posts) to track movement and prove ROI to the client.'
    )
    return recs


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description='Track local search rankings on a geographic grid.'
    )
    p.add_argument('--keyword', required=True)
    p.add_argument('--business', required=True, help='Target business name to track')
    p.add_argument('--lat', type=float, required=True, help='Center latitude')
    p.add_argument('--lng', type=float, required=True, help='Center longitude')
    p.add_argument('--grid-size', type=int, default=5, help='Grid dimension (default 5 = 5x5)')
    p.add_argument('--step-km', type=float, default=1.0, help='km between grid points')
    p.add_argument('--radius', type=int, default=1000, help='local_radius in meters')
    p.add_argument('--output', default='/tmp/local_grid_report', help='Output directory')
    args = p.parse_args()

    login, _ = get_creds()
    if not login:
        print('WARNING: No DataForSEO credentials found. The script will still '
              'build a grid and report, but all SERP calls will be marked "not '
              'found". Add DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD to '
              '~/.hermes/.env for live data.')

    print(f'Scanning {args.grid_size}x{args.grid_size} grid '
          f'(step={args.step_km} km) for "{args.keyword}" '
          f'around ({args.lat}, {args.lng}) ...')
    grid = run_grid_scan(
        args.keyword, args.lat, args.lng,
        grid_size=args.grid_size, step_km=args.step_km,
    )
    metrics = compute_grid_metrics(grid, args.business)
    print(f'Done. Found in {metrics["found_count"]}/{metrics["total_points"]} '
          f'grid points. Avg rank: '
          f'{metrics["average_rank"] if metrics["average_rank"] is not None else "N/A"}')

    report = generate_grid_report(
        args.keyword, args.business, grid, args.output
    )
    print(f'Report written: {report}')
    # Emit JSON summary for machine callers
    print(json.dumps({
        'total_points': metrics['total_points'],
        'found_count': metrics['found_count'],
        'average_rank': metrics['average_rank'],
        'top3_coverage': metrics['top3_coverage'],
        'top10_coverage': metrics['top10_coverage'],
        'report_path': report,
    }))


if __name__ == '__main__':
    main()
