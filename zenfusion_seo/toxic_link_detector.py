#!/usr/bin/env python3
"""
Toxic Link Detector — Identifies potentially harmful backlinks and generates a
Google Disavow Tool compatible file.

Usage:
  python3 toxic_link_detector.py --domain softprowatersystems.com --output /tmp/test_toxic

Requires DataForSEO credentials in ~/.hermes/.env:
  DATAFORSEO_LOGIN=...
  DATAFORSEO_PASSWORD=...
"""

import os
import sys
import json
import base64
import argparse
import urllib.request
import re
from datetime import datetime
from urllib.parse import urlparse

ENV_PATH = os.path.expanduser("~/.hermes/.env")

# ── Credentials ──────────────────────────────────────────────────────────────

def get_creds():
    """Load DataForSEO credentials from env or ~/.hermes/.env."""
    login = os.environ.get("DATAFORSEO_LOGIN")
    password = os.environ.get("DATAFORSEO_PASSWORD")
    if not login or not password:
        if os.path.exists(ENV_PATH):
            with open(ENV_PATH) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("DATAFORSEO_LOGIN="):
                        login = line.split("=", 1)[1]
                    elif line.startswith("DATAFORSEO_PASSWORD="):
                        password = line.split("=", 1)[1]
    if not login or not password:
        print("ERROR: DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD not found.")
        print("Set them in ~/.hermes/.env or as environment variables.")
        sys.exit(1)
    return login, password


def dfs_api_call(endpoint, payload, timeout=120):
    """Make a DataForSEO API POST call. payload is a single dict (wrapped in array)."""
    login, password = get_creds()
    creds = base64.b64encode(f"{login}:{password}".encode()).decode()
    url = f"https://api.dataforseo.com/v3/{endpoint}"
    headers = {
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/json",
    }
    body = json.dumps([payload]).encode()
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        if data.get("status_code") != 20000:
            print(f"  API error: {data.get('status_message', 'Unknown')}")
            return None
        return data
    except Exception as e:
        print(f"  Request error: {e}")
        return None


def normalize_domain(d):
    if not d:
        return ""
    d = d.lower().strip()
    if d.startswith("http://") or d.startswith("https://"):
        d = urlparse(d).netloc
    if d.startswith("www."):
        d = d[4:]
    return d


# ── Function 1: fetch_backlink_profile ───────────────────────────────────────

def fetch_backlink_profile(domain, limit=1000):
    """Fetch individual backlinks via DataForSEO backlinks/backlinks/live.

    Returns list of dicts with:
      url, referring_domain, anchor_text, spam_score, domain_strength,
      dofollow, first_seen, last_seen
    """
    payload = {
        "target": domain,
        "mode": "as_is",
        "limit": limit,
        "include_subdomains": True,
    }
    data = dfs_api_call("backlinks/backlinks/live", payload)
    if not data or not data.get("tasks"):
        return []

    task = data["tasks"][0]
    if task.get("status_code") != 20000:
        print(f"  Task error: {task.get('status_message', 'Unknown')}")
        return []

    result = task.get("result", [{}])[0]
    items = result.get("items", [])

    backlinks = []
    for item in items:
        links_type = item.get("type", "")
        is_dofollow = links_type == "dofollow"
        backlinks.append({
            "url": item.get("url_from", ""),
            "referring_domain": normalize_domain(item.get("domain_from", "")),
            "anchor_text": item.get("anchor", "") or "",
            "spam_score": item.get("backlinks_spam_score", 0) or 0,
            "domain_strength": item.get("domain_from_rank", 0) or 0,
            "dofollow": is_dofollow,
            "first_seen": (item.get("first_seen", "") or "")[:10],
            "last_seen": (item.get("last_seen", "") or "")[:10],
            "links_type": links_type,
            "target_url": item.get("url_to", ""),
        })
    return backlinks


# ── Function 2: classify_toxicity ──────────────────────────────────────────────

# Topic keywords that signal potentially toxic neighborhoods
TOXIC_TOPICS = {
    "adult": ["porn", "xxx", "sex", "escort", "nude", "adult", "erotic", "milf", "hentai"],
    "gambling": ["casino", "poker", "bet", "gamble", "slots", "roulette", "blackjack", "wager", "sportsbook"],
    "pharma": ["viagra", "cialis", "steroid", "opioid", "xanax", "valium", "tramadol",
               "phentermine", "hydrocodone", "oxycodone", "drug-online", "pill", "rx-"],
}

