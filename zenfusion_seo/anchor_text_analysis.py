#!/usr/bin/env python3
"""
Zenfusion Anchor Text Analysis & Over-Optimization Detector

Fetches anchor text distribution for a domain via DataForSEO's
backlinks/anchors/live endpoint, classifies each anchor, computes
distribution percentages against safe thresholds, and generates an HTML
report with an SVG pie chart, expandable anchor table, risk score, and
recommendations.

Usage:
  python3 anchor_text_analysis.py --domain softprowatersystems.com \
      --brand softpro --output /tmp/test_anchor_text

DataForSEO credentials in ~/.hermes/.env:
  DATAFORSEO_LOGIN
  DATAFORSEO_PASSWORD
"""

import os
import sys
import json
import base64
import urllib.request
import argparse
import re
import html
import math
from datetime import datetime
from urllib.parse import urlparse

ENV_PATH = os.path.expanduser('~/.hermes/.env')

# ---------------------------------------------------------------------------
# Credentials & API helpers
# ---------------------------------------------------------------------------

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
        print('ERROR: DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD not found')
        print('Set them in ~/.hermes/.env or as environment variables')
        sys.exit(1)
    return login, password


def dfs_api_call(endpoint, payload, timeout=120):
    """POST to a DataForSEO v3 live endpoint. Wraps payload in an array."""
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
            print(f'  API error ({data.get("status_code")}): {err}')
            return None
        return data
    except Exception as e:
        print(f'  Request error: {e}')
        return None


# ---------------------------------------------------------------------------
# 1. fetch_anchor_text
# ---------------------------------------------------------------------------

def fetch_anchor_text(domain, limit=100):
    """Call DataForSEO backlinks/anchors/live and return all anchor texts
    with their backlink counts.

    Returns a list of dicts, each with:
        anchor (str), backlinks (int), referring_domains (int),
        dofollow (int), first_seen (str), lost_backlinks (int)
    Sorted by backlinks descending.
    """
    payload = {
        'target': domain,
        'include_subdomains': True,
        'limit': limit,
        'order_by': ['backlinks,desc'],
    }
    data = dfs_api_call('backlinks/anchors/live', payload, timeout=180)
    if not data or not data.get('tasks'):
        return []

    task = data['tasks'][0]
    if task.get('status_code') != 20000:
        print(f'  Task error: {task.get("status_message", "Unknown")}')
        return []

    result = task.get('result', [{}])[0]
    items = result.get('items', [])

    anchors = []
    for item in items:
        anchor_text = item.get('anchor', '') or ''
        # DataForSEO returns aggregate fields per anchor text
        anchors.append({
            'anchor': anchor_text,
            'backlinks': item.get('backlinks', 0) or 0,
            'referring_domains': item.get('referring_domains', 0) or 0,
            'dofollow': item.get('dofollow_links', 0)
            or item.get('dofollow', 0) or 0,
            'nofollow': item.get('nofollow_links', 0)
            or item.get('nofollow', 0) or 0,
            'first_seen': item.get('first_seen', ''),
            'lost_backlinks': item.get('lost_backlinks', 0) or 0,
        })

    # Ensure sorted by backlinks desc
    anchors.sort(key=lambda a: a['backlinks'], reverse=True)
    return anchors


# ---------------------------------------------------------------------------
# 2. classify_anchor
# ---------------------------------------------------------------------------

GENERIC_PATTERNS = [
    r'click here', r'here', r'read more', r'learn more', r'more',
    r'this link', r'this post', r'this article', r'website',
    r'link', r'source', r'view', r'check', r'see more', r'get started',
    r'contact us', r'sign up', r'join', r'continue', r'visit',
    r'read', r'blog', r'post', r'home', r'next', r'previous',
    r'info', r'details', r'watch', r'play', r'listen',
]


def _is_url(text):
    """Return True if the text looks like a bare URL."""
    t = text.strip().lower()
    if not t:
        return False
    if re.match(r'^https?://', t):
        return True
    if re.match(r'^www\.', t):
        return True
    # bare domain like example.com
    if re.match(r'^[a-z0-9.-]+\.[a-z]{2,}$', t):
        return True
    return False


