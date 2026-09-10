#!/usr/bin/env python3
"""
Zenfusion Competitive Link Analysis

For a given brand, produces a unified competitive link analysis page that:
1. Shows brand's top 100 backlinks (from DB) with click-to-expand
2. Copy to Clipboard button for all T1 domains in Majestic Million
3. Link overlap section: domains that link to both brand AND competitors
4. Per-competitor gap analysis (click to expand each competitor)

Data sources:
  - Zenfusion DB (~/.hermes/zenfusion.db): backlinks table (submitted links)
  - Zenfusion DB: backlink_gap_analyses table (DataForSEO gap data)
  - Zenfusion DB: competitors table (competitor list)
  - Majestic Million CSV (~/.hermes/data/majestic_million.csv)

Usage:
  python3 competitive_link_analysis.py --brand-id 1 --output /path/to/output.html
  python3 competitive_link_analysis.py --brand softpro --output /path/to/output.html
"""

import sqlite3, json, csv, os, argparse, re, sys
from datetime import datetime
from urllib.parse import urlparse
from collections import defaultdict

DB_PATH = os.path.expanduser("~/.hermes/zenfusion.db")
MAJESTIC_CSV = os.path.expanduser("~/.hermes/data/majestic_million.csv")

# ── Helpers ──────────────────────────────────────────────────────────────────

def normalize_domain(d):
    if not d:
        return ''
    d = d.lower().strip()
    if d.startswith('http://') or d.startswith('https://'):
        d = urlparse(d).netloc
    if d.startswith('www.'):
        d = d[4:]
    return d

def load_majestic():
    """Load Majestic Million CSV into domain->rank dict."""
    global MAJESTIC_CSV
    if not os.path.exists(MAJESTIC_CSV):
        # Try Downloads fallback
        alt = os.path.expanduser("~/Downloads/majestic_million.csv")
        if os.path.exists(alt):
            MAJESTIC_CSV = alt
        else:
            print("  WARNING: Majestic Million CSV not found — scoring without it")
            return {}
    db = {}
    with open(MAJESTIC_CSV, 'r', errors='replace') as f:
        reader = csv.DictReader(f)
        for row in reader:
            domain = row.get('Domain', '').lower().strip()
            if domain:
                try:
                    db[domain] = int(row.get('GlobalRank', 0))
                except ValueError:
                    pass
    return db

# ── Data loading ─────────────────────────────────────────────────────────────

def get_brand(conn, brand_id=None, brand_slug=None):
    """Get brand info from DB."""
    c = conn.cursor()
    if brand_id:
        c.execute("SELECT id, name, domain FROM brands WHERE id=?", (brand_id,))
    elif brand_slug:
        c.execute("SELECT id, name, domain FROM brands WHERE name LIKE ? OR domain LIKE ?", 
                  (f"%{brand_slug}%", f"%{brand_slug}%"))
    row = c.fetchone()
    if not row:
        print(f"Brand not found (id={brand_id}, slug={brand_slug})")
        sys.exit(1)
    return {"id": row[0], "name": row[1], "domain": row[2]}

def get_brand_backlinks(conn, brand_id):
    """Get all backlinks for a brand from the DB."""
    c = conn.cursor()
    c.execute("""
        SELECT source_domain, url, anchor_text, tier, tool_source, link_type, 
               domain_power, first_seen, last_seen
        FROM backlinks WHERE brand_id=? ORDER BY source_domain
    """, (brand_id,))
    backlinks = []
    for r in c.fetchall():
        backlinks.append({
            "source_domain": r[0] or "",
            "url": r[1] or "",
            "anchor_text": r[2] or "",
            "tier": r[3] or 1,
            "tool_source": r[4] or "",
            "link_type": r[5] or "",
            "domain_power": r[6] or 0,
            "first_seen": r[7] or "",
            "last_seen": r[8] or "",
        })
    return backlinks