EXACT_MATCH_PATTERNS = [
    "buy", "cheap", "free", "download", "best", "top", "review",
    "discount", "deal", "offer", "price", "order",
]


def classify_toxicity(backlink):
    """Classify a backlink as Toxic, Suspicious, or Clean.

    Returns dict:
      classification: 'Toxic' | 'Suspicious' | 'Clean'
      reasons: list of strings explaining why flagged
    """
    reasons = []
    spam_score = backlink.get("spam_score", 0)
    domain_strength = backlink.get("domain_strength", 0)
    anchor = (backlink.get("anchor_text") or "").lower()
    referring_domain = (backlink.get("referring_domain") or "").lower()

    # ── Primary rules ────────────────────────────────────────────────────────
    if spam_score > 50 or domain_strength < 5:
        reasons.append(f"High spam score ({spam_score}) or very low domain strength ({domain_strength})")
    elif 20 <= spam_score <= 50 or (5 <= domain_strength < 15):
        reasons.append(f"Moderate spam score ({spam_score}) or low domain strength ({domain_strength})")

    # ── Exact-match anchor over-optimization ────────────────────────────────
    if anchor:
        # Check for exact-match commercial anchor patterns
        ema_match = any(kw in anchor for kw in EXACT_MATCH_PATTERNS)
        if ema_match:
            reasons.append(f"Exact-match commercial anchor: \"{backlink.get('anchor_text', '')}\"")

    # ── Penalized-domain heuristics ──────────────────────────────────────────
    # Domain strength of 0 with spam score > 0 suggests a penalized/weak domain
    if domain_strength == 0 and spam_score > 0:
        reasons.append(f"Likely penalized domain (strength=0, spam={spam_score})")

    # ── Toxic topic detection ───────────────────────────────────────────────
    combined = f"{referring_domain} {anchor}"
    for topic, keywords in TOXIC_TOPICS.items():
        for kw in keywords:
            if kw in combined:
                reasons.append(f"Link from {topic}-related site (keyword: '{kw}')")
                break

    # ── Final classification ────────────────────────────────────────────────
    if spam_score > 50 or domain_strength < 5 or any(
        kw in combined for kws in TOXIC_TOPICS.values() for kw in kws
    ):
        # Toxic topic links are always toxic
        if not reasons:
            reasons.append("General toxicity signal")
        classification = "Toxic"
    elif 20 <= spam_score <= 50 or domain_strength < 15 or len(reasons) > 0:
        classification = "Suspicious"
    else:
        classification = "Clean"

    return {
        "classification": classification,
        "reasons": reasons,
        "spam_score": spam_score,
        "domain_strength": domain_strength,
    }


# ── Function 3: generate_disavow_file ──────────────────────────────────────────

def generate_disavow_file(toxic_links, output_path=None):
    """Generate a Google Disavow Tool compatible text file.

    Uses domain: entries for toxic domains (deduplicated).
    If output_path is provided, writes to file. Returns the file content string.
    """
    disavow_domains = set()
    for link in toxic_links:
        domain = normalize_domain(link.get("referring_domain", ""))
        if domain:
            disavow_domains.add(domain)

    lines = [
        f"# Google Disavow File — Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"# Toxic backlinks detected by toxic_link_detector.py",
        f"# Total domains to disavow: {len(disavow_domains)}",
        "",
    ]
    for domain in sorted(disavow_domains):
        lines.append(f"domain:{domain}")

    content = "\n".join(lines) + "\n"

    if output_path:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w") as f:
            f.write(content)

    return content


# ── Helper: SVG pie chart ──────────────────────────────────────────────────────