def classify_anchor(text, brand_name):
    """Classify an anchor text into one of:
    exact_match, partial_match, branded, generic, naked_url, image_alt, empty

    Classification priority (checked in order):
      1. empty            - no text
      2. image_alt        - indicates an image link alt attribute
      3. naked_url        - bare URL
      4. branded          - contains brand name
      5. generic          - generic CTA phrases
      6. exact_match      - exact keyword (treated as target keyword heuristic:
                            short, non-brand text that is not generic)
      7. partial_match    - contains the target keyword as a substring
      8. other            - fallback

    Note: Without a known target keyword list, "exact_match" vs
    "partial_match" is determined using the brand name as the keyword
    discriminator when brand_name is supplied. If brand name IS in the
    text -> branded. Otherwise the text is treated as an exact-match
    keyword candidate (short, focused) or partial_match (longer).
    """
    if not text or not text.strip():
        return 'empty'

    clean = text.strip()
    low = clean.lower()

    # image_alt: DataForSEO sometimes returns "(image)" or alt-style text
    if (low.startswith('[image') or low.startswith('(image')
            or low.startswith('image:') or low == '[image]'):
        return 'image_alt'

    # naked URL
    if _is_url(clean):
        return 'naked_url'

    brand = (brand_name or '').strip().lower()
    brand_re = re.compile(r'\b' + re.escape(brand), re.I) if brand else None

    # branded: contains brand name
    if brand and brand_re.search(clean):
        # If it is ONLY the brand, treat as branded (still branded)
        return 'branded'

    # generic CTA
    for pat in GENERIC_PATTERNS:
        if re.search(r'\b' + re.escape(pat) + r'\b', low):
            return 'generic'

    # Without a keyword list, we treat short focused anchor text as
    # exact_match (the typical "money keyword" over-optimization signal)
    # and longer descriptive text as partial_match.
    word_count = len(low.split())
    if word_count <= 3:
        return 'exact_match'
    else:
        return 'partial_match'


# ---------------------------------------------------------------------------
# 3. analyze_distribution
# ---------------------------------------------------------------------------

# Safe thresholds (by % of total backlinks)
SAFE_THRESHOLDS = {
    'exact_match': 10.0,   # < 10% safe, > 20% risky
    'partial_match': 30.0,
    'branded': 40.0,        # branded should be a large share
    'generic': 20.0,
    'naked_url': 10.0,
    'image_alt': 10.0,
    'empty': 5.0,
}

RISKY_THRESHOLDS = {
    'exact_match': 20.0,   # > 20% is risky
    'partial_match': 50.0,
    'generic': 40.0,
    'naked_url': 30.0,
}