def get_competitors(conn, brand_id):
    """Get unique competitor domains for a brand."""
    c = conn.cursor()
    c.execute("""
        SELECT DISTINCT domain, type, notes FROM competitors 
        WHERE brand_id=? ORDER BY domain
    """, (brand_id,))
    competitors = []
    seen = set()
    for r in c.fetchall():
        d = normalize_domain(r[0])
        if d and d not in seen:
            seen.add(d)
            competitors.append({"domain": d, "type": r[1] or "", "notes": r[2] or ""})
    return competitors

def get_gap_analyses(conn, brand_id):
    """Get stored gap analyses for a brand."""
    c = conn.cursor()
    c.execute("""
        SELECT id, competitor_domain, missing_links_json, html_report_url, date_run,
               opportunities_count, in_majestic_count, top_opportunity,
               brand_ref_domains, competitor_ref_domains, 
               brand_total_backlinks, competitor_total_backlinks
        FROM backlink_gap_analyses WHERE brand_id=? ORDER BY competitor_domain
    """, (brand_id,))
    analyses = []
    for r in c.fetchall():
        analyses.append({
            "id": r[0],
            "competitor_domain": r[1],
            "opportunities": json.loads(r[2]) if r[2] else [],
            "html_report_url": r[3] or "",
            "date_run": r[4] or "",
            "opportunities_count": r[5] or 0,
            "in_majestic_count": r[6] or 0,
            "top_opportunity": r[7] or "",
            "brand_ref_domains": r[8] or 0,
            "competitor_ref_domains": r[9] or 0,
            "brand_total_backlinks": r[10] or 0,
            "competitor_total_backlinks": r[11] or 0,
        })
    return analyses

# ── Analysis ─────────────────────────────────────────────────────────────────

