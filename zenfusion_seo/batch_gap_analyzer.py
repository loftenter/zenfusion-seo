#!/usr/bin/env python3
"""
Batch Backlink Gap Analyzer — runs gap analysis for all competitors of a brand.
Uses the existing backlink_gap_analyzer.py functions, stores results in DB.
"""

import sys, os, json, time, sqlite3
from datetime import datetime

# Add scripts dir to path
sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))

# Import from existing analyzer
from backlink_gap_analyzer import (
    normalize_domain, fetch_backlinks_summary, fetch_backlinks_list,
    load_majestic, majestic_rank, compute_opportunity_score
)

DB_PATH = os.path.expanduser("~/.hermes/zenfusion.db")

def get_competitors_needing_analysis(conn, brand_id):
    """Get competitors that don't have gap analyses yet."""
    c = conn.cursor()
    c.execute("SELECT DISTINCT competitor_domain FROM backlink_gap_analyses WHERE brand_id=?", (brand_id,))
    have = set(r[0] for r in c.fetchall())
    
    c.execute("SELECT DISTINCT domain FROM competitors WHERE brand_id=? ORDER BY domain", (brand_id,))
    all_comps = [r[0] for r in c.fetchall()]
    
    return [d for d in all_comps if d not in have and normalize_domain(d) not in have]


def run_single_gap(brand_domain, comp_domain, majestic_db):
    """Run a single gap analysis and return results dict."""
    brand_domain = normalize_domain(brand_domain)
    comp_domain = normalize_domain(comp_domain)
    
    # Fetch summaries
    brand_summary = fetch_backlinks_summary(brand_domain)
    comp_summary = fetch_backlinks_summary(comp_domain)
    
    # Fetch backlink lists
    brand_links = fetch_backlinks_list(brand_domain, limit=1000)
    comp_links = fetch_backlinks_list(comp_domain, limit=1000)
    
    # Build referring domain sets
    brand_ref_domains = set()
    for bl in brand_links:
        d = normalize_domain(bl.get('domain_from', ''))
        if d:
            brand_ref_domains.add(d)
    
    comp_ref_domains = set()
    for bl in comp_links:
        d = normalize_domain(bl.get('domain_from', ''))
        if d:
            comp_ref_domains.add(d)
    
    # Find gap
    missing_domains = comp_ref_domains - brand_ref_domains
    
    # Build opportunities from missing backlinks
    comp_by_domain = {}
    for bl in comp_links:
        d = normalize_domain(bl.get('domain_from', ''))
        if d not in missing_domains:
            continue
        existing = comp_by_domain.get(d)
        if existing is None:
            comp_by_domain[d] = bl
        else:
            if (bl.get('is_dofollow') and not existing.get('is_dofollow')) or \
               (bl.get('is_dofollow') == existing.get('is_dofollow') and \
                bl.get('domain_strength', 0) > existing.get('domain_strength', 0)):
                comp_by_domain[d] = bl
    
    opportunities = []
    for d, bl in comp_by_domain.items():
        maj_rank = majestic_rank(d, majestic_db)
        ds = bl.get('domain_strength', 0) or 0
        is_df = bl.get('is_dofollow', False)
        score = compute_opportunity_score(ds, maj_rank, is_df, brand_domain, comp_domain)
        
        opportunities.append({
            'source_domain': d,
            'source_url': bl.get('source_url', ''),
            'anchor_text': bl.get('anchor', ''),
            'target_url': bl.get('target_url', ''),
            'links_type': bl.get('links_type', ''),
            'is_dofollow': is_df,
            'first_seen': bl.get('first_seen', ''),
            'last_seen': bl.get('last_seen', ''),
            'domain_strength': ds,
            'majestic_rank': maj_rank,
            'in_majestic': maj_rank is not None,
            'opportunity_score': round(score, 1),
        })
    
    opportunities.sort(key=lambda x: x['opportunity_score'], reverse=True)
    
    return {
        'brand_domain': brand_domain,
        'competitor_domain': comp_domain,
        'brand_summary': brand_summary,
        'competitor_summary': comp_summary,
        'brand_ref_domains_count': len(brand_ref_domains),
        'competitor_ref_domains_count': len(comp_ref_domains),
        'missing_domains_count': len(missing_domains),
        'opportunities': opportunities,
    }