def analyze_distribution(anchors, brand_name):
    """Compute distribution percentages and compare against safe thresholds.

    Returns a dict with:
      total_backlinks, unique_anchors, top_anchor, dofollow_total,
      dofollow_ratio, distribution (type -> {count, backlinks, pct}),
      risk_score (0-100), risk_level (Safe/Monitor/At Risk/Dangerous),
      flagged (list of anchors that look spammy),
      recommendations (list of strings)
    """
    total_backlinks = sum(a['backlinks'] for a in anchors)
    dofollow_total = sum(a['dofollow'] for a in anchors)
    dofollow_ratio = (dofollow_total / total_backlinks * 100) if total_backlinks else 0

    # Classify each anchor and accumulate by type
    type_map = {
        'exact_match': {'count': 0, 'backlinks': 0},
        'partial_match': {'count': 0, 'backlinks': 0},
        'branded': {'count': 0, 'backlinks': 0},
        'generic': {'count': 0, 'backlinks': 0},
        'naked_url': {'count': 0, 'backlinks': 0},
        'image_alt': {'count': 0, 'backlinks': 0},
        'empty': {'count': 0, 'backlinks': 0},
        'other': {'count': 0, 'backlinks': 0},
    }

    classified = []
    for a in anchors:
        t = classify_anchor(a['anchor'], brand_name)
        if t not in type_map:
            t = 'other'
        type_map[t]['count'] += 1
        type_map[t]['backlinks'] += a['backlinks']
        a_copy = dict(a)
        a_copy['type'] = t
        classified.append(a_copy)

    # Compute percentages based on backlinks share (not count of unique anchors)
    distribution = {}
    for t, vals in type_map.items():
        pct = (vals['backlinks'] / total_backlinks * 100) if total_backlinks else 0
        distribution[t] = {
            'count': vals['count'],
            'backlinks': vals['backlinks'],
            'pct': round(pct, 2),
        }

    # Top anchor by backlinks
    top_anchor = classified[0]['anchor'] if classified else ''
    top_anchor_type = classified[0]['type'] if classified else 'empty'
    unique_anchors = len(set(a['anchor'] for a in anchors))

    # ---- Risk score (0-100, higher = riskier) ----
    risk_score = 0
    em_pct = distribution['exact_match']['pct']
    pm_pct = distribution['partial_match']['pct']
    br_pct = distribution['branded']['pct']
    gen_pct = distribution['generic']['pct']
    nu_pct = distribution['naked_url']['pct']

    # Exact-match over-optimization is the primary signal
    if em_pct > 20:
        risk_score += min(40, (em_pct - 20) * 2)
    elif em_pct > 10:
        risk_score += (em_pct - 10)

    # Low branded share is a secondary negative signal
    if br_pct < 20:
        risk_score += (20 - br_pct)
    elif br_pct < 40:
        risk_score += 5

    # Too few generic/natural anchors
    if gen_pct < 5:
        risk_score += 5

    # Too many naked URLs
    if nu_pct > 30:
        risk_score += (nu_pct - 30) * 0.5

    # Low diversity of anchor texts
    diversity = unique_anchors / max(len(anchors), 1)
    if total_backlinks > 0 and diversity < 0.3:
        risk_score += 10

    # Concentration: top anchor share
    top_share = (classified[0]['backlinks'] / total_backlinks * 100) if total_backlinks and classified else 0
    if top_share > 40:
        risk_score += min(15, (top_share - 40) * 0.5)

    risk_score = max(0, min(100, round(risk_score)))

    if risk_score < 15:
        risk_level = 'Safe'
    elif risk_score < 35:
        risk_level = 'Monitor'
    elif risk_score < 60:
        risk_level = 'At Risk'
    else:
        risk_level = 'Dangerous'

    # ---- Flag spammy anchors ----
    flagged = []
    for a in classified:
        flags = []
        low = a['anchor'].lower()
        # money keyword with high concentration
        if a['type'] == 'exact_match' and total_backlinks > 0 and (a['backlinks'] / total_backlinks * 100) > 10:
            flags.append('high-concentration exact match')
        # very long keyword-stuffed anchor
        if len(a['anchor'].split()) > 8 and a['type'] != 'generic':
            flags.append('keyword-stuffed (long)')
        # repeated word density
        if a['anchor']:
            words = re.findall(r'\w+', low)
            if words:
                from collections import Counter
                freq = Counter(words)
                top_word, top_count = freq.most_common(1)[0]
                if top_count > 3 and top_word not in (brand_name or '').lower().split():
                    flags.append(f'repeated word "{top_word}" x{top_count}')
        # excessive dofollow on exact_match
        if a['type'] == 'exact_match' and a['backlinks'] > 0:
            df_ratio = a['dofollow'] / a['backlinks']
            if df_ratio > 0.9 and a['backlinks'] > 5:
                flags.append(f'high dofollow ratio ({df_ratio*100:.0f}%)')
        if flags:
            flagged.append({
                'anchor': a['anchor'],
                'type': a['type'],
                'backlinks': a['backlinks'],
                'flags': flags,
            })

    # ---- Recommendations ----
    recommendations = []
    if em_pct > 20:
        recommendations.append(
            f'Reduce exact-match anchor text from {em_pct:.1f}% to below 10%. '
            'Dilute with branded and generic anchors.')
    elif em_pct > 10:
        recommendations.append(
            f'Exact-match anchors at {em_pct:.1f}% — monitor closely. '
            'Aim to keep below 10% for a natural profile.')
    else:
        recommendations.append(
            f'Exact-match anchors at {em_pct:.1f}% — within safe range.')

    if br_pct < 40:
        recommendations.append(
            f'Increase branded anchor text from {br_pct:.1f}% toward 40-60%. '
            'Branded anchors are the strongest natural signal.')
    else:
        recommendations.append(
            f'Branded anchors at {br_pct:.1f}% — healthy branded share.')

    if gen_pct < 10:
        recommendations.append(
            f'Add more generic anchors (currently {gen_pct:.1f}%). '
            'Natural profiles include "click here", "read more", etc.')
    if nu_pct < 5 and total_backlinks > 0:
        recommendations.append(
            f'Consider more naked-URL anchors (currently {nu_pct:.1f}%). '
            'A natural profile has some bare-URL links.')
    if diversity < 0.3 and total_backlinks > 0:
        recommendations.append(
            f'Anchor text diversity is low ({unique_anchors} unique / '
            f'{len(anchors)} returned). Build a wider variety of anchor texts.')
    if top_share > 40:
        recommendations.append(
            f'Top anchor "{top_anchor[:40]}" accounts for {top_share:.1f}% '
            'of backlinks — diversify to reduce concentration risk.')
    if not recommendations:
        recommendations.append('Anchor text profile looks natural and balanced.')

    return {
        'total_backlinks': total_backlinks,
        'unique_anchors': unique_anchors,
        'total_anchor_texts': len(anchors),
        'top_anchor': top_anchor,
        'top_anchor_type': top_anchor_type,
        'top_anchor_share': round(top_share, 2),
        'dofollow_total': dofollow_total,
        'dofollow_ratio': round(dofollow_ratio, 2),
        'distribution': distribution,
        'risk_score': risk_score,
        'risk_level': risk_level,
        'flagged': flagged,
        'recommendations': recommendations,
        'classified': classified,
    }