def _svg_pie_chart(toxic_count, suspicious_count, clean_count):
    """Generate an SVG pie chart for toxicity distribution."""
    total = toxic_count + suspicious_count + clean_count
    if total == 0:
        return '<p style="text-align:center;color:#888;">No data for chart</p>'

    import math
    cx, cy, r = 110, 110, 80
    colors = {"toxic": "#e74c3c", "suspicious": "#f39c12", "clean": "#27ae60"}

    segments = [
        ("Toxic", toxic_count, colors["toxic"]),
        ("Suspicious", suspicious_count, colors["suspicious"]),
        ("Clean", clean_count, colors["clean"]),
    ]

    svg_parts = [f'<svg width="340" height="260" viewBox="0 0 340 260" xmlns="http://www.w3.org/2000/svg">']
    start_angle = -90.0  # start at top

    for label, count, color in segments:
        if count == 0:
            continue
        sweep = (count / total) * 360.0
        end_angle = start_angle + sweep
        # Convert to radians
        sa = math.radians(start_angle)
        ea = math.radians(end_angle)
        x1 = cx + r * math.cos(sa)
        y1 = cy + r * math.sin(sa)
        x2 = cx + r * math.cos(ea)
        y2 = cy + r * math.sin(ea)
        large_arc = 1 if sweep > 180 else 0
        path = (
            f"M {cx},{cy} L {x1:.1f},{y1:.1f} A {r},{r} 0 {large_arc} 1 {x2:.1f},{y2:.1f} Z"
        )
        svg_parts.append(f'<path d="{path}" fill="{color}" stroke="#fff" stroke-width="2"/>')
        start_angle = end_angle

    svg_parts.append(f'<circle cx="{cx}" cy="{cy}" r="35" fill="#fff"/>')
    svg_parts.append(
        f'<text x="{cx}" y="{cy-2}" text-anchor="middle" font-size="20" font-weight="bold" fill="#333">{total}</text>'
    )
    svg_parts.append(f'<text x="{cx}" y="{cy+14}" text-anchor="middle" font-size="10" fill="#999">Backlinks</text>')

    # Legend
    ly = 20
    for label, count, color in segments:
        pct = round((count / total) * 100, 1) if total else 0
        svg_parts.append(f'<rect x="220" y="{ly}" width="14" height="14" fill="{color}" rx="2"/>')
        svg_parts.append(f'<text x="240" y="{ly+12}" font-size="13" fill="#555">{label}: {count} ({pct}%)</text>')
        ly += 24

    svg_parts.append("</svg>")
    return "\n".join(svg_parts)


# ── Function 4: generate_toxicity_report ───────────────────────────────────────