def store_gap_analysis(conn, brand_id, results):
    """Store gap analysis results in the DB."""
    c = conn.cursor()
    opps = results['opportunities']
    in_maj = sum(1 for o in opps if o.get('in_majestic'))
    top_opp = opps[0]['source_domain'] if opps else ''
    
    c.execute("""INSERT INTO backlink_gap_analyses 
        (brand_id, competitor_domain, missing_links_json, html_report_url, date_run,
         brand_domain, opportunities_count, in_majestic_count, top_opportunity,
         brand_ref_domains, competitor_ref_domains, brand_total_backlinks, competitor_total_backlinks)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (brand_id, results['competitor_domain'], json.dumps(opps), '', datetime.now().strftime('%Y-%m-%d'),
         results['brand_domain'], len(opps), in_maj, top_opp,
         results['brand_ref_domains_count'], results['competitor_ref_domains_count'],
         results.get('brand_summary', {}).get('backlinks', 0),
         results.get('competitor_summary', {}).get('backlinks', 0)))
    
    conn.commit()


def main():
    brand_id = 1
    brand_domain = 'softprowatersystems.com'
    
    print(f"\n{'='*70}")
    print(f"  BATCH BACKLINK GAP ANALYSIS")
    print(f"  Brand: {brand_domain}")
    print(f"{'='*70}")
    
    # Load Majestic
    print("\n  Loading Majestic Million database...")
    majestic_db = load_majestic()
    print(f"  {len(majestic_db):,} domains loaded")
    
    # Connect to DB
    conn = sqlite3.connect(DB_PATH)
    
    # Get competitors needing analysis
    competitors = get_competitors_needing_analysis(conn, brand_id)
    print(f"\n  Competitors to analyze: {len(competitors)}")
    
    results_summary = []
    
    for i, comp in enumerate(competitors, 1):
        print(f"\n  [{i}/{len(competitors)}] Analyzing vs {comp}...")
        try:
            results = run_single_gap(brand_domain, comp, majestic_db)
            store_gap_analysis(conn, brand_id, results)
            
            opps = results['opportunities']
            in_maj = sum(1 for o in opps if o.get('in_majestic'))
            top = opps[0]['source_domain'] if opps else 'N/A'
            
            print(f"    Backlinks: {results.get('competitor_summary', {}).get('backlinks', 0):,}")
            print(f"    Ref domains: {results['competitor_ref_domains_count']}")
            print(f"    Gap opportunities: {len(opps)} ({in_maj} in Majestic)")
            print(f"    Top: {top}")
            
            results_summary.append({
                'competitor': comp,
                'opps': len(opps),
                'in_majestic': in_maj,
                'top': top,
                'comp_backlinks': results.get('competitor_summary', {}).get('backlinks', 0),
            })
            
        except Exception as e:
            print(f"    ERROR: {e}")
            results_summary.append({'competitor': comp, 'error': str(e)})
        
        # Rate limit — 0.5s between competitors (each makes 4 API calls)
        time.sleep(0.5)
    
    conn.close()
    
    # Print summary
    print(f"\n{'='*70}")
    print(f"  BATCH COMPLETE — {len(results_summary)} competitors processed")
    print(f"{'='*70}")
    print(f"\n  {'Competitor':<35} {'Opps':>6} {'Majestic':>8} {'Comp BLs':>10}  Top")
    print(f"  {'-'*35} {'-'*6} {'-'*8} {'-'*10}  {'-'*20}")
    
    total_opps = 0
    for r in results_summary:
        if 'error' in r:
            print(f"  {r['competitor']:<35} ERROR: {r['error'][:40]}")
        else:
            total_opps += r['opps']
            print(f"  {r['competitor']:<35} {r['opps']:>6} {r['in_majestic']:>8} {r['comp_backlinks']:>10,}  {r['top']}")
    
    print(f"\n  Total gap opportunities across all competitors: {total_opps:,}")


if __name__ == "__main__":
    main()