# ---------------------------------------------------------------------------
# 4. generate_anchor_report
# ---------------------------------------------------------------------------

PIE_COLORS = {
    'exact_match': '#ef4444',
    'partial_match': '#f59e0b',
    'branded': '#10b981',
    'generic': '#3b82f6',
    'naked_url': '#8b5cf6',
    'image_alt': '#ec4899',
    'empty': '#9ca3af',
    'other': '#6b7280',
}


def _svg_pie_chart(distribution, size=260):
    """Generate an SVG pie chart from distribution percentages."""
    cx, cy, r = size / 2, size / 2, size / 2 - 10
    total_pct = sum(d['pct'] for d in distribution.values())
    if total_pct <= 0:
        return '<p class="muted">No distribution data.</p>'

    angle = -90.0  # start at top
    slices = []
    legend = []
    for label, vals in distribution.items():
        pct = vals['pct']
        if pct <= 0:
            continue
        color = PIE_COLORS.get(label, '#6b7280')
        sweep = (pct / total_pct) * 360.0
        # SVG arc
        start_a = math.radians(angle)
        end_a = math.radians(angle + sweep)
        x1 = cx + r * math.cos(start_a)
        y1 = cy + r * math.sin(start_a)
        x2 = cx + r * math.cos(end_a)
        y2 = cy + r * math.sin(end_a)
        large = 1 if sweep > 180 else 0
        path = (f'M {cx:.1f},{cy:.1f} L {x1:.1f},{y1:.1f} '
                f'A {r:.1f},{r:.1f} 0 {large} 1 {x2:.1f},{y2:.1f} Z')
        slices.append(
            f'<path d="{path}" fill="{color}" stroke="#fff" stroke-width="1.5"/>')
        legend.append(
            f'<div class="legend-item"><span class="legend-swatch" '
            f'style="background:{color}"></span>'
            f'<span class="legend-label">{label.replace("_", " ").title()}</span>'
            f'<span class="legend-pct">{pct:.1f}%</span></div>')
        angle += sweep

    svg = (
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" '
        f'xmlns="http://www.w3.org/2000/svg">'
        + ''.join(slices)
        + f'<circle cx="{cx}" cy="{cy}" r="{r*0.55}" fill="#fff"/>'
        + f'<text x="{cx}" y="{cy-4}" text-anchor="middle" '
          'font-size="13" font-weight="700" fill="#111827">Anchor</text>'
        + f'<text x="{cx}" y="{cy+12}" text-anchor="middle" '
          'font-size="13" font-weight="700" fill="#111827">Text</text>'
        + '</svg>'
    )
    legend_html = '<div class="legend">' + ''.join(legend) + '</div>'
    return f'<div class="pie-wrap">{svg}{legend_html}</div>'