def generate_toxicity_report(domain, backlinks, analysis, output_path):
    """Generate an HTML toxicity report.

    Args:
        domain: target domain string
        backlinks: list of backlink dicts from fetch_backlink_profile
        analysis: list of dicts (backlink + classification + reasons)
        output_path: directory to write report files
    """
    os.makedirs(output_path, exist_ok=True)

    total = len(analysis)
    toxic = [a for a in analysis if a["classification"] == "Toxic"]
    suspicious = [a for a in analysis if a["classification"] == "Suspicious"]
    clean = [a for a in analysis if a["classification"] == "Clean"]

    toxic_count = len(toxic)
    suspicious_count = len(suspicious)
    clean_count = len(clean)

    spam_scores = [a.get("spam_score", 0) for a in analysis]
    avg_spam = round(sum(spam_scores) / len(spam_scores), 2) if spam_scores else 0

    # Top toxic domains
    toxic_domain_counts = {}
    for a in toxic:
        d = a.get("referring_domain", "")
        if d:
            toxic_domain_counts[d] = toxic_domain_counts.get(d, 0) + 1
    top_toxic_domains = sorted(toxic_domain_counts.items(), key=lambda x: -x[1])[:15]

    # Anchor text analysis on toxic links
    toxic_anchors = [a.get("anchor_text", "") for a in toxic if a.get("anchor_text")]
    ema_count = 0
    for anchor in toxic_anchors:
        a_lower = anchor.lower()
        if any(kw in a_lower for kw in EXACT_MATCH_PATTERNS):
            ema_count += 1
    ema_pct = round((ema_count / len(toxic_anchors)) * 100, 1) if toxic_anchors else 0

    # Disavow file content
    disavow_content = generate_disavow_file(toxic)
    disavow_domains = sorted(set(
        normalize_domain(a.get("referring_domain", ""))
        for a in toxic if a.get("referring_domain")
    ))

    # ── Build HTML ───────────────────────────────────────────────────────────
    def esc(s):
        import html
        return html.escape(str(s))

    # Summary cards
    cards = f"""
    <div class="card-grid">
      <div class="card"><div class="card-num">{total}</div><div class="card-label">Total Backlinks</div></div>
      <div class="card card-toxic"><div class="card-num">{toxic_count}</div><div class="card-label">Toxic</div></div>
      <div class="card card-suspicious"><div class="card-num">{suspicious_count}</div><div class="card-label">Suspicious</div></div>
      <div class="card card-clean"><div class="card-num">{clean_count}</div><div class="card-label">Clean</div></div>
      <div class="card"><div class="card-num">{avg_spam}</div><div class="card-label">Avg Spam Score</div></div>
    </div>
    """

    # Pie chart
    pie_svg = _svg_pie_chart(toxic_count, suspicious_count, clean_count)

    # Toxic table
    def build_table(rows, table_id):
        if not rows:
            return f'<p style="color:#888;padding:12px;">No links in this category.</p>'
        header = """
        <tr><th>Domain</th><th>URL</th><th>Anchor Text</th><th>Spam Score</th><th>Domain Strength</th><th>Reason Flagged</th></tr>
        """
        body_rows = []
        for a in rows[:500]:
            reasons_html = "<br>".join(esc(r) for r in a.get("reasons", []))
            body_rows.append(f"""
            <tr>
              <td>{esc(a.get('referring_domain', ''))}</td>
              <td><a href="{esc(a.get('url', ''))}" target="_blank" rel="nofollow noopener" style="color:#2563eb;">{esc(a.get('url', '')[:80])}</a></td>
              <td>{esc(a.get('anchor_text', ''))[:60]}</td>
              <td>{a.get('spam_score', 0)}</td>
              <td>{a.get('domain_strength', 0)}</td>
              <td>{reasons_html}</td>
            </tr>""")
        return f"""
        <table class="data-table" id="{table_id}">
          <thead>{header}</thead>
          <tbody>{''.join(body_rows)}</tbody>
        </table>
        <button class="expand-btn" onclick="toggleTable('{table_id}')">Show/Hide {len(rows)} rows</button>
        """

    toxic_table = build_table(toxic, "toxic-table")
    suspicious_table = build_table(suspicious, "suspicious-table")

    # Top toxic domains
    if top_toxic_domains:
        domain_rows = "".join(
            f"<tr><td>{esc(d)}</td><td>{c}</td></tr>" for d, c in top_toxic_domains
        )
        top_domains_html = f"""
        <table class="data-table">
          <thead><tr><th>Domain</th><th>Toxic Link Count</th></tr></thead>
          <tbody>{domain_rows}</tbody>
        </table>
        """
    else:
        top_domains_html = '<p style="color:#888;padding:12px;">No toxic domains found.</p>'

    # Disavow preview
    disavow_preview = esc(disavow_content)
    disavow_domain_count = len(disavow_domains)

    # Recommendations
    rec_disavow = "Disavow all toxic links from low-quality or spammy domains listed above."
    rec_monitor = f"Monitor the {suspicious_count} suspicious links — re-evaluate in 30-60 days."
    rec_leave = f"Leave the {clean_count} clean links alone — they contribute positively to your profile."
    rec_ema = ""
    if ema_pct > 30:
        rec_ema = f"<li>⚠️ <strong>High exact-match anchor %</strong> ({ema_pct}% among toxic links) — diversify your anchor profile.</li>"

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex,nofollow">
<title>Toxic Link Report — {esc(domain)}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #f8fafc; color: #1e293b; padding: 24px; max-width: 1200px; margin: 0 auto; }}
  h1 {{ font-size: 1.8rem; margin-bottom: 4px; }}
  h2 {{ font-size: 1.3rem; margin: 32px 0 12px; padding-bottom: 8px; border-bottom: 2px solid #e2e8f0; }}
  .meta {{ color: #64748b; font-size: 0.9rem; margin-bottom: 24px; }}
  .card-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin: 20px 0; }}
  .card {{ background: #fff; border-radius: 12px; padding: 24px; text-align: center;
          box-shadow: 0 1px 3px rgba(0,0,0,0.08); border: 1px solid #e2e8f0; }}
  .card-num {{ font-size: 2.2rem; font-weight: 700; }}
  .card-label {{ font-size: 0.85rem; color: #64748b; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.5px; }}
  .card-toxic .card-num {{ color: #e74c3c; }}
  .card-suspicious .card-num {{ color: #f39c12; }}
  .card-clean .card-num {{ color: #27ae60; }}
  .chart-container {{ background: #fff; border-radius: 12px; padding: 24px; text-align: center;
                     box-shadow: 0 1px 3px rgba(0,0,0,0.08); border: 1px solid #e2e8f0; }}
  .data-table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px;
                overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08); margin: 12px 0; }}
  .data-table th {{ background: #f1f5f9; padding: 10px 12px; text-align: left; font-size: 0.85rem;
                    text-transform: uppercase; letter-spacing: 0.5px; color: #475569; }}
  .data-table td {{ padding: 10px 12px; border-top: 1px solid #e2e8f0; font-size: 0.9rem; }}
  .data-table tbody tr:hover {{ background: #f8fafc; }}
  .expand-btn {{ background: #2563eb; color: #fff; border: none; padding: 8px 16px; border-radius: 6px;
                 cursor: pointer; font-size: 0.85rem; margin: 8px 0; }}
  .expand-btn:hover {{ background: #1d4ed8; }}
  .code-block {{ background: #1e293b; color: #e2e8f0; padding: 16px; border-radius: 8px;
                font-family: 'Courier New', monospace; font-size: 0.85rem; overflow-x: auto;
                max-height: 400px; overflow-y: auto; white-space: pre-wrap; position: relative; }}
  .copy-btn {{ position: absolute; top: 8px; right: 8px; background: #475569; color: #fff;
              border: none; padding: 4px 12px; border-radius: 4px; cursor: pointer; font-size: 0.8rem; }}
  .copy-btn:hover {{ background: #64748b; }}
  .recommendations {{ background: #fff; border-radius: 12px; padding: 24px; margin-top: 12px;
                      box-shadow: 0 1px 3px rgba(0,0,0,0.08); border: 1px solid #e2e8f0; }}
  .recommendations ul {{ list-style: none; padding-left: 0; }}
  .recommendations li {{ padding: 8px 0; border-bottom: 1px solid #f1f5f9; font-size: 0.95rem; }}
  .recommendations li:last-child {{ border-bottom: none; }}
  .anchor-analysis {{ background: #fff; border-radius: 12px; padding: 20px; margin-top: 12px;
                      box-shadow: 0 1px 3px rgba(0,0,0,0.08); border: 1px solid #e2e8f0; }}
  .stat-line {{ font-size: 1.1rem; margin: 8px 0; }}
  .stat-line strong {{ color: #2563eb; }}
  .section {{ margin-bottom: 32px; }}
  footer {{ margin-top: 40px; text-align: center; color: #94a3b8; font-size: 0.8rem; }}
</style>
</head>
<body>
<h1>🔗 Toxic Link Detector Report</h1>
<div class="meta">Domain: <strong>{esc(domain)}</strong> &nbsp;|&nbsp; Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} &nbsp;|&nbsp; Backlinks analyzed: {total}</div>

<h2>📊 Summary</h2>
{cards}

<h2>📈 Toxicity Distribution</h2>
<div class="chart-container">
{pie_svg}
</div>

<h2>☠️ Toxic Backlinks ({toxic_count})</h2>
<div class="section">
{toxic_table}
</div>

<h2>⚠️ Suspicious Backlinks ({suspicious_count})</h2>
<div class="section">
{suspicious_table}
</div>

<h2>🔝 Top Toxic Domains</h2>
<div class="section">
{top_domains_html}
</div>

<h2>📝 Anchor Text Analysis (Toxic Links)</h2>
<div class="anchor-analysis">
  <div class="stat-line">Total toxic anchors analyzed: <strong>{len(toxic_anchors)}</strong></div>
  <div class="stat-line">Exact-match commercial anchors: <strong>{ema_count}</strong></div>
  <div class="stat-line">Exact-match percentage among toxic: <strong>{ema_pct}%</strong></div>
</div>

<h2>📋 Disavow File Preview ({disavow_domain_count} domains)</h2>
<div class="code-block" id="disavow-code">{disavow_preview}
<button class="copy-btn" onclick="copyDisavow()">Copy</button></div>

<h2>💡 Recommendations</h2>
<div class="recommendations">
  <ul>
    <li>🔴 <strong>Disavow:</strong> {esc(rec_disavow)}</li>
    <li>🟡 <strong>Monitor:</strong> {esc(rec_monitor)}</li>
    <li>🟢 <strong>Leave alone:</strong> {esc(rec_leave)}</li>
    {rec_ema}
    <li>📄 Download the disavow file and submit via <a href="https://search.google.com/search-console/disavow-links" target="_blank" rel="nofollow noopener">Google Search Console Disavow Tool</a>.</li>
  </ul>
</div>

<footer>Generated by toxic_link_detector.py &nbsp;|&nbsp; DataForSEO Backlinks API &nbsp;|&nbsp; noindex,nofollow</footer>

<script>
function toggleTable(id) {{
  var table = document.getElementById(id);
  if (table.style.display === 'none' || table.style.display === '') {{
    table.style.display = 'table';
  }} else {{
    table.style.display = 'none';
  }}
}}
function copyDisavow() {{
  var code = document.getElementById('disavow-code');
  var text = code.innerText.replace('Copy', '').trim();
  navigator.clipboard.writeText(text).then(function() {{
    alert('Disavow content copied to clipboard!');
  }});
}}
</script>
</body>
</html>"""

    report_file = os.path.join(output_path, f"toxic_link_report_{domain.replace('.', '_')}.html")
    with open(report_file, "w") as f:
        f.write(html_content)

    # Also write the disavow file
    disavow_file = os.path.join(output_path, "disavow.txt")
    with open(disavow_file, "w") as f:
        f.write(disavow_content)

    return report_file


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Detect toxic backlinks and generate a disavow file.")
    parser.add_argument("--domain", required=True, help="Target domain (e.g., softprowatersystems.com)")
    parser.add_argument("--limit", type=int, default=1000, help="Max backlinks to fetch (default: 1000)")
    parser.add_argument("--output", default="/tmp/toxic_report", help="Output directory for report files")
    args = parser.parse_args()

    domain = args.domain.lower().strip()
    output_dir = args.output

    print(f"\n{'='*60}")
    print(f"  Toxic Link Detector — {domain}")
    print(f"{'='*60}\n")

    # 1. Fetch backlinks
    print(f"[1/4] Fetching backlink profile (limit={args.limit})...")
    backlinks = fetch_backlink_profile(domain, limit=args.limit)
    print(f"      Retrieved {len(backlinks)} backlinks.\n")

    if not backlinks:
        print("  No backlinks found. Exiting.")
        sys.exit(0)

    # 2. Classify toxicity
    print("[2/4] Classifying toxicity...")
    analysis = []
    for bl in backlinks:
        result = classify_toxicity(bl)
        merged = {**bl, **result}
        analysis.append(merged)

    toxic_count = sum(1 for a in analysis if a["classification"] == "Toxic")
    suspicious_count = sum(1 for a in analysis if a["classification"] == "Suspicious")
    clean_count = sum(1 for a in analysis if a["classification"] == "Clean")
    print(f"      Toxic: {toxic_count} | Suspicious: {suspicious_count} | Clean: {clean_count}\n")

    # 3. Generate disavow file
    print("[3/4] Generating disavow file...")
    toxic_links = [a for a in analysis if a["classification"] == "Toxic"]
    disavow_content = generate_disavow_file(toxic_links)
    disavow_domains = set(normalize_domain(a.get("referring_domain", "")) for a in toxic_links if a.get("referring_domain"))
    print(f"      {len(disavow_domains)} domains to disavow.\n")

    # 4. Generate HTML report
    print("[4/4] Generating HTML report...")
    report_path = generate_toxicity_report(domain, backlinks, analysis, output_dir)
    print(f"      Report saved to: {report_path}")
    print(f"      Disavow file saved to: {os.path.join(output_dir, 'disavow.txt')}\n")

    print(f"{'='*60}")
    print(f"  Done! Toxic: {toxic_count} | Suspicious: {suspicious_count} | Clean: {clean_count}")
    print(f"  Report: {report_path}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