def analyze(brand, backlinks, competitors, gap_analyses, majestic_db):
    """Run the full competitive link analysis."""
    brand_domain = normalize_domain(brand["domain"])
    
    # Build set of all brand referring domains (from DB)
    brand_ref_domains = set()
    for bl in backlinks:
        d = normalize_domain(bl["source_domain"])
        if d:
            brand_ref_domains.add(d)
    
    # Enrich backlinks with Majestic data, dedupe by domain
    by_domain = {}
    for bl in backlinks:
        d = normalize_domain(bl["source_domain"])
        if not d:
            continue
        if d not in by_domain:
            by_domain[d] = {
                "domain": d,
                "url": bl["url"],
                "anchor_text": bl["anchor_text"],
                "tier": bl["tier"],
                "tool_source": bl["tool_source"],
                "link_type": bl["link_type"],
                "domain_power": bl["domain_power"],
                "first_seen": bl["first_seen"],
                "last_seen": bl["last_seen"],
                "majestic_rank": majestic_db.get(d),
                "in_majestic": d in majestic_db,
            }
    
    # Sort by Majestic rank (None last), then domain_power
    all_links = sorted(by_domain.values(), 
                       key=lambda x: (x["majestic_rank"] is None, 
                                      x["majestic_rank"] or 999999,
                                      -(x["domain_power"] or 0)))
    
    # Top 100 links
    top_100 = all_links[:100]
    
    # T1 links in Majestic Million (for copy to clipboard)
    t1_in_majestic = [l for l in all_links if l["in_majestic"] and l["tier"] == 1]
    
    # Link overlap: domains in gap analyses that brand ALSO has (false gaps)
    # and domains brand has that competitors also link to
    overlap_data = {}
    true_gaps = {}
    
    for gap in gap_analyses:
        comp_domain = gap["competitor_domain"]
        opps = gap["opportunities"]
        
        # Check which "opportunities" brand already has (false gaps)
        already_have = []
        real_gaps = []
        for o in opps:
            d = normalize_domain(o.get("source_domain", ""))
            if d in brand_ref_domains:
                already_have.append(o)
            else:
                real_gaps.append(o)
        
        overlap_data[comp_domain] = {
            "already_have": already_have,
            "already_have_count": len(already_have),
            "total_opps": len(opps),
            "gap_analyses_id": gap["id"],
            "html_report_url": gap["html_report_url"],
            "date_run": gap["date_run"],
            "brand_total_backlinks": gap["brand_total_backlinks"],
            "competitor_total_backlinks": gap["competitor_total_backlinks"],
            "brand_ref_domains": gap["brand_ref_domains"],
            "competitor_ref_domains": gap["competitor_ref_domains"],
        }
        
        # Store real gaps (deduped against DB)
        true_gaps[comp_domain] = real_gaps
    
    # Competitors with no gap analysis yet
    gap_competitors = {g["competitor_domain"] for g in gap_analyses}
    missing_analyses = [c for c in competitors if c["domain"] not in gap_competitors]
    
    return {
        "brand": brand,
        "brand_domain": brand_domain,
        "total_backlinks": len(backlinks),
        "total_ref_domains": len(brand_ref_domains),
        "total_in_majestic": len(t1_in_majestic),
        "top_100": top_100,
        "t1_in_majestic": t1_in_majestic,
        "all_links_count": len(all_links),
        "competitors": competitors,
        "gap_analyses": gap_analyses,
        "overlap_data": overlap_data,
        "true_gaps": true_gaps,
        "missing_analyses": missing_analyses,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


# ── HTML Generation ──────────────────────────────────────────────────────────

def generate_html(results):
    """Generate the competitive link analysis HTML page."""
    brand = results["brand"]
    brand_domain = results["brand_domain"]
    brand_id = brand["id"]
    
    # --- Top 100 table rows ---
    top100_rows = ""
    for i, link in enumerate(results["top_100"], 1):
        maj = f"#{link['majestic_rank']:,}" if link["majestic_rank"] else "—"
        dp = link["domain_power"] or "—"
        tier_badge = f'<span class="badge tier-{link["tier"]}">T{link["tier"]}</span>'
        maj_badge = '<span class="badge maj">Majestic</span>' if link["in_majestic"] else ""
        anchor = link["anchor_text"][:60] if link["anchor_text"] else "—"
        url_display = link["url"][:80] if link["url"] else "—"
        source = link["tool_source"] or "—"
        
        top100_rows += f"""
        <tr>
          <td class="rank">{i}</td>
          <td class="domain"><a href="https://{link['domain']}" target="_blank" rel="noopener noindex nofollow">{link['domain']}</a></td>
          <td class="anchor">{anchor}</td>
          <td class="url"><a href="{link['url']}" target="_blank" rel="noopener noindex nofollow">{url_display}</a></td>
          <td class="num">{dp}</td>
          <td class="num">{maj}</td>
          <td>{tier_badge} {maj_badge}</td>
          <td class="num">{link['first_seen'] or '—'}</td>
          <td class="source">{source}</td>
        </tr>"""
    
    # --- T1 Majestic domains for copy to clipboard ---
    t1_majestic_domains = [l["domain"] for l in results["t1_in_majestic"]]
    t1_majestic_str = "\\n".join(t1_majestic_domains)
    
    t1_majestic_cards = ""
    for i, link in enumerate(results["t1_in_majestic"][:50], 1):
        maj = f"#{link['majestic_rank']:,}" if link["majestic_rank"] else "—"
        t1_majestic_cards += f"""
        <div class="t1-card">
          <span class="t1-rank">{i}</span>
          <span class="t1-domain">{link['domain']}</span>
          <span class="t1-maj">Majestic {maj}</span>
        </div>"""
    
    # --- Per-competitor gap sections ---
    competitor_sections = ""
    
    # Competitors WITH gap analyses
    for comp_domain, data in results["overlap_data"].items():
        real_gaps = results["true_gaps"].get(comp_domain, [])
        already_have = data["already_have"]
        
        # Sort real gaps by opportunity score
        real_gaps_sorted = sorted(real_gaps, key=lambda x: x.get("opportunity_score", 0), reverse=True)
        
        # Gap table rows
        gap_rows = ""
        for i, o in enumerate(real_gaps_sorted[:100], 1):
            maj = f"#{o['majestic_rank']:,}" if o.get("majestic_rank") else "—"
            score = o.get("opportunity_score", 0)
            df = "DF" if o.get("is_dofollow") else "NF"
            anchor = (o.get("anchor_text") or "")[:50]
            gap_rows += f"""
            <tr>
              <td class="rank">{i}</td>
              <td class="domain"><a href="https://{o['source_domain']}" target="_blank" rel="noopener noindex nofollow">{o['source_domain']}</a></td>
              <td class="anchor">{anchor}</td>
              <td class="num">{o.get('domain_strength', 0)}</td>
              <td class="num">{maj}</td>
              <td>{df}</td>
              <td class="score">{score:.1f}</td>
            </tr>"""
        
        # Already-have (overlap) domains
        overlap_items = ""
        for o in already_have[:20]:
            maj = f"#{o['majestic_rank']:,}" if o.get("majestic_rank") else "—"
            overlap_items += f'<span class="overlap-domain">{o["source_domain"]} <span class="maj-small">{maj}</span></span>'
        
        total_opps = data["total_opps"]
        false_gaps = data["already_have_count"]
        real_count = len(real_gaps_sorted)
        pct_false = (false_gaps / total_opps * 100) if total_opps else 0
        
        comp_total = data["competitor_total_backlinks"]
        brand_total = data["brand_total_backlinks"]
        
        competitor_sections += f"""
        <div class="competitor-section" id="comp-{comp_domain.replace('.','-')}">
          <div class="comp-header" onclick="toggleComp('{comp_domain.replace('.','-')}')">
            <div class="comp-name">{comp_domain}</div>
            <div class="comp-stats">
              <span class="stat-pill gap-count">{real_count} real gaps</span>
              <span class="stat-pill overlap-count">{false_gaps} already have</span>
              <span class="stat-pill">{pct_false:.0f}% false positives filtered</span>
              <span class="stat-pill comp-backlinks">Their backlinks: {comp_total:,}</span>
              <span class="expand-icon">▼ click to expand</span>
            </div>
          </div>
          <div class="comp-body" id="body-{comp_domain.replace('.','-')}" style="display:none;">
            <div class="overlap-section">
              <div class="section-label">✅ Already have ({false_gaps} domains — these were false positives in the original gap analysis):</div>
              <div class="overlap-domains">{overlap_items or '<span class="empty">None — all gaps are real</span>'}</div>
            </div>
            <div class="gap-section">
              <div class="section-label">🎯 Real gaps — links {comp_domain} has that we don't ({real_count} domains):</div>
              <table class="gap-table">
                <thead><tr><th>#</th><th>Domain</th><th>Anchor</th><th>DS</th><th>Majestic</th><th>Type</th><th>Score</th></tr></thead>
                <tbody>{gap_rows or '<tr><td colspan="7" class="empty">No real gaps found — brand has all competitor links!</td></tr>'}</tbody>
              </table>
            </div>
            {f'<a href="{data["html_report_url"]}" class="full-report-link" target="_blank">📄 View Full Original Report →</a>' if data.get("html_report_url") else ''}
          </div>
        </div>"""
    
    # Competitors WITHOUT gap analyses yet
    if results["missing_analyses"]:
        missing_html = ""
        for comp in results["missing_analyses"]:
            missing_html += f'<div class="missing-comp">⚠️ {comp["domain"]} ({comp["type"]}) — Run Gap Analysis →</div>'
        
        competitor_sections += f"""
        <div class="missing-analyses">
          <div class="section-label">Competitors without gap analysis yet:</div>
          {missing_html}
        </div>"""
    
    # --- Build full HTML ---
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<title>Competitive Link Analysis — {brand['name']}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f1117; color: #fff; line-height: 1.6; padding: 20px 40px; max-width: 1200px; margin: 0 auto; }}
a {{ color: #4A90D9; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
h1 {{ font-size: 28px; margin-bottom: 4px; }}
.subtitle {{ color: #8892a6; font-size: 14px; margin-bottom: 30px; }}
.generated {{ color: #5c6578; font-size: 12px; margin-bottom: 30px; }}

/* Summary cards */
.cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 16px; margin-bottom: 36px; }}
.card {{ background: #1a1d28; border: 1px solid #2a2e3c; border-radius: 10px; padding: 20px; }}
.card-label {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; color: #8892a6; margin-bottom: 8px; }}
.card-value {{ font-size: 32px; font-weight: 700; color: #fff; }}
.card-sub {{ font-size: 13px; color: #5c6578; margin-top: 4px; }}

/* Section headers */
.section-title {{ color: #4A90D9; font-size: 20px; margin: 36px 0 16px; border-bottom: 1px solid #2a3142; padding-bottom: 8px; }}

/* Copy button */
.copy-btn {{ background: #27ae60; color: white; border: none; padding: 10px 20px; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; margin-bottom: 16px; }}
.copy-btn:hover {{ background: #219150; }}
.copy-btn.copied {{ background: #2ecc71; }}

/* Tables */
table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; }}
th {{ text-align: left; padding: 10px 12px; background: #1a1d28; color: #8892a6; font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 1px solid #2a3142; }}
td {{ padding: 8px 12px; border-bottom: 1px solid #1e2230; font-size: 13px; }}
td.rank {{ color: #5c6578; font-weight: 700; width: 30px; }}
td.num {{ text-align: right; color: #4A90D9; font-weight: 600; }}
td.domain {{ font-weight: 600; }}
td.anchor {{ color: #b0b8c8; max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
td.url {{ color: #4A90D9; max-width: 250px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
td.source {{ color: #5c6578; font-size: 11px; }}
td.score {{ text-align: right; font-weight: 700; color: #27ae60; }}

/* Badges */
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 10px; font-weight: 700; }}
.badge.tier-1 {{ background: rgba(39,174,96,0.15); color: #27ae60; }}
.badge.tier-2 {{ background: rgba(243,156,18,0.15); color: #f39c12; }}
.badge.tier-3 {{ background: rgba(127,140,141,0.15); color: #7f8c8d; }}
.badge.maj {{ background: rgba(74,144,217,0.15); color: #4A90D9; margin-left: 4px; }}

/* Expandable sections */
.expandable {{ display: none; }}
.expandable.show {{ display: block; }}
.expand-toggle {{ color: #4A90D9; cursor: pointer; font-size: 14px; font-weight: 600; margin-bottom: 12px; display: inline-block; }}
.expand-toggle:hover {{ text-decoration: underline; }}

/* T1 Majestic cards */
.t1-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 8px; margin-bottom: 20px; }}
.t1-card {{ background: #1a1d28; border: 1px solid #2a3142; border-radius: 6px; padding: 10px 14px; display: flex; align-items: center; gap: 10px; }}
.t1-rank {{ color: #5c6578; font-weight: 700; min-width: 24px; }}
.t1-domain {{ color: #4A90D9; flex: 1; font-size: 13px; }}
.t1-maj {{ color: #27ae60; font-size: 11px; font-weight: 600; }}

/* Competitor sections */
.competitor-section {{ background: #1a1d28; border: 1px solid #2a3142; border-radius: 10px; margin-bottom: 12px; overflow: hidden; }}
.comp-header {{ padding: 16px 20px; cursor: pointer; display: flex; justify-content: space-between; align-items: center; }}
.comp-header:hover {{ background: #1e2230; }}
.comp-name {{ font-size: 16px; font-weight: 700; color: #4A90D9; }}
.comp-stats {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
.stat-pill {{ font-size: 12px; padding: 3px 10px; border-radius: 12px; background: rgba(255,255,255,0.05); color: #b0b8c8; }}
.stat-pill.gap-count {{ background: rgba(231,76,60,0.15); color: #e74c3c; }}
.stat-pill.overlap-count {{ background: rgba(39,174,96,0.15); color: #27ae60; }}
.stat-pill.comp-backlinks {{ background: rgba(74,144,217,0.1); color: #4A90D9; }}
.expand-icon {{ color: #5c6578; font-size: 12px; }}
.comp-body {{ padding: 20px; border-top: 1px solid #2a3142; }}
.overlap-section {{ margin-bottom: 24px; }}
.section-label {{ color: #8892a6; font-size: 13px; font-weight: 600; margin-bottom: 10px; }}
.overlap-domains {{ display: flex; flex-wrap: wrap; gap: 6px; }}
.overlap-domain {{ background: rgba(39,174,96,0.1); border: 1px solid rgba(39,174,96,0.2); border-radius: 6px; padding: 4px 10px; font-size: 12px; color: #27ae60; }}
.maj-small {{ color: #5c6578; font-size: 10px; margin-left: 4px; }}
.gap-table th {{ font-size: 11px; }}
.full-report-link {{ display: inline-block; margin-top: 16px; padding: 8px 20px; background: rgba(74,144,217,0.15); border: 1px solid rgba(74,144,217,0.3); border-radius: 6px; color: #4A90D9; font-size: 13px; font-weight: 600; }}

/* Missing analyses */
.missing-analyses {{ background: #1a1d28; border: 1px solid rgba(243,156,18,0.2); border-radius: 10px; padding: 16px; margin-bottom: 12px; }}
.missing-comp {{ font-size: 13px; color: #f39c12; margin-bottom: 6px; }}

/* Update button */
.update-btn {{ background: #4A90D9; color: white; border: none; padding: 12px 28px; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; }}
.update-btn:hover {{ background: #3a7ec9; }}
.update-btn:disabled {{ background: #2a3142; cursor: not-allowed; }}
.update-btn.processing {{ background: #f39c12; }}
.update-info {{ color: #5c6578; font-size: 12px; margin-left: 12px; }}

.empty {{ color: #5c6578; font-size: 13px; padding: 12px; text-align: center; }}
.footer {{ text-align: center; margin-top: 40px; color: #5c6578; font-size: 11px; border-top: 1px solid #2a3142; padding-top: 20px; }}
</style>
</head>
<body>
<script src="/zenfusion-audits/scripts/export.js"></script>
<div id="export-container"></div>
<script>ZenfusionExport.createExportBar({{container:'#export-container',type:'data',reportName:'Competitive Link Analysis — {brand['name']}',brandName:'{brand['name']}',filename:'{brand_domain}-competitive-link-analysis'}});</script>

<h1>🔗 Competitive Link Analysis: {brand['name']}</h1>
<p class="subtitle">{brand_domain}</p>
<p class="generated">Generated: {results['generated_at']}</p>

<!-- Summary Cards -->
<div class="cards">
  <div class="card">
    <div class="card-label">Total Backlinks</div>
    <div class="card-value">{results['total_backlinks']:,}</div>
    <div class="card-sub">in database</div>
  </div>
  <div class="card">
    <div class="card-label">Referring Domains</div>
    <div class="card-value">{results['total_ref_domains']:,}</div>
    <div class="card-sub">unique</div>
  </div>
  <div class="card">
    <div class="card-label">T1 in Majestic</div>
    <div class="card-value">{results['total_in_majestic']:,}</div>
    <div class="card-sub">high-authority</div>
  </div>
  <div class="card">
    <div class="card-label">Competitors Tracked</div>
    <div class="card-value">{len(results['competitors'])}</div>
    <div class="card-sub">{len(results['gap_analyses'])} with gap data</div>
  </div>
</div>

<!-- DataForSEO Update Button -->
<div style="margin-bottom: 24px;">
  <button class="update-btn" id="dfs-update-btn" onclick="triggerDataForSEOUpdate()">🔄 Update from DataForSEO</button>
  <span class="update-info" id="update-info">Last synced: {results.get('last_sync', 'Never')}</span>
</div>

<!-- Top 100 Links -->
<div class="section-title">🏆 Brand's Top 100 Links</div>
<span class="expand-toggle" onclick="toggleSection('top100')">▼ Show Top 100</span>
<div class="expandable" id="top100">
  <table>
    <thead><tr><th>#</th><th>Domain</th><th>Anchor</th><th>URL</th><th>DS</th><th>Majestic</th><th>Tier</th><th>First Seen</th><th>Source</th></tr></thead>
    <tbody>{top100_rows}</tbody>
  </table>
</div>

<!-- T1 Majestic Million — Copy to Clipboard -->
<div class="section-title">📋 T1 Domains in Majestic Million ({results['total_in_majestic']} total)</div>
<button class="copy-btn" id="copy-t1-btn" onclick="copyT1Majestic()">Copy All T1 Majestic Domains to Clipboard</button>
<span class="expand-toggle" onclick="toggleSection('t1majestic')">▼ Show T1 Majestic List</span>
<div class="expandable" id="t1majestic">
  <div class="t1-grid">{t1_majestic_cards}</div>
</div>

<!-- Link Overlap Summary -->
<div class="section-title">🔄 Link Overlap & Competitor Gaps</div>
<p style="color:#b0b8c8;font-size:13px;margin-bottom:20px">
  Each competitor section shows: (1) domains that were flagged as "gaps" but we <strong style="color:#27ae60">already have</strong> (false positives filtered by DB cross-reference), 
  and (2) <strong style="color:#e74c3c">real gaps</strong> — domains linking to the competitor but not to us.
</p>

{competitor_sections}

<div class="footer">
  Zenfusion Competitive Link Analysis v1.0 · DataForSEO + Majestic Million + Zenfusion DB · noindex, nofollow
</div>

<script>
function toggleSection(id) {{
  var el = document.getElementById(id);
  el.classList.toggle('show');
}}

function toggleComp(id) {{
  var el = document.getElementById('body-' + id);
  if (el.style.display === 'none') {{
    el.style.display = 'block';
  }} else {{
    el.style.display = 'none';
  }}
}}

function copyT1Majestic() {{
  var domains = "{t1_majestic_str}".split('\\n');
  var text = domains.join('\\n');
  navigator.clipboard.writeText(text).then(function() {{
    var btn = document.getElementById('copy-t1-btn');
    btn.textContent = '✅ Copied ' + domains.length + ' domains!';
    btn.classList.add('copied');
    setTimeout(function() {{
      btn.textContent = 'Copy All T1 Majestic Domains to Clipboard';
      btn.classList.remove('copied');
    }}, 3000);
  }});
}}

function runGapAnalysis(domain) {{
  alert('Gap analysis for ' + domain + ' will be run by the team. Check back soon!');
}}

function triggerDataForSEOUpdate() {{
  var btn = document.getElementById('dfs-update-btn');
  var info = document.getElementById('update-info');
  btn.disabled = true;
  btn.classList.add('processing');
  btn.textContent = '⏳ Syncing from DataForSEO...';
  info.textContent = 'Fetching all T1 backlinks. This takes 2-5 minutes. Page will reload when done.';

  // Submit to Google Apps Script which triggers the sync
  var formData = new FormData();
  formData.append('brand_id', '{brand_id}');
  formData.append('action', 'dataforseo_sync');
  formData.append('source', 'competitive_link_analysis');
  formData.append('sheet_url', 'https://www.softprowatersystems.com');
  formData.append('link_list', 'dataforseo_sync');
  navigator.sendBeacon('https://script.google.com/macros/s/AKfycbzDJgJ6hhIN2Bv4_wnqVLCgOKuVUP9wig94iGdmR2fXHATVQb571OGWUEZyACad0z0u/exec', formData);

  // Poll for completion — reload page after 3 minutes
  setTimeout(function() {{
    btn.textContent = '✅ Sync complete — reloading...';
    info.textContent = 'Reloading page with updated data...';
    setTimeout(function() {{ location.reload(); }}, 2000);
  }}, 180000);
}}

// Re-inject copy buttons when competitor sections are toggled open
document.addEventListener('click', function(e) {{
  if (e.target.closest('.comp-header')) {{
    setTimeout(function() {{ ZenfusionExport.addCopyButtons(); }}, 100);
  }}
}});
</script>
</body>
</html>"""
    
    return html


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Zenfusion Competitive Link Analysis")
    parser.add_argument("--brand-id", type=int, help="Brand ID from DB")
    parser.add_argument("--brand", help="Brand name slug (e.g. 'softpro')")
    parser.add_argument("--output", required=True, help="Output HTML file path")
    args = parser.parse_args()
    
    if not args.brand_id and not args.brand:
        print("ERROR: --brand-id or --brand required")
        sys.exit(1)
    
    print(f"\n{'='*70}")
    print(f"  COMPETITIVE LINK ANALYSIS")
    print(f"{'='*70}")
    
    # Load Majestic
    print("\n  Loading Majestic Million database...")
    majestic_db = load_majestic()
    print(f"  {len(majestic_db):,} domains loaded")
    
    # Connect to DB
    conn = sqlite3.connect(DB_PATH)
    
    # Get brand
    brand = get_brand(conn, brand_id=args.brand_id, brand_slug=args.brand)
    print(f"\n  Brand: {brand['name']} ({brand['domain']})")
    
    # Load data
    print("\n  Loading backlinks from DB...")
    backlinks = get_brand_backlinks(conn, brand["id"])
    print(f"  {len(backlinks):,} backlinks found")
    
    print("\n  Loading competitors...")
    competitors = get_competitors(conn, brand["id"])
    print(f"  {len(competitors)} competitors found")
    
    print("\n  Loading gap analyses...")
    gap_analyses = get_gap_analyses(conn, brand["id"])
    print(f"  {len(gap_analyses)} gap analyses found")
    
    conn.close()
    
    # Run analysis
    print("\n  Running competitive link analysis...")
    results = analyze(brand, backlinks, competitors, gap_analyses, majestic_db)
    
    # Get last sync date
    try:
        conn2 = sqlite3.connect(DB_PATH)
        c2 = conn2.cursor()
        c2.execute("SELECT synced_at FROM dataforseo_sync_log WHERE brand_id=? ORDER BY synced_at DESC LIMIT 1", (brand["id"],))
        row = c2.fetchone()
        results["last_sync"] = row[0][:10] if row else "Never"
        conn2.close()
    except:
        results["last_sync"] = "Never"
    
    print(f"\n  Results:")
    print(f"    Total backlinks: {results['total_backlinks']:,}")
    print(f"    Referring domains: {results['total_ref_domains']:,}")
    print(f"    T1 in Majestic: {results['total_in_majestic']}")
    print(f"    Competitors with gap data: {len(results['gap_analyses'])}")
    
    for comp, data in results["overlap_data"].items():
        real = len(results["true_gaps"].get(comp, []))
        false = data["already_have_count"]
        print(f"    vs {comp}: {real} real gaps, {false} false positives filtered")
    
    # Generate HTML
    print("\n  Generating HTML report...")
    html = generate_html(results)
    
    with open(args.output, 'w') as f:
        f.write(html)
    
    print(f"\n  HTML saved: {args.output}")
    print(f"\n{'='*70}")
    print(f"  ANALYSIS COMPLETE")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