def _risk_badge(level):
    colors = {
        'Safe': '#10b981',
        'Monitor': '#f59e0b',
        'At Risk': '#f97316',
        'Dangerous': '#ef4444',
    }
    c = colors.get(level, '#6b7280')
    return (f'<span class="risk-badge" style="background:{c}20;color:{c};'
            f'border:1px solid {c}">{level}</span>')


def generate_anchor_report(domain, anchors, analysis, output_path):
    """Generate the HTML anchor text analysis report.

    output_path: directory path; the report is written to
    output_path/anchor_text_report_<domain>_<timestamp>.html
    Returns the absolute path to the generated HTML file.
    """
    os.makedirs(output_path, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    safe_domain = re.sub(r'[^a-z0-9.]', '_', domain.lower())
    filename = f'anchor_text_report_{safe_domain}_{ts}.html'
    filepath = os.path.join(output_path, filename)

    dist = analysis['distribution']
    pie = _svg_pie_chart(dist)

    # Overview cards
    total_anchors = analysis['total_backlinks']
    unique = analysis['unique_anchors']
    top_anchor_disp = html.escape(analysis['top_anchor'][:60] or '—')
    dofollow_ratio = analysis['dofollow_ratio']

    # Risk score gauge (0-100)
    rs = analysis['risk_score']
    risk_color = '#10b981' if rs < 15 else '#f59e0b' if rs < 35 else '#f97316' if rs < 60 else '#ef4444'

    # Classified anchors table rows
    classified = analysis.get('classified', [])
    total_bl = analysis['total_backlinks'] or 1
    rows_html = []
    for a in classified:
        pct = (a['backlinks'] / total_bl) * 100
        anchor_disp = html.escape(a['anchor'][:80] or '(empty)')
        type_badge_color = PIE_COLORS.get(a['type'], '#6b7280')
        is_flagged = any(f['anchor'] == a['anchor'] for f in analysis['flagged'])
        flag_icon = ' <span title="Flagged" style="color:#ef4444">⚠</span>' if is_flagged else ''
        rows_html.append(
            f'<tr class="anchor-row" data-type="{a["type"]}">'
            f'<td class="anchor-text">{anchor_disp}{flag_icon}</td>'
            f'<td><span class="type-badge" style="background:{type_badge_color}20;'
            f'color:{type_badge_color}">{a["type"].replace("_", " ")}</span></td>'
            f'<td class="num">{a["backlinks"]:,}</td>'
            f'<td class="num">{pct:.1f}%</td>'
            f'<td class="num">{a["dofollow"]:,}</td>'
            f'<td class="num">{a["referring_domains"]:,}</td>'
            f'</tr>'
        )
    table_body = '\n'.join(rows_html) if rows_html else '<tr><td colspan="6">No anchor data.</td></tr>'

    # Flagged anchors
    flagged_html = ''
    if analysis['flagged']:
        flag_rows = []
        for f in analysis['flagged']:
            anchor_disp = html.escape(f['anchor'][:60] or '(empty)')
            flags = ', '.join(f['flags'])
            flag_rows.append(
                f'<tr><td class="anchor-text">{anchor_disp}</td>'
                f'<td>{f["type"].replace("_", " ")}</td>'
                f'<td class="num">{f["backlinks"]:,}</td>'
                f'<td>{html.escape(flags)}</td></tr>'
            )
        flagged_html = (
            '<div class="card">'
            '<h2>⚠ Penalty Risk Indicators</h2>'
            '<p class="muted">Anchors flagged for manual review:</p>'
            '<table class="data-table"><thead><tr>'
            '<th>Anchor</th><th>Type</th><th>Backlinks</th><th>Flags</th>'
            '</tr></thead><tbody>' + '\n'.join(flag_rows) + '</tbody></table>'
            '</div>'
        )
    else:
        flagged_html = (
            '<div class="card"><h2>✓ No Penalty Risk Indicators</h2>'
            '<p class="muted">No anchors flagged for manual review.</p></div>'
        )

    # Recommendations
    rec_html = ''.join(f'<li>{html.escape(r)}</li>' for r in analysis['recommendations'])

    # Distribution table
    dist_rows = []
    for t in ['exact_match', 'partial_match', 'branded', 'generic', 'naked_url', 'image_alt', 'empty', 'other']:
        d = dist.get(t, {'count': 0, 'backlinks': 0, 'pct': 0})
        if d['backlinks'] == 0 and d['count'] == 0:
            continue
        color = PIE_COLORS.get(t, '#6b7280')
        safe = SAFE_THRESHOLDS.get(t, 0)
        risky = RISKY_THRESHOLDS.get(t, 999)
        status = '✓ Safe'
        if d['pct'] > risky:
            status = '⚠ Risky'
        elif d['pct'] > safe:
            status = '◆ Monitor'
        dist_rows.append(
            f'<tr><td><span class="type-badge" style="background:{color}20;'
            f'color:{color}">{t.replace("_", " ")}</span></td>'
            f'<td class="num">{d["count"]}</td>'
            f'<td class="num">{d["backlinks"]:,}</td>'
            f'<td class="num">{d["pct"]:.1f}%</td>'
            f'<td class="num">{safe}%</td>'
            f'<td>{status}</td></tr>'
        )

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="robots" content="noindex,nofollow">
<title>Anchor Text Analysis — {html.escape(domain)}</title>
<style>
  :root {{
    --bg:#f8fafc; --card:#fff; --border:#e5e7eb; --text:#111827;
    --muted:#6b7280; --accent:#3b82f6;
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    background:var(--bg); color:var(--text); line-height:1.6; padding:20px; }}
  h1 {{ font-size:1.6rem; margin-bottom:4px; }}
  h2 {{ font-size:1.1rem; margin-bottom:10px; }}
  .muted {{ color:var(--muted); font-size:.9rem; }}
  .header {{ margin-bottom:20px; }}
  .header .muted {{ margin-top:2px; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
    gap:14px; margin-bottom:20px; }}
  .card {{ background:var(--card); border:1px solid var(--border);
    border-radius:10px; padding:18px; margin-bottom:20px; }}
  .stat-card {{ background:var(--card); border:1px solid var(--border);
    border-radius:10px; padding:16px; }}
  .stat-card .label {{ font-size:.78rem; color:var(--muted);
    text-transform:uppercase; letter-spacing:.05em; }}
  .stat-card .value {{ font-size:1.5rem; font-weight:700; margin-top:4px; }}
  .two-col {{ display:grid; grid-template-columns:1fr 1fr; gap:20px;
    margin-bottom:20px; }}
  @media (max-width:768px) {{ .two-col {{ grid-template-columns:1fr; }} }}
  .pie-wrap {{ display:flex; gap:24px; align-items:center; flex-wrap:wrap; }}
  .legend {{ display:flex; flex-direction:column; gap:6px; }}
  .legend-item {{ display:flex; align-items:center; gap:8px; font-size:.88rem; }}
  .legend-swatch {{ width:14px; height:14px; border-radius:3px; display:inline-block; }}
  .legend-pct {{ font-weight:700; margin-left:auto; }}
  .risk-gauge {{ display:flex; align-items:center; gap:16px; }}
  .risk-bar {{ flex:1; height:12px; background:#e5e7eb; border-radius:6px; overflow:hidden; }}
  .risk-fill {{ height:100%; border-radius:6px; background:{risk_color};
    width:{rs}%; transition:width .3s; }}
  .risk-badge {{ padding:4px 14px; border-radius:20px; font-weight:700; font-size:.9rem; }}
  .type-badge {{ padding:2px 10px; border-radius:12px; font-size:.78rem; font-weight:600; }}
  table.data-table {{ width:100%; border-collapse:collapse; font-size:.88rem; }}
  table.data-table th {{ text-align:left; padding:10px 12px; background:#f1f5f9;
    border-bottom:2px solid var(--border); font-weight:600; font-size:.8rem;
    text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }}
  table.data-table td {{ padding:10px 12px; border-bottom:1px solid var(--border); }}
  table.data-table tbody tr:hover {{ background:#f9fafb; }}
  .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .anchor-text {{ max-width:300px; word-break:break-word; }}
  .table-wrap {{ max-height:500px; overflow-y:auto; }}
  .table-controls {{ margin-bottom:10px; display:flex; gap:8px; flex-wrap:wrap; }}
  .filter-btn {{ padding:4px 12px; border:1px solid var(--border); border-radius:6px;
    background:#fff; cursor:pointer; font-size:.82rem; }}
  .filter-btn.active {{ background:var(--accent); color:#fff; border-color:var(--accent); }}
  .rec-list {{ padding-left:20px; }}
  .rec-list li {{ margin-bottom:6px; }}
  .footer {{ margin-top:24px; text-align:center; color:var(--muted); font-size:.82rem; }}
</style>
</head>
<body>

<div class="header">
  <h1>Anchor Text Analysis</h1>
  <p class="muted">{html.escape(domain)} — Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
</div>

<!-- Overview cards -->
<div class="cards">
  <div class="stat-card">
    <div class="label">Total Backlinks</div>
    <div class="value">{total_anchors:,}</div>
  </div>
  <div class="stat-card">
    <div class="label">Unique Anchors</div>
    <div class="value">{unique:,}</div>
  </div>
  <div class="stat-card">
    <div class="label">Top Anchor</div>
    <div class="value" style="font-size:1rem;line-height:1.4">{top_anchor_disp}</div>
    <div class="muted" style="font-size:.8rem">Type: {analysis['top_anchor_type'].replace('_',' ')}</div>
  </div>
  <div class="stat-card">
    <div class="label">Dofollow Ratio</div>
    <div class="value">{dofollow_ratio:.1f}%</div>
    <div class="muted" style="font-size:.8rem">{analysis['dofollow_total']:,} dofollow</div>
  </div>
  <div class="stat-card">
    <div class="label">Anchor Texts Returned</div>
    <div class="value">{analysis['total_anchor_texts']:,}</div>
  </div>
</div>

<!-- Two-column: pie + risk -->
<div class="two-col">
  <div class="card">
    <h2>Distribution</h2>
    {pie}
  </div>
  <div class="card">
    <h2>Over-Optimization Risk</h2>
    <div style="margin-bottom:14px">
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">
        <div style="font-size:2.4rem;font-weight:800;color:{risk_color}">{rs}</div>
        <div>/ 100</div>
        {_risk_badge(analysis['risk_level'])}
      </div>
      <div class="risk-gauge">
        <div class="risk-bar"><div class="risk-fill"></div></div>
      </div>
      <p class="muted" style="margin-top:8px">
        Score based on exact-match share, branded ratio, diversity, and
        concentration. Lower is better.
      </p>
    </div>
    <table class="data-table">
      <thead><tr><th>Type</th><th class="num">Count</th><th class="num">BL</th>
      <th class="num">%</th><th class="num">Safe ≤</th><th>Status</th></tr></thead>
      <tbody>{''.join(dist_rows)}</tbody>
    </table>
  </div>
</div>

<!-- Anchor table -->
<div class="card">
  <h2>Anchor Text Detail</h2>
  <div class="table-controls">
    <button class="filter-btn active" data-filter="all">All</button>
    <button class="filter-btn" data-filter="exact_match">Exact Match</button>
    <button class="filter-btn" data-filter="partial_match">Partial Match</button>
    <button class="filter-btn" data-filter="branded">Branded</button>
    <button class="filter-btn" data-filter="generic">Generic</button>
    <button class="filter-btn" data-filter="naked_url">Naked URL</button>
    <button class="filter-btn" data-filter="image_alt">Image Alt</button>
    <button class="filter-btn" data-filter="empty">Empty</button>
  </div>
  <div class="table-wrap">
    <table class="data-table">
      <thead><tr>
        <th>Anchor Text</th><th>Type</th><th class="num">Backlinks</th>
        <th class="num">% of Total</th><th class="num">Dofollow</th>
        <th class="num">Ref Domains</th>
      </tr></thead>
      <tbody>{table_body}</tbody>
    </table>
  </div>
</div>

{flagged_html}

<!-- Recommendations -->
<div class="card">
  <h2>Recommendations</h2>
  <ul class="rec-list">{rec_html}</ul>
</div>

<div class="footer">
  Zenfusion Anchor Text Analysis — noindex/nofollow — DataForSEO backlinks/anchors/live
</div>

<script>
  document.querySelectorAll('.filter-btn').forEach(btn => {{
    btn.addEventListener('click', () => {{
      document.querySelectorAll('.filter-btn').forEach(b =>
        b.classList.remove('active'));
      btn.classList.add('active');
      const filter = btn.dataset.filter;
      document.querySelectorAll('.anchor-row').forEach(row => {{
        if (filter === 'all' || row.dataset.type === filter) {{
          row.style.display = '';
        }} else {{
          row.style.display = 'none';
        }}
      }});
    }});
  }});
</script>
</body>
</html>"""

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(html_doc)

    return filepath


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Analyze anchor text distribution for a domain')
    parser.add_argument('--domain', required=True,
                        help='Domain to analyze (e.g. softprowatersystems.com)')
    parser.add_argument('--brand', default='',
                        help='Brand name for classification (e.g. softpro)')
    parser.add_argument('--limit', type=int, default=100,
                        help='Max anchor texts to fetch (default 100)')
    parser.add_argument('--output', default='/tmp/anchor_text_report',
                        help='Output directory for HTML report')
    args = parser.parse_args()

    domain = args.domain.strip().lower()
    brand = args.brand.strip()
    print(f'Fetching anchor text data for {domain} ...')
    anchors = fetch_anchor_text(domain, limit=args.limit)
    print(f'  Got {len(anchors)} anchor texts ({sum(a["backlinks"] for a in anchors):,} backlinks)')

    if not anchors:
        print('  WARNING: No anchor data returned. Generating empty report.')

    print('Classifying and analyzing distribution ...')
    analysis = analyze_distribution(anchors, brand)
    print(f'  Risk score: {analysis["risk_score"]} ({analysis["risk_level"]})')
    print(f'  Exact-match: {analysis["distribution"]["exact_match"]["pct"]:.1f}%')
    print(f'  Branded: {analysis["distribution"]["branded"]["pct"]:.1f}%')
    print(f'  Flagged anchors: {len(analysis["flagged"])}')

    print('Generating HTML report ...')
    report_path = generate_anchor_report(domain, anchors, analysis, args.output)
    print(f'  Report: {report_path}')

    # Also save JSON alongside
    json_path = report_path.replace('.html', '.json')
    with open(json_path, 'w') as f:
        json.dump({
            'domain': domain,
            'brand': brand,
            'analysis': {k: v for k, v in analysis.items() if k != 'classified'},
            'anchors': analysis.get('classified', anchors),
        }, f, indent=2, default=str)
    print(f'  JSON:   {json_path}')

    return report_path


if __name__ == '__main__':
    main()
